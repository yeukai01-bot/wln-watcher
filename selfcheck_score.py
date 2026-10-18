"""Well-led Self-Check score page -> Telegram alert (+ Brevo fields).

The page https://www.welllednetwork.com/self-check-score posts here (POST /selfcheck/score) when a
reader tells Yeukai their Self-Check total. This module:
  1. sends a Telegram alert with the band, name, email, service and whether they want a call,
     plus the suggested next step (0-12 = call them within 24 hours);
  2. if the reader is already a Brevo contact (they got the Self-Check from Brevo), saves their
     answers on the contact as SELFCHECK_BAND, SELFCHECK_SCORE, SELFCHECK_SERVICE, SELFCHECK_CALL.
     Nobody new is added to Brevo here: the page is not a sign-up form.
The same email and band is only alerted once a day, so a double click never sends two alerts.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
BREVO = "https://api.brevo.com/v3"
ATTRS = ("SELFCHECK_BAND", "SELFCHECK_SCORE", "SELFCHECK_SERVICE", "SELFCHECK_CALL", "SELFCHECK_AT")
BANDS = {
    "0-12": ("AT RISK", "Call or email within 24 hours and offer the free strategy call: https://shor.by/strategy-call"),
    "13-19": ("GAPS SHOWING", "Personal reply this week. Point them to the Governance Fix (https://shor.by/governance-fix) "
                              "or the Accelerator enrolment call (https://shor.by/enrolment-call)."),
    "20-24": ("STRONG", "No action needed. They stay on the welcome emails and were pointed to the WhatsApp group."),
}
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _clean(v, n=120) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _brevo(method: str, path: str, body: dict | None = None):
    key = os.getenv("BREVO_API_KEY", "").strip()
    return requests.request(method, f"{BREVO}{path}", json=body, timeout=20,
                            headers={"api-key": key, "accept": "application/json", "content-type": "application/json"})


def _save_to_brevo(store, d: dict) -> str:
    if not os.getenv("BREVO_API_KEY", "").strip():
        return "not saved (BREVO_API_KEY is not set in Render)"
    try:
        if not (store.r and store.r.get("wln:selfcheck_attrs")):
            for a in ATTRS:  # 400 just means the field already exists
                _brevo("POST", f"/contacts/attributes/normal/{a}", {"type": "text"})
            if store.r:
                store.r.set("wln:selfcheck_attrs", "1")
        r = _brevo("PUT", f"/contacts/{requests.utils.quote(d['email'])}", {"attributes": {
            "SELFCHECK_BAND": d["band"], "SELFCHECK_SCORE": d["score"], "SELFCHECK_SERVICE": d["service"],
            "SELFCHECK_CALL": d["wants_call"], "SELFCHECK_AT": d["at"]}})
        if r.status_code in (200, 204):
            return "saved on their Brevo contact"
        if r.status_code == 404:
            return "not on the Brevo Self-Check list (used a different email)"
        return f"not saved (Brevo said {r.status_code})"
    except Exception as exc:
        return f"not saved (Brevo error: {str(exc)[:120]})"


def handle(store, payload, send) -> dict:
    p = payload if isinstance(payload, dict) else {}
    band = _clean(p.get("band"), 8)
    email = _clean(p.get("email"), 160).lower()
    if band not in BANDS or not EMAIL.match(email):
        return {"ok": False, "error": "band and a valid email are needed"}
    d = {
        "band": band, "email": email,
        "first_name": _clean(p.get("first_name"), 60) or "(no name)",
        "service": _clean(p.get("service"), 60) or "not given",
        "score": _clean(p.get("score"), 4) if re.fullmatch(r"\d{1,2}", _clean(p.get("score"), 4)) else "",
        "wants_call": _clean(p.get("wants_call"), 20) or "not answered",
        "at": datetime.now(UK).strftime("%d %b %Y %H:%M"),
    }
    key = f"wln:selfcheck_seen:{email}:{band}:{datetime.now(UK):%Y%m%d}"
    if store.r and not store.r.set(key, "1", nx=True, ex=86400):
        return {"ok": True, "skipped": "already alerted today"}
    d["brevo"] = _save_to_brevo(store, d)
    label, action = BANDS[band]
    call = d["wants_call"]
    send(
        f"Self-Check score: {label} ({band.replace('-', ' to ')})\n"
        f"{d['first_name']}, {email}\n"
        f"Exact score: {d['score'] or 'not given'}\n"
        f"Service: {d['service']}\n"
        f"Wants a free call: {call}" + ("  <- they asked for a call" if call == "yes" else "") + "\n"
        f"Brevo: {d['brevo']}\n\n"
        f"Next step: {action}\n"
        f"Received {d['at']} (UK)"
    )
    if store.r:
        store.r.lpush("wln:selfcheck_scores", f"{d['at']} | {band} | {email} | call {call}")
        store.r.ltrim("wln:selfcheck_scores", 0, 499)
    return {"ok": True}
