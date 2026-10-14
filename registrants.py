"""Webinar registrants (EverWebinar / WebinarJam) -> Brevo list + Telegram alert.

EverWebinar (Profile > Integrations > Autoresponder > Custom Webhook) posts each new registration to
/ew/<event> on the reply service. For every new registrant this module:
  1. keeps the record in the shared store (so nobody is added twice),
  2. adds or updates them in Brevo on the list "Webinar Registrants" (folder "Well-Led Network"), with
     FIRSTNAME, LASTNAME, WEBINAR (the webinar's name), LEAD_SOURCE and REGISTERED_AT, using
     BREVO_API_KEY in Render. The list is reusable: any future source can add people to it, and the
     Brevo automation "Webinar Registrants nurture" starts from it,
  3. sends a short "New masterclass registrant" message to Telegram.

Nothing is emailed from this code. EverWebinar sends its own reminders and follow-ups.

EverWebinar does not publish its payload field names, so the parser looks for them by meaning
(email, first name, webinar name...) wherever they sit, in JSON or form posts. The last raw payload is
kept so the field names can be checked. If EW_TOKEN is set in Render, requests must carry it
(EverWebinar's Bearer Token option).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import os

import requests

UK = ZoneInfo("Europe/London")
SOURCE = "EverWebinar"
SEEN = "wln:ew_seen"              # Redis set of "email|webinar" already handled
REGS = "wln:ew_registrants"       # Redis hash: "email|webinar" -> record (JSON)
LAST_RAW = "wln:ew_last"
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

# Our webinars by EverWebinar id, so the alert and Brevo show a readable name and the offer it sells
WEBINARS = {
    "12": ("CQC Registration Masterclass", "Risk Check + Application Review"),
}


def _flatten(obj, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _find(flat: dict, *wanted: str) -> str:
    """First non-empty value whose key (last path part, normalised) matches a wanted name."""
    for w in wanted:
        w = _norm(w)
        for k, v in flat.items():
            if v in (None, "", [], {}):
                continue
            last = _norm(re.split(r"[.\[]", k)[-1] or k)
            if last == w or _norm(k) == w:
                return str(v).strip()
    return ""


def parse(payload) -> dict:
    flat = _flatten(payload if isinstance(payload, (dict, list)) else {"body": payload})
    email = _find(flat, "email", "attendee_email", "user_email", "registrant_email", "lead_email").lower()
    if not EMAIL.match(email):
        email = next((str(v).strip().lower() for v in flat.values()
                      if isinstance(v, str) and EMAIL.match(v.strip())), "")
    first = _find(flat, "first_name", "firstname", "fname", "given_name")
    last = _find(flat, "last_name", "lastname", "lname", "family_name", "surname")
    full = _find(flat, "name", "full_name", "fullname", "attendee_name")
    if not first and full:
        first, _, rest = full.partition(" ")
        last = last or rest
    wid = _find(flat, "webinar_id", "webinarid", "event_id")
    wname = _find(flat, "webinar_name", "webinar_title", "webinar", "title", "event_name")
    known = WEBINARS.get(wid)
    if known and (not wname or wname.isdigit()):
        wname = known[0]
    return {
        "email": email, "first": first[:60], "last": last[:60],
        "phone": _find(flat, "phone", "phone_number", "mobile")[:40],
        "webinar_id": wid, "webinar": (wname or "EverWebinar masterclass")[:120],
        "offer": known[1] if known else "",
        "schedule": _find(flat, "schedule", "date", "event_date", "webinar_date", "start_time")[:60],
        "live_link": _find(flat, "live_room_url", "live_link", "webinar_link", "room_url")[:300],
    }


BREVO = "https://api.brevo.com/v3"
LIST_NAME = "Webinar Registrants"
FOLDER = "Well-Led Network"
ATTRS = ("WEBINAR", "LEAD_SOURCE", "REGISTERED_AT")


def _brevo(method: str, path: str, body: dict | None = None):
    key = os.getenv("BREVO_API_KEY", "").strip()
    r = requests.request(method, f"{BREVO}{path}", json=body, timeout=30,
                         headers={"api-key": key, "accept": "application/json", "content-type": "application/json"})
    if r.status_code == 401:
        raise PermissionError("Brevo key rejected")
    return r


def _list_id(store) -> int | None:
    """The Brevo list "Webinar Registrants", created on first use (with its folder and text fields)."""
    cached = store.r.get("wln:ew_brevo_list") if store.r else None
    if cached:
        return int(cached)
    lid = None
    offset = 0
    while lid is None:
        r = _brevo("GET", f"/contacts/lists?limit=50&offset={offset}")
        r.raise_for_status()
        lists = r.json().get("lists") or []
        lid = next((l["id"] for l in lists if l.get("name") == LIST_NAME), None)
        if len(lists) < 50:
            break
        offset += 50
    if lid is None:
        r = _brevo("GET", "/contacts/folders?limit=50&offset=0")
        r.raise_for_status()
        folders = r.json().get("folders") or []
        fid = next((f["id"] for f in folders if f.get("name") == FOLDER), None)
        if fid is None:
            r = _brevo("POST", "/contacts/folders", {"name": FOLDER})
            r.raise_for_status()
            fid = r.json()["id"]
        r = _brevo("POST", "/contacts/lists", {"name": LIST_NAME, "folderId": fid})
        r.raise_for_status()
        lid = r.json()["id"]
    for a in ATTRS:  # 400 just means the field already exists
        _brevo("POST", f"/contacts/attributes/normal/{a}", {"type": "text"})
    if store.r:
        store.r.set("wln:ew_brevo_list", str(lid))
    return lid


def _add_to_brevo(store, reg: dict) -> str:
    if not os.getenv("BREVO_API_KEY", "").strip():
        return "not added (BREVO_API_KEY is not set in Render)"
    try:
        lid = _list_id(store)
        r = _brevo("POST", "/contacts", {
            "email": reg["email"], "updateEnabled": True, "listIds": [lid],
            "attributes": {"FIRSTNAME": reg["first"], "LASTNAME": reg["last"], "WEBINAR": reg["webinar"],
                           "LEAD_SOURCE": SOURCE, "REGISTERED_AT": reg["date"]},
        })
        if r.status_code not in (200, 201, 204):
            return f"not added (Brevo said {r.status_code}: {r.text[:150]})"
        return f"added to Brevo (list {LIST_NAME})"
    except PermissionError:
        return "not added: the Brevo API key in Render was rejected and needs replacing"
    except Exception as exc:
        return f"not added (Brevo error: {str(exc)[:150]})"


def handle(store, event: str, payload, send) -> dict:
    if store.r:
        store.r.set(LAST_RAW, json.dumps({"event": event, "at": time.time(), "payload": payload})[:20000])
    reg = parse(payload)
    if not reg["email"]:
        return {"ok": True, "skipped": "no usable email"}
    key = f"{reg['email']}|{reg['webinar_id'] or reg['webinar']}"
    if store.r and not store.r.sadd(SEEN, key):
        return {"ok": True, "skipped": "already handled"}
    reg["date"] = datetime.now(UK).strftime("%d %b %Y %H:%M")
    reg["event"] = event
    reg["brevo"] = _add_to_brevo(store, reg)
    if store.r:
        store.r.hset(REGS, key, json.dumps(reg))
    name = f"{reg['first']} {reg['last']}".strip() or "(no name given)"
    send(
        "New masterclass registrant\n"
        f"{name}, {reg['email']}" + (f", {reg['phone']}" if reg["phone"] else "") + "\n"
        f"Webinar: {reg['webinar']}" + (f" (sells {reg['offer']})" if reg["offer"] else "") + "\n"
        f"Brevo: {reg['brevo']}\n"
        "EverWebinar sends the reminders; the Brevo list starts the longer follow-up."
    )
    return {"ok": True, "brevo": reg["brevo"]}


def recent(store, days: int = 30) -> list[dict]:
    if not store.r:
        return []
    out = [json.loads(v) for v in store.r.hvals(REGS)]
    return sorted(out, key=lambda r: r.get("date", ""), reverse=True)
