"""SalesBlink (cold email) side of the CQC report radar.

Each morning, radar prospects that are supported living services with a new Requires improvement or
Inadequate report, a company number (UK PECR: limited companies only) and an email address that passed
the Clearout check are added to the SalesBlink "CQC Radar - Supported Living" list.

That list feeds an evergreen SalesBlink sequence which Yeukai approved once. The sequence opens with a
wait of a few days, so the email lands after the LinkedIn invitation sent by Sendpilot. It sends from the
warmed-up welllednetworkhq.com mailboxes, stops when someone replies, and handles unsubscribes itself.

Each lead carries its own details as custom fields, so the email can name the service, the report date
and link to that service's free Report Review:
    {{first_name}} {{service_name}} {{rating}} {{report_date}} {{weak_areas}} {{review_url}} {{video_url}}

Settings on Render (wln-secrets):
    SALESBLINK_API_KEY         required; without it this module quietly does nothing
    SALESBLINK_RADAR_LIST_ID   the list UUID (falls back to the list created on 18 Oct 2026)
    RADAR_SB_MATCH             which services qualify (regex, default "supported living")
    RADAR_SB_ALLOW_RISKY       "1" to also add addresses Clearout could not fully confirm (default off)
"""
from __future__ import annotations

import json
import os
import re

import requests

API = "https://run.salesblink.io/api/public/v1.0.0"
DEFAULT_LIST_ID = "ad2de090-00ce-4b8a-8bac-14d9f00f21de"  # SalesBlink list "CQC Radar - Supported Living"
LAST_ERROR = [""]


def enabled() -> bool:
    return bool(os.getenv("SALESBLINK_API_KEY") and list_id())


def list_id() -> str:
    return os.getenv("SALESBLINK_RADAR_LIST_ID", "").strip() or DEFAULT_LIST_ID


def _match_rx():
    return re.compile(os.getenv("RADAR_SB_MATCH", "supported living"), re.I)


def is_target(p: dict) -> bool:
    """True for the services this email avenue is for (supported living by default)."""
    text = " ".join(str(p.get(k, "")) for k in ("service_type", "location_name", "provider_name"))
    return bool(_match_rx().search(text))


def _post(path: str, body: dict) -> dict:
    r = requests.post(API + path, timeout=60, data=json.dumps(body),
                      headers={"Authorization": os.getenv("SALESBLINK_API_KEY", ""), "Content-Type": "application/json",
                               "Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"SalesBlink {path} {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


def eligible(p: dict, suppressed: set) -> tuple[bool, str]:
    """Whether this prospect may go into the cold email sequence, and if not, why not."""
    if not is_target(p):
        return False, "not supported living"
    if not p.get("email"):
        return False, "no email found"
    if not p.get("can_email"):
        return False, "no company number (call or write instead)"
    check = p.get("email_check", "")
    if check != "safe" and not (check == "risky" and os.getenv("RADAR_SB_ALLOW_RISKY") == "1"):
        return False, f"email not confirmed by Clearout ({check or 'unchecked'})"
    if p["email"].lower() in suppressed:
        return False, "asked not to be contacted"
    if p.get("large_org"):
        return False, "large organisation or council"
    return True, ""


def _contact(p: dict) -> dict:
    name = (p.get("registered_manager") or "").strip()
    first, _, last = name.partition(" ")
    weak = [k.split(":")[0] for k in p.get("weak_key_questions") or []]
    return {
        "email": p["email"].lower(),
        "first_name": first or "there",
        "last_name": last,
        "phone": p.get("phone", ""),
        "company_name": p.get("provider_name") or p.get("location_name", ""),
        "job_title": "Registered Manager" if name else "",
        "service_name": p.get("location_name", ""),
        "rating": p.get("rating", ""),
        "report_date": p.get("report_date", ""),
        "weak_areas": ", ".join(weak),
        "town": p.get("town", ""),
        "review_url": p.get("review_url", ""),
        "video_url": p.get("video_url", ""),
        "cqc_page": p.get("cqc_page", ""),
        "radar_code": p.get("code", ""),
        "source": "CQC report radar",
    }


def add_radar_prospects(prospects: list[dict], store) -> str:
    """Add qualifying radar prospects to the SalesBlink list. Marks each one so it is never added twice.
    Returns a short Telegram summary ('' when switched off or nothing to report)."""
    if not enabled():
        return ""
    suppressed = set(store.suppressed())
    to_add, added_codes, skipped = [], [], {}
    for p in prospects:
        key = f"sb:radar:{p.get('location_id')}"
        if store.has(key):
            continue
        ok, why = eligible(p, suppressed)
        if not ok:
            if why != "not supported living":
                skipped[why] = skipped.get(why, 0) + 1
            continue
        to_add.append((p, key))
    if not to_add and not skipped:
        return ""
    added = 0
    if to_add:
        try:
            _post("/contacts", {"list_id": list_id(), "contacts": [_contact(p) for p, _ in to_add],
                                "remove_duplicates": True})
            added = len(to_add)
            store.add_many([k for _, k in to_add])
            for p, _ in to_add:
                p["salesblink"] = True
                store.put_prospect(p)
                added_codes.append(f"{p.get('code', '')} {p.get('location_name', '')} ({p['email']})")
        except Exception as exc:
            LAST_ERROR[0] = str(exc)[:300]
    msg = f"Email (SalesBlink): added {added} supported living service{'s' if added != 1 else ''} to the CQC Radar sequence."
    if added:
        msg += " They get the approved emails automatically, a few days after the LinkedIn invitation. Do not email these by hand.\n"
        msg += "\n".join(added_codes)
    if skipped:
        msg += "\nNot added: " + "; ".join(f"{v} {k}" for k, v in skipped.items())
    if LAST_ERROR[0] and not added and to_add:
        msg += f"\nSalesBlink error: {LAST_ERROR[0]}"
    return msg
