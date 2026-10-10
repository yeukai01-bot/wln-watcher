"""Free AI practice room tasters (Tough Tongue) -> SAM.AI leads + Telegram alert.

When someone finishes one of the four free tasters, this:
  1. reads the finished session from Tough Tongue (TT_API_KEY in Render),
  2. picks out the name and email the person gave the AI inspector, their score and weakest area,
  3. adds them to SAM.AI as a lead in the folder "AI Practice Room Leads" (SAM_API_TOKEN in Render),
  4. tells Yeukai in Telegram straight away.
Nobody is emailed from here. Follow-up emails are drafted by the daily Claude task and sent only after his yes.

Runs every 10 minutes from the keep-awake ping (UK daytime), instantly when Tough Tongue calls /tt/ping,
and once more from the morning cron as a safety net. Each session is handled once (remembered in Redis).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

TT_API = "https://api.toughtongueai.com/api/public"
SAM_API = "https://go.sam.ai/api/v1"
FOLDER_NAME = "AI Practice Room Leads"
SOURCE = "Practice Room"

# Free tasters and the paid next step each one leads to
TASTERS = {
    "6ac82e6a798e99b2d6b27c0c": ("Well-Led Interview Taster", "https://shor.by/well-led-ready"),
    "6ac76618dffaab401d2369ab": ("Fit Person Interview Taster", "https://shor.by/fit-person-ready"),
    "6ac831e6798e99b2d6b27c10": ("Difficult Conversation Taster", "https://shor.by/hard-conversations-ready"),
    "6ac833ce798e99b2d6b27c15": ("Commissioner Pitch Taster", "https://shor.by/first-packages"),
}

SEEN = "wln:tt_seen"            # Redis set of session ids already handled
LEADS = "wln:practice_leads"    # Redis hash: session id -> lead record (JSON)
SAM_WARNED = "wln:tt_sam_warned"
OWN = re.compile(r"kajidori|yeukai|example\.com|toughtongue|welllednetwork|sam\.ai|shor\.by", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PREPARED = re.compile(r"Prepared for:\s*\**\s*([^(\n\"*]+?)\s*\(", re.I)

_lock = threading.Lock()


def _tt(path: str, params: dict | None = None) -> dict:
    key = os.getenv("TT_API_KEY", "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    r = requests.get(f"{TT_API}{path}", params=params or {}, timeout=30,
                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _sam(method: str, path: str, body: dict) -> dict:
    token = os.getenv("SAM_API_TOKEN", "").strip()
    r = requests.request(method, f"{SAM_API}{path}", json=body, timeout=30,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code in (401, 403):
        raise PermissionError("SAM.AI token expired")
    r.raise_for_status()
    return r.json() if r.content else {}


def _id(resp: dict):
    d = resp.get("data", resp) if isinstance(resp, dict) else {}
    if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict):
        d = d["data"]
    return d.get("id") if isinstance(d, dict) else None


def extract(s: dict) -> dict:
    """Name, email, score and weakest area from a finished session."""
    blob = json.dumps(s, ensure_ascii=False).replace("\\n", "\n")
    ev = s.get("evaluation_results") or {}
    email = (s.get("user_email") or "").strip()
    if not email or OWN.search(email):
        email = next((e for e in EMAIL.findall(blob) if not OWN.search(e)), "")
    m = PREPARED.search(blob)
    name = (m.group(1).strip() if m else "") or (s.get("user_name") or "").strip()
    if not name or "@" in name or OWN.search(name) or name.lower() in ("not given", "anonymous", "guest"):
        name = ""
    score = ev.get("final_score")
    if score in (None, ""):
        score = ev.get("overall_score", "")
    weakest = (ev.get("weaknesses") or "").strip()
    if not weakest:
        cards = [c for c in ev.get("report_card") or [] if isinstance(c, dict) and c.get("score") is not None]
        if cards:
            low = min(cards, key=lambda c: c.get("score", 99))
            weakest = f"{low.get('topic', '')}: {low.get('note', '')}".strip(": ")
    return {"email": email.lower(), "name": name, "score": str(score)[:120], "weakest": weakest[:400]}


def _add_to_sam(store, lead: dict) -> str:
    if not os.getenv("SAM_API_TOKEN", "").strip():
        return "not added (SAM_API_TOKEN is not set in Render)"
    try:
        folder = _id(_sam("POST", "/crm/lead-folders/find-or-create", {"name": FOLDER_NAME}))
        first, _, last = (lead["name"] or "Practice Room Lead").partition(" ")
        contact = _id(_sam("POST", "/crm/contacts", {
            "first_name": first, "last_name": last or "", "email": lead["email"],
            "job_title": "", "type": "lead", "source": SOURCE,
        }))
        if not contact:
            return "not added (SAM.AI did not return a contact id)"
        body = {
            "contact_id": contact, "name": lead["name"] or lead["email"], "status": "open",
            "source": SOURCE, "campaign": lead["taster"],
            "notes": (f"{lead['date']} {lead['taster']}: score {lead['score']}. "
                      f"Weakest area: {lead['weakest'] or 'see Tough Tongue report'}. "
                      f"Paid next step: {lead['next']}. Tough Tongue session {lead['session']}. "
                      "Follow-up email to be drafted by the daily task, sent only after Yeukai's yes."),
        }
        if folder:
            body["folder_id"] = folder
        _sam("POST", "/crm/leads", body)
        return f"added to SAM.AI (folder {FOLDER_NAME})"
    except PermissionError:
        last = float(store.r.get(SAM_WARNED) or 0) if store.r else 0
        if time.time() - last > 24 * 3600:
            if store.r:
                store.r.set(SAM_WARNED, str(time.time()))
            return "not added: the SAM.AI token in Render has expired and needs renewing"
        return "not added (SAM.AI token expired)"
    except Exception as exc:
        return f"not added (SAM.AI error: {str(exc)[:150]})"


def check(store, send, hours: int = 72) -> str:
    if not os.getenv("TT_API_KEY", "").strip():
        return "TT_API_KEY not set"
    if not _lock.acquire(blocking=False):
        return "busy"
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        done = []
        for sid_scn, (taster, nxt) in TASTERS.items():
            try:
                items = _tt("/v2/sessions", {"scenario_id": sid_scn, "from_date": since, "is_org": "true", "limit": 50}).get("sessions") or []
            except Exception as exc:
                print(f"[tt list failed] {taster}: {exc}")
                continue
            for it in items:
                sid = it.get("id")
                if not sid or (store.r and store.r.sismember(SEEN, sid)):
                    continue
                if (it.get("status") or "").lower() != "completed" or not it.get("evaluation_results"):
                    continue  # not scored yet: try again on the next check
                try:
                    full = _tt(f"/sessions/{sid}", {"include_recording_url": "false"})
                except Exception:
                    full = it
                info = extract({**it, **full})
                lead = {"session": sid, "taster": taster, "next": next_step(taster),
                        "date": (it.get("completed_at") or it.get("created_at") or "")[:10], **info}
                if store.r:
                    store.r.sadd(SEEN, sid)
                if not info["email"] or OWN.search(info["email"]):
                    if not info["email"]:
                        send(f"Practice room: someone finished the {taster} but gave no email, so they could not be added "
                             f"to SAM.AI. Score {info['score'] or 'n/a'}. Session {sid} in Tough Tongue History.")
                    continue
                lead["sam"] = _add_to_sam(store, lead)
                if store.r:
                    store.r.hset(LEADS, sid, json.dumps(lead))
                send(
                    "New practice room lead\n"
                    f"{lead['name'] or '(name not given)'} <{lead['email']}>\n"
                    f"{taster}, {lead['date']}\n"
                    f"Score: {lead['score'] or 'n/a'}\n"
                    f"Weakest area: {lead['weakest'] or 'see the Tough Tongue report'}\n"
                    f"Paid next step: {nxt}\n"
                    f"SAM.AI: {lead['sam']}\n"
                    "Your follow-up email will be drafted for your yes in the morning task."
                )
                done.append(sid)
        return f"handled {len(done)}"
    finally:
        _lock.release()


def next_step(taster: str) -> str:
    for t, n in TASTERS.values():
        if t == taster:
            return n
    return ""


def leads(store, days: int = 14) -> list[dict]:
    if not store.r:
        return []
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    for v in (store.r.hvals(LEADS) or []):
        try:
            d = json.loads(v)
        except Exception:
            continue
        if (d.get("date") or "") >= cutoff:
            out.append(d)
    return sorted(out, key=lambda d: d.get("date", ""), reverse=True)


def check_in_background(store, send) -> None:
    threading.Thread(target=lambda: _safe(store, send), daemon=True).start()


def _safe(store, send) -> None:
    try:
        print("[practice rooms]", check(store, send))
    except Exception as exc:
        print("[practice rooms failed]", exc)
