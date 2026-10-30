"""SalesBlink (cold email) side of the CQC report radar.

Each morning, radar prospects that are supported living services with a new Requires improvement or
Inadequate report, a company number (UK PECR: limited companies only) and an email address that passed
the Clearout check are added to the SalesBlink "CQC Radar - Supported Living" list.

That list feeds an evergreen SalesBlink sequence which Yeukai approved once. The sequence opens with a
wait of a few days, so the email lands after the LinkedIn invitation sent by Sendpilot. It sends from the
warmed-up welllednetworkhq.com mailboxes, stops when someone replies, and handles unsubscribes itself.

Each lead carries its own details as custom fields, so the email can name the service, the report date
and link to that service's free Report Review:
    {{first_name}} {{service_name}} {{rating}} {{report_date}} {{weak_areas}} {{review_url}} {{guide_url}} {{video_url}}

Settings on Render (wln-secrets):
    SALESBLINK_API_KEY         required; without it this module quietly does nothing
    SALESBLINK_RADAR_LIST_ID   the list UUID (falls back to the list created on 18 Oct 2026)
    RADAR_SB_MATCH             which services qualify (regex; default "." = every adult social care service.
                               Widened from "supported living" on 30 Oct 2026: almost no radar finds qualified)
    RADAR_SB_ALLOW_RISKY       "1" to add any address Clearout could not fully confirm. Without it, a "risky"
                               address is still added when it was published on the service's own website
                               (catch-all inboxes at small providers are real, just unconfirmable)
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

API = "https://run.salesblink.io/api/public/v1.0.0"
DEFAULT_LIST_ID = "ad2de090-00ce-4b8a-8bac-14d9f00f21de"  # SalesBlink list "CQC Radar - Supported Living"
LAST_ERROR = [""]
# Well-Led Network blog guide matched to the rating, used in email 1 as {{guide_url}} (tracked Shorby links)
GUIDES = {
    "Requires improvement": "https://shor.by/ri-to-good-guide",
    "Inadequate": "https://shor.by/inadequate-guide",
}


def enabled() -> bool:
    return bool(os.getenv("SALESBLINK_API_KEY") and list_id())


def list_id() -> str:
    return os.getenv("SALESBLINK_RADAR_LIST_ID", "").strip() or DEFAULT_LIST_ID


def _match_rx():
    return re.compile(os.getenv("RADAR_SB_MATCH", "."), re.I)


def is_target(p: dict) -> bool:
    """True for the services this email avenue is for (every service by default)."""
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
        return False, "outside RADAR_SB_MATCH"
    if not p.get("email"):
        return False, "no email found"
    if not p.get("can_email"):
        return False, "no company number (call or write instead)"
    check = p.get("email_check", "")
    risky_ok = check == "risky" and (os.getenv("RADAR_SB_ALLOW_RISKY") == "1"
                                     or p.get("email_source") == "service website")
    if check != "safe" and not risky_ok:
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
        "guide_url": GUIDES.get(p.get("rating", ""), GUIDES["Requires improvement"]),
        "video_url": p.get("video_url", ""),
        "cqc_page": p.get("cqc_page", ""),
        "radar_code": p.get("code", ""),
        "source": "CQC report radar",
    }


DELAY_DAYS = float(os.getenv("RADAR_SB_DELAY_DAYS", "3"))  # LinkedIn invitation goes first


def add_radar_prospects(prospects: list[dict], store) -> str:
    """Queue qualifying radar prospects for SalesBlink. They are added to the list DELAY_DAYS later
    (by flush_due, run every morning), so the email lands after the Sendpilot LinkedIn invitation.
    Returns a short Telegram summary ('' when switched off or nothing to report)."""
    if not enabled():
        return ""
    suppressed = set(store.suppressed())
    queued, skipped = [], {}
    now = time.time()
    for p in prospects:
        key = f"sb:radar:{p.get('location_id')}"
        if store.has(key):
            continue
        ok, why = eligible(p, suppressed)
        if not ok:
            if why != "outside RADAR_SB_MATCH":
                skipped[why] = skipped.get(why, 0) + 1
            continue
        p["sb_queued_at"] = now
        p["sb_due"] = time.strftime("%d %B", time.localtime(now + DELAY_DAYS * 86400))
        store.add_many([key])
        store.put_prospect(p)
        queued.append(f"{p.get('code', '')} {p.get('location_name', '')} ({p['email']})")
    if not queued and not skipped:
        return ""
    msg = (f"Email (SalesBlink): {len(queued)} service{'s' if len(queued) != 1 else ''} queued for the "
           f"CQC Radar sequence. They join it in {DELAY_DAYS:g} days, after the LinkedIn invitation, and then get the "
           "approved emails automatically. Do not email these by hand.")
    if queued:
        msg += "\n" + "\n".join(queued)
    if skipped:
        msg += "\nNot added: " + "; ".join(f"{v} {k}" for k, v in skipped.items())
    return msg


def flush_due(store) -> str:
    """Every morning: add queued prospects whose wait is over to the SalesBlink list."""
    if not enabled():
        return ""
    suppressed = set(store.suppressed())
    cutoff = time.time() - DELAY_DAYS * 86400 + 3600  # an hour's grace so the morning run never slips a day
    due = [p for p in store.prospects()
           if p.get("sb_queued_at") and not p.get("salesblink") and p["sb_queued_at"] <= cutoff]
    due = [p for p in due if (p.get("email") or "").lower() not in suppressed]
    if not due:
        return ""
    try:
        for start in range(0, len(due), 500):
            batch = due[start:start + 500]
            _post("/contacts", {"list_id": list_id(), "contacts": [_contact(p) for p in batch], "remove_duplicates": True})
    except Exception as exc:
        LAST_ERROR[0] = str(exc)[:300]
        return f"SalesBlink: could not add {len(due)} queued radar prospects today, will retry tomorrow. Error: {LAST_ERROR[0]}"
    for p in due:
        p["salesblink"] = True
        store.put_prospect(p)
    return (f"SalesBlink: {len(due)} radar prospect{'s' if len(due) != 1 else ''} joined the CQC Radar sequence today:\n"
            + "\n".join(f"{p.get('code', '')} {p.get('location_name', '')} ({p['email']})" for p in due))
