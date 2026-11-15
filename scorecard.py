"""In-page scorecard for the free CQC Interview Ready practice (Well-Led Network lesson 3367).

When the call ends, the lesson page asks /tt/scorecard for that session's report and shows it on the
same page (no new tab). The Tough Tongue key stays here in Render (TT_API_KEY); the page never sees it.

Safeguards
- Only the Fit Person Interview Taster scenario is served.
- The caller must give the session id AND the email the session was run under (the member's own
  Well-Led Network email, passed to Tough Tongue by the lesson). If they do not match, nothing is returned.
- Only the debrief (overview, what went well, what to improve, detailed feedback) and simple stats are
  returned. No transcript, recording or contact details.
- "Email this report" sends only to that same email, at most 3 times per session, through Brevo.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

FIT_PERSON_TASTER = "6ac76618dffaab401d2369ab"
FIT_PERSON_FULL = "6ac7671d8a363fb6f5a1c4f8"   # paid 45-minute mock (Essentials, Plus, VIP)
ALLOWED_SCENARIOS = {FIT_PERSON_TASTER, FIT_PERSON_FULL}
SESSION_ID = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
MAX_AGE_DAYS = 7
EMAILS_PER_SESSION = 3
CHECKOUTS = [
    ("Essentials", "£97", "https://www.welllednetwork.com/paywall/487/checkout"),
    ("Plus", "£247", "https://www.welllednetwork.com/paywall/488/checkout"),
    ("VIP", "£497", "https://www.welllednetwork.com/paywall/489/checkout"),
]
WHATSAPP = "https://wa.me/447907904199"
ALLOWED_TAGS = {"h3", "h4", "p", "ul", "ol", "li", "strong", "b", "em", "i", "br"}


def _get_session(sid: str) -> dict:
    import practice_rooms  # reuses the existing Tough Tongue call (key + workspace header)
    return practice_rooms._tt(f"/sessions/{sid}", {"include_recording_url": "false"})


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


SECTIONS = [("overall_score", "Overview"), ("strengths", "What went well"), ("weaknesses", "What could be improved")]
SKIP_KEYS = {"final_score", "score", "scores", "percentage", "status", "model", "version", "created_at", "updated_at"}


def _find_debrief(s: dict) -> str:
    """The written debrief Tough Tongue produced (the same text as the 'Analysis Report' email).

    Tough Tongue keeps it in evaluation_results as separate markdown fields (overall_score, strengths,
    weaknesses, then the detailed feedback). Older or other shapes: fall back to one long text block."""
    ev = s.get("evaluation_results") or {}
    if isinstance(ev, dict) and any(isinstance(ev.get(k), str) and ev.get(k).strip() for k, _ in SECTIONS):
        parts = []
        for k, title in SECTIONS:
            v = ev.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(f"### {title}\n{v.strip()}")
        for k, v in ev.items():
            if k in dict(SECTIONS) or k in SKIP_KEYS or not isinstance(v, str) or len(v.strip()) < 40:
                continue
            title = "Detailed Feedback" if "detail" in k or "feedback" in k else k.replace("_", " ").capitalize()
            body = v.strip()
            if title == "Detailed Feedback" or not body.lstrip().startswith(("#", "<h3")):
                body = f"### {title}\n{body}"
            parts.append(body)
        return "\n\n".join(parts)
    best = ""
    for t in _strings(s):
        if ("Overview" in t or "Detailed Feedback" in t) and ("What went well" in t or "Detailed Feedback" in t):
            if len(t) > len(best):
                best = t
    return best


def _md_to_html(t: str) -> str:
    out, in_list = [], False
    for line in t.splitlines():
        line = line.rstrip()
        m = re.match(r"^#{1,4}\s+(.*)", line)
        b = re.match(r"^\s*[-*•]\s+(.*)", line)
        if b:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(b.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if m:
            out.append(f"<h3>{html.escape(m.group(1))}</h3>")
        elif line.strip():
            out.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    h = "\n".join(out)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)


def _clean(raw: str) -> str:
    """Keep only simple formatting; drop links, the full-report link and the lines we show ourselves."""
    from bs4 import BeautifulSoup

    if "<" not in raw:
        raw = _md_to_html(raw)
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(True):
        if tag.name in ("script", "style", "iframe", "img"):
            tag.decompose()
            continue
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        tag.attrs = {}
    text = str(soup)
    # Remove sentences the page shows as buttons instead (WhatsApp tip, full practice offer, full-report link)
    text = re.sub(r"(<li>|<p>)?\s*Message Yeukai your score on WhatsApp[^<]*(</li>|</p>|<br/?>)?", "", text)
    text = re.sub(r"(<li>|<p>)?\s*Want to be fully interview-ready\?[^<]*(</li>|</p>|<br/?>)?", "", text)
    text = re.sub(r"Open the full report:?\s*\S*", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"<(p|li)>\s*</\1>", "", text)
    text = re.sub(r"(<br/?>\s*){2,}", "<br/>", text)
    return text.strip()


def _when(s: dict):
    for k in ("completed_at", "ended_at", "updated_at", "created_at"):
        v = s.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _check(sid: str, email: str):
    """Returns (session, error_code). error_code is None when the caller may see this session."""
    if not SESSION_ID.match(sid or "") or not EMAIL.match(email or ""):
        return None, "bad_request"
    try:
        s = _get_session(sid)
    except requests.HTTPError as exc:
        code = getattr(exc.response, "status_code", 0)
        return None, "pending" if code in (404, 409, 425) else "unavailable"
    except Exception:
        return None, "unavailable"
    s = s.get("data", s) if isinstance(s, dict) else {}
    if str(s.get("scenario_id") or (s.get("scenario") or {}).get("id") or "") not in ALLOWED_SCENARIOS:
        return None, "not_found"
    owner = (s.get("user_email") or (s.get("user") or {}).get("email") or "").strip().lower()
    if not owner or owner != email.strip().lower():
        return None, "not_found"
    when = _when(s)
    if when and when < datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS):
        return None, "not_found"
    return s, None


def get(sid: str, email: str) -> tuple[dict, int]:
    s, err = _check(sid, email)
    if err == "bad_request":
        return {"status": "error"}, 400
    if err == "not_found":
        return {"status": "not_found"}, 404
    if err:
        return {"status": err}, 200
    raw = _find_debrief(s)
    if not raw:
        return {"status": "pending"}, 200
    body = _clean(raw)
    dur = s.get("duration") or s.get("duration_seconds") or s.get("call_duration")
    try:
        minutes = round(float(dur) / 60) if dur and float(dur) > 90 else (int(dur) if dur else None)
    except (TypeError, ValueError):
        minutes = None
    if not minutes:
        try:
            a = datetime.fromisoformat(str(s.get("created_at")).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(s.get("completed_at")).replace("Z", "+00:00"))
            minutes = max(1, round((b - a).total_seconds() / 60))
        except (TypeError, ValueError):
            minutes = None
    m = re.search(r"<strong>([^<]{3,120})</strong>", body)
    when = _when(s)
    return {
        "status": "ready",
        "html": body,
        "stats": {
            "headline": m.group(1).strip() if m else "",
            "minutes": minutes,
            "completed": when.strftime("%-d %b %Y, %H:%M") if when else "",
            "questions": 5 if str(s.get("scenario_id")) == FIT_PERSON_TASTER else None,
            "full": str(s.get("scenario_id")) == FIT_PERSON_FULL,
        },
    }, 200


def _email_html(name: str, body: str, stats: dict) -> str:
    buttons = ('<a href="https://www.welllednetwork.com/cqc-interview-ready" style="display:inline-block;background:#d4a72c;'
               'color:#1f3a5f;font-weight:bold;padding:11px 16px;border-radius:7px;text-decoration:none">See the CQC Interview Ready options</a>')
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;color:#1c2733">
<div style="background:#1f3a5f;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0">
<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#d4a72c;font-weight:bold">Well-Led Network</div>
<div style="font-size:21px;font-weight:bold;margin-top:4px">Your CQC fit person practice scorecard</div>
<div style="font-size:13px;color:#dfe6ef;margin-top:4px">{html.escape(stats.get('completed') or '')}</div></div>
<div style="border:1px solid #e3e7ee;border-top:0;padding:20px 22px;border-radius:0 0 10px 10px;line-height:1.55;font-size:15px">
<p>Hi {html.escape(name or 'there')},</p><p>Here is the report from your practice interview, as promised.</p>
{body}
<p style="margin-top:18px"><a href="{WHATSAPP}" style="color:#1f3a5f;font-weight:bold">Message Yeukai your score on WhatsApp</a> for one personal tip on your weakest area.</p>
<div style="margin-top:20px;padding:18px;background:#f6f1e6;border-radius:10px">
<div style="font-weight:bold;font-size:17px;margin-bottom:6px">Your next step: CQC Interview Ready</div>
<div style="margin-bottom:12px">The full 45-minute practice interview, model answers and a written report. Plus and VIP add personal support from Yeukai.</div>
{buttons}</div>
<p style="font-size:12px;color:#6b7684;margin-top:18px">This is practice feedback, not a CQC decision. Practice questions are not CQC's own questions.</p>
<p style="font-size:13px;margin-top:16px">Kind regards<br/>Yeukai Kajidori<br/>Founder, Well-Led Network<br/>Creator of the Well-Led and Safe Supported Living Framework<br/>
<a href="https://www.welllednetwork.com/blog">www.welllednetwork.com/blog</a><br/>Work with me: <a href="https://shor.by/wln-work-with-me">https://shor.by/wln-work-with-me</a><br/>The Kajidori Collective Ltd</p>
</div></div>"""


def email(store, sid: str, addr: str, name: str = "") -> tuple[dict, int]:
    import briefing

    s, err = _check(sid, addr)
    if err:
        return {"ok": False, "status": err}, 404 if err in ("not_found", "bad_request") else 200
    res, _ = get(sid, addr)
    if res.get("status") != "ready":
        return {"ok": False, "status": res.get("status")}, 200
    key = f"wln:scorecard_emails:{sid}"
    if store.r:
        n = store.r.incr(key)
        store.r.expire(key, 60 * 60 * 24 * 14)
        if n > EMAILS_PER_SESSION:
            return {"ok": False, "status": "limit"}, 200
    if not briefing._key():
        return {"ok": False, "status": "unavailable"}, 200
    r = briefing._brevo("POST", "/smtp/email", {
        "sender": briefing.SENDER, "replyTo": {"email": briefing.REPLY_TO},
        "to": [{"email": addr, "name": name or addr}],
        "subject": ("Your CQC fit person interview report" if res["stats"].get("full") else "Your CQC fit person practice scorecard"),
        "htmlContent": _email_html(name, res["html"], res["stats"]),
        "tags": ["cqc-ir-scorecard"],
    })
    if r.status_code >= 300:
        return {"ok": False, "status": "unavailable"}, 200
    return {"ok": True}, 200
