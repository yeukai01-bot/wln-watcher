"""Weekly Well-Led Briefing, sent through Brevo so it never depends on a computer being on.

Monday (morning cron): pick the next article from ROTATION, read it, have Claude write the briefing
from that article only, build the branded email, create a Brevo campaign to the "WLN Members" list
scheduled for Tuesday 07:30 UK, and send Yeukai the full text on Telegram with a preview link.
If he does nothing, Brevo sends it on Tuesday. Telegram replies:
  briefing          what is scheduled, to how many people
  briefing hold     stops this week's send (Brevo campaign suspended)
  briefing go       puts a held briefing back on (or sends now if Tuesday 07:30 has passed)
  briefing setup    the member sync link to paste into the Well-Led Network webhooks

Members reach the Brevo list two ways: a one-off import of existing members, then the Well-Led
Network "New signup" webhook for everyone who joins later (see web.py /wln/members/<token>).
Brevo adds the unsubscribe link and honours unsubscribes on every send.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
BREVO = "https://api.brevo.com/v3"
LIST_NAME = "WLN Members"
FOLDER = "Well-Led Network"
SENDER = {"name": "Yeukai Kajidori", "email": os.getenv("BRIEFING_SENDER_EMAIL", "support@welllednetwork.com")}
REPLY_TO = "kajidoricollective@gmail.com"
# The Well-Led Network itself sends the briefings already scheduled up to 1 Dec 2026, so Brevo starts after.
START = os.getenv("BRIEFING_START", "2026-12-07")
TEST_TO = os.getenv("BRIEFING_TEST_TO", "kajidoricollective@gmail.com")

K_LIST = "wln:brief:list_id"
K_USED = "wln:brief:used"
K_WEEK = "wln:brief:week:"  # + Monday date, JSON of this week's briefing
K_LAST = "wln:brief:last_week"
K_RAW = "wln:brief:last_member_raw"

SIGNATURE_HTML = (
    "<p style=\"margin:24px 0 0\">Kind regards<br><strong>Yeukai Kajidori</strong><br>Founder, Well-Led Network<br>"
    "Creator of the Well-Led and Safe Supported Living Framework<br>"
    "<a href=\"https://www.welllednetwork.com/blog\">www.welllednetwork.com/blog</a><br>"
    "Work with me: <a href=\"https://shor.by/wln-work-with-me\">https://shor.by/wln-work-with-me</a><br>"
    "The Kajidori Collective Ltd</p>"
)
WHATSAPP_PS = ("P.S. Want quick answers to your CQC questions? Join the Well-Led Network, my free WhatsApp group "
               "for UK care leaders. Managers post their questions and I answer them there: https://shor.by/wln-whatsapp")

# Main article for each week, in order. Each is read live on the Monday; if a page cannot be read the
# next one is used and Yeukai is told. Already sent through the Well-Led Network: risk register,
# late calls, audits missed, continuity plan (10 Nov to 1 Dec 2026), so they are not repeated here.
ROTATION = [
    {"key": "fit-person-interview", "url": "https://shor.by/fit-person-interview"},
    {"key": "care-home-audits", "url": "https://shor.by/care-home-audits"},
    {"key": "prepare-staff-cqc-interviews", "url": "https://www.welllednetwork.com/blog/prepare-care-staff-for-cqc-interviews"},
    {"key": "delegated-tasks", "url": "https://shor.by/wln-delegated-tasks"},
    {"key": "find-clients", "url": "https://shor.by/find-clients"},
    {"key": "ri-to-good", "url": "https://www.welllednetwork.com/blog/requires-improvement-to-good-programme"},
    {"key": "oliver-mcgowan", "url": "https://shor.by/oliver-mcgowan"},
    {"key": "govern-ai", "url": "https://www.welllednetwork.com/blog/how-care-providers-govern-ai"},
    {"key": "leaving-rm", "url": "https://shor.by/leaving-rm"},
    {"key": "switching-software", "url": "https://shor.by/switching-care-software"},
]

# Free tools and next steps the "Also this week" box may point to. Links are fixed here, never written by AI.
LIBRARY = {
    "scorecard": ("Free CQC Inspection Readiness Scorecard", "https://shor.by/readiness-scorecard", "Get the scorecard"),
    "risk-register": ("Free Home Care Operational Risk Register", "https://shor.by/risk-register-tool", "Get the register"),
    "late-calls-log": ("Free Late and Missed Call Log and Review", "https://shor.by/late-calls-log", "Get the log"),
    "well-led-practice": ("Free 10-minute well-led practice interview", "https://shor.by/well-led-practice", "Try it now"),
    "fit-person-practice": ("Free fit person practice interview", "https://shor.by/fit-person-practice", "Try it now"),
    "mock-inspection": ("Free mock inspection interview", "https://shor.by/free-mock-inspection", "Start it"),
    "readiness-replay": ("Free replay: if CQC inspected tomorrow, could you prove you're ready?", "https://shor.by/readiness-replay", "Watch the replay"),
    "om-tracker": ("Free Oliver McGowan training tracker", "https://shor.by/om-tracker", "Get the tracker"),
    "delegation-pack": ("Free delegated healthcare tasks pack", "https://shor.by/wln-delegation-pack", "Get the pack"),
    "first-10-packages": ("Free plan for winning your first care packages", "https://shor.by/first-10-packages", "Get the plan"),
    "continuity-plan": ("A continuity plan someone can use at four in the morning", "https://shor.by/continuity-plan", "Read the guide"),
    "whatsapp": ("Ask other care leaders", "https://shor.by/wln-whatsapp", "Join the WhatsApp group"),
}

PROOF = """True lines about Yeukai you may use (use at most one, word for word or lightly trimmed; never invent others):
- I spent 15 years as a CQC Registered Manager.
- I ran Home Angels, a homecare agency with two branches and around 3,000 hours of care a week, rated Good.
- I led Support Horizons to local authority Preferred Provider status with zero deficiencies in the annual quality review.
- I grew PLS, a mental health supported living provider, from one service to five in two years.
- I led a failing service from "Embargo" to "Green" status with the local authority."""

SYSTEM = f"""You write the Weekly Well-Led Briefing, a short email from Yeukai Kajidori to members of the Well-Led
Network (UK care Registered Managers, Nominated Individuals and owners, mostly adult social care in England).
Write in his voice: warm, calm, practical, British English, short sentences. Never use dashes as punctuation.

Each briefing helps the reader do one thing this week, taken ONLY from the article text you are given.
Never add facts, figures, regulations or dates that are not in the article. Never state prices.
Never name a day, date or time for any clinic, call or event. No hype, no shaming, no swearing.

Shape:
- subject: under 60 characters, names the reader's problem or outcome.
- preheader: one sentence, under 110 characters.
- headline: under 70 characters, the one thing this email helps them do.
- hook: 2 or 3 sentences. Start with "You know those [role] who [enviable result]?" then "It isn't X, and it isn't Y."
  then what they really do.
- context: 1 or 2 sentences on why it matters, from the article.
- steps: exactly 3 practical steps, each a short bold title and one or two sentences of how to do it.
- closing: 1 or 2 sentences; may include one proof line from the list below.
- also: pick exactly 2 ids from the LIBRARY that fit this topic best, each with one line (under 110 characters)
  on why it helps. Never pick the same thing as the main article.

{PROOF}

Reply with JSON only:
{{"subject": "", "preheader": "", "headline": "", "hook": "", "context": "", "steps": [{{"title": "", "text": ""}}],
 "closing": "", "also": [{{"id": "", "line": ""}}]}}"""


# ---------------------------------------------------------------- Brevo
def _key() -> str:
    return os.getenv("BREVO_API_KEY", "").strip()


def _brevo(method: str, path: str, body: dict | None = None) -> requests.Response:
    r = requests.request(method, f"{BREVO}{path}", json=body, timeout=40,
                         headers={"api-key": _key(), "accept": "application/json", "content-type": "application/json"})
    if r.status_code == 401:
        raise PermissionError("the Brevo API key in Render was rejected")
    return r


def list_id(store) -> int:
    """The Brevo list 'WLN Members' (in the 'Well-Led Network' folder), created on first use."""
    if store.r:
        cached = store.r.get(K_LIST)
        if cached:
            return int(cached)
    lid, offset = None, 0
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
        fid = next((f["id"] for f in r.json().get("folders") or [] if f.get("name") == FOLDER), None)
        if fid is None:
            r = _brevo("POST", "/contacts/folders", {"name": FOLDER})
            r.raise_for_status()
            fid = r.json()["id"]
        r = _brevo("POST", "/contacts/lists", {"name": LIST_NAME, "folderId": fid})
        r.raise_for_status()
        lid = r.json()["id"]
    if store.r:
        store.r.set(K_LIST, str(lid))
    return int(lid)


def list_count(store) -> int | None:
    try:
        r = _brevo("GET", f"/contacts/lists/{list_id(store)}")
        return int(r.json().get("uniqueSubscribers") or r.json().get("totalSubscribers") or 0) if r.ok else None
    except Exception:
        return None


# ---------------------------------------------------------------- members
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _find(d, names: tuple[str, ...]):
    """First non-empty value whose key matches one of names, searching nested dicts."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() in names and isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        for v in d.values():
            got = _find(v, names)
            if got:
                return got
    elif isinstance(d, list):
        for v in d:
            got = _find(v, names)
            if got:
                return got
    return ""


def parse_member(payload) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("member"), dict):  # Well-Led Network webhook shape
        payload = payload["member"]
    email = _find(payload, ("email", "user_email", "useremail", "member_email")).lower()
    first = _find(payload, ("first_name", "firstname", "firstName".lower(), "fname"))
    last = _find(payload, ("last_name", "lastname", "lname"))
    if not first:
        full = _find(payload, ("name", "full_name", "fullname", "display_name"))
        first, _, rest = full.partition(" ")
        last = last or rest
    return {"email": email, "first": first, "last": last}


def add_member(store, m: dict) -> str:
    if not EMAIL_RE.match(m.get("email") or ""):
        return "skipped (no usable email)"
    if not _key():
        return "not added (BREVO_API_KEY is not set in Render)"
    r = _brevo("POST", "/contacts", {"email": m["email"], "updateEnabled": True, "listIds": [list_id(store)],
                                     "attributes": {"FIRSTNAME": m.get("first", ""), "LASTNAME": m.get("last", "")}})
    return "added" if r.status_code in (200, 201, 204) else f"not added ({r.status_code}: {r.text[:120]})"


def remove_member(store, email: str) -> str:
    if not EMAIL_RE.match(email or ""):
        return "skipped"
    r = _brevo("POST", f"/contacts/lists/{list_id(store)}/contacts/remove", {"emails": [email]})
    return "removed" if r.ok else f"not removed ({r.status_code})"


def on_member_event(store, event: str, payload, send) -> dict:
    """Well-Led Network webhook: new sign-ups join the list; deleted or suspended accounts leave it."""
    if store.r:
        store.r.set(K_RAW, json.dumps({"event": event, "at": datetime.now(UK).isoformat(), "payload": payload})[:20000])
    if isinstance(payload, dict) and isinstance(payload.get("members"), list):  # one-off bulk import
        results = [add_member(store, parse_member(m)) for m in payload["members"]]
        added = sum(1 for x in results if x == "added")
        send(f"Weekly briefing list: imported {added} of {len(results)} members into Brevo ({LIST_NAME}).")
        return {"ok": True, "added": added, "total": len(results)}
    m = parse_member(payload)
    ev = (event or _find(payload, ("event", "type", "event_type")) or "").lower()
    if any(w in ev for w in ("delete", "removed", "suspend")):
        return {"ok": True, "result": remove_member(store, m["email"])}
    return {"ok": True, "result": add_member(store, m)}


def member_token() -> str:
    return hmac.new(os.getenv("WEBHOOK_SECRET", "").encode(), b"wln-members", hashlib.sha256).hexdigest()[:24]


def preview_token(week: str) -> str:
    return hmac.new(os.getenv("WEBHOOK_SECRET", "").encode(), f"briefing:{week}".encode(), hashlib.sha256).hexdigest()[:20]


# ---------------------------------------------------------------- writing
def write(article_text: str, article_url: str) -> dict:
    import ai

    lib = "\n".join(f"- {k}: {v[0]}" for k, v in LIBRARY.items())
    user = (f"LIBRARY ids you may pick for 'also':\n{lib}\n\nMain article link (the button goes here): {article_url}\n\n"
            f"ARTICLE TEXT:\n{article_text[:14000]}")
    data = ai._json(ai._ask(SYSTEM, user, max_tokens=3000))
    steps = [s for s in data.get("steps") or [] if s.get("title") and s.get("text")][:3]
    also = [a for a in data.get("also") or [] if a.get("id") in LIBRARY][:2]
    if len(steps) != 3 or not data.get("subject") or not data.get("headline") or len(also) < 1:
        raise ValueError("the briefing came back incomplete")
    for f in ("subject", "preheader", "headline", "hook", "context", "closing"):
        data[f] = re.sub(r"\s+[–—-]\s+", ", ", (data.get(f) or "").strip())
    data["steps"], data["also"] = steps, also
    return data


def render(b: dict, article_url: str) -> str:
    e = html.escape
    steps = "".join(f"<li style=\"margin:0 0 10px\"><strong>{e(s['title'])}</strong> {e(s['text'])}</li>" for s in b["steps"])
    also = "".join(
        f"<p style=\"margin:0 0 14px\"><strong>{e(LIBRARY[a['id']][0])}</strong><br>{e(a['line'])} "
        f"<a href=\"{LIBRARY[a['id']][1]}\" style=\"color:#142B4C\">{e(LIBRARY[a['id']][2])}</a></p>" for a in b["also"])
    btn = (f"<a href=\"{article_url}\" style=\"display:inline-block;background:#142B4C;color:#ffffff;text-decoration:none;"
           f"padding:12px 22px;border-radius:6px;font-weight:bold\">Read the full guide</a>")
    ps = e(WHATSAPP_PS).replace("https://shor.by/wln-whatsapp", "<a href=\"https://shor.by/wln-whatsapp\">https://shor.by/wln-whatsapp</a>")
    return f"""<!doctype html><html><body style="margin:0;background:#F4EBD7;font-family:Arial,Helvetica,sans-serif;color:#1f2937">
<span style="display:none;max-height:0;overflow:hidden">{e(b['preheader'])}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4EBD7"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px">
<tr><td style="background:#142B4C;color:#ffffff;padding:18px 28px;font-size:13px;letter-spacing:1px;border-radius:8px 8px 0 0">THE WELL-LED NETWORK &middot; THIS WEEK</td></tr>
<tr><td style="padding:28px;font-size:16px;line-height:1.6">
<h1 style="font-family:Georgia,serif;font-size:26px;line-height:1.25;color:#142B4C;margin:0 0 18px">{e(b['headline'])}</h1>
<p>Hi {{{{ contact.FIRSTNAME | default : "there" }}}},</p>
<p>{e(b['hook'])}</p><p>{e(b['context'])}</p>
<p>Three things to do this week:</p><ol style="padding-left:20px">{steps}</ol>
<p>{e(b['closing'])}</p>
<p style="margin:24px 0">{btn}</p>
<p style="font-size:12px;letter-spacing:1px;color:#B88A2E;margin:28px 0 10px">ALSO THIS WEEK</p>{also}
<div style="background:#F4EBD7;border-radius:6px;padding:16px 18px;margin:24px 0">
<strong style="color:#142B4C">Bring your question to the Well-Led Clinic</strong><br>
The Clinic runs live, and the next date is always on the Events page. Ask anything on CQC, registration, inspection or
running your service, and get a straight answer from someone who has done the job.
<a href="https://www.welllednetwork.com/events" style="color:#142B4C">See the next date</a></div>
{SIGNATURE_HTML}
<p style="font-size:14px;color:#4b5563;margin-top:18px">{ps}</p>
</td></tr>
<tr><td style="padding:16px 28px 24px;font-size:12px;color:#6b7280;line-height:1.5">You are getting this because you are a member of the
Well-Led Network. <a href="{{{{ unsubscribe }}}}" style="color:#6b7280">Unsubscribe</a> at any time.<br>
The Kajidori Collective Ltd, trading as Well-Led Network. Registered in England and Wales, company number 17556769.
71-75 Shelton Street, Covent Garden, London WC2H 9JQ.</td></tr>
</table></td></tr></table></body></html>"""


def as_text(b: dict, article_url: str) -> str:
    steps = "\n".join(f"{n}. {s['title']} {s['text']}" for n, s in enumerate(b["steps"], 1))
    also = "\n".join(f"- {LIBRARY[a['id']][0]}: {a['line']} {LIBRARY[a['id']][1]}" for a in b["also"])
    return (f"Subject: {b['subject']}\nPreview line: {b['preheader']}\n\n{b['headline']}\n\n{b['hook']}\n\n{b['context']}\n\n"
            f"{steps}\n\n{b['closing']}\n\nButton: Read the full guide {article_url}\n\nAlso this week:\n{also}")


# ---------------------------------------------------------------- the Monday job
def _tuesday_send_time(now: datetime) -> datetime:
    days = (1 - now.weekday()) % 7 or 7
    tue = (now + timedelta(days=days)).date()
    return datetime(tue.year, tue.month, tue.day, 7, 30, tzinfo=UK)


def monday(store, telegram, now: datetime | None = None, force: bool = False) -> str:
    import sources

    now = now or datetime.now(UK)
    week = now.strftime("%Y-%m-%d")
    if not force and (now.weekday() != 0 or week < START):
        return "not a briefing Monday"
    if store.r and store.r.get(K_WEEK + week):
        return "already drafted this week"
    if not _key():
        telegram.send_message("Weekly briefing: not drafted, because BREVO_API_KEY is not set in Render.")
        return "no key"
    used = set(store.r.smembers(K_USED)) if store.r else set()
    skipped = []
    for t in ROTATION:
        if t["key"] in used:
            continue
        text = sources.page_text(t["url"])
        if len(text) < 1200:
            skipped.append(t["url"])
            continue
        b = write(text, t["url"])
        html_body = render(b, t["url"])
        send_at = _tuesday_send_time(now)
        lid = list_id(store)
        r = _brevo("POST", "/emailCampaigns", {
            "name": f"Weekly Well-Led Briefing - {send_at:%d %b %Y}", "subject": b["subject"], "previewText": b["preheader"],
            "sender": SENDER, "replyTo": REPLY_TO, "htmlContent": html_body, "recipients": {"listIds": [lid]},
            "scheduledAt": send_at.isoformat(), "inlineImageActivation": False})
        if r.status_code not in (200, 201):
            telegram.send_message(f"Weekly briefing: Brevo would not create the campaign ({r.status_code}: {r.text[:300]}). "
                                  "Nothing will go out this Tuesday unless this is fixed.")
            return "brevo refused"
        cid = r.json().get("id")
        try:
            _brevo("POST", f"/emailCampaigns/{cid}/sendTest", {"emailTo": [TEST_TO]})
        except Exception:
            pass
        rec = {"week": week, "campaign_id": cid, "send_at": send_at.isoformat(), "topic": t["key"], "url": t["url"],
               "briefing": b, "status": "scheduled"}
        if store.r:
            store.r.set(K_WEEK + week, json.dumps(rec))
            store.r.set(K_LAST, week)
            store.r.sadd(K_USED, t["key"])
        n = list_count(store)
        base = os.getenv("PUBLIC_BASE_URL", "https://wln-replies.onrender.com")
        telegram.send_message(
            f"Weekly briefing for Tuesday {send_at:%d %B}, scheduled for 07:30 to "
            f"{n if n is not None else 'all'} members on the Brevo list.\n"
            f"See it as members will: {base}/briefing/{week}/{preview_token(week)}\n"
            f"A test copy has gone to {TEST_TO}.\n\n{as_text(b, t['url'])}\n\n"
            "Do nothing and it goes out on Tuesday. Reply briefing hold to stop it, briefing go to put it back on."
            + (f"\n\nCould not read, so skipped: {', '.join(skipped)}" if skipped else ""))
        return f"scheduled campaign {cid}"
    telegram.send_message("Weekly briefing: every article in the rotation has been used"
                          + (f" or could not be read ({', '.join(skipped)})" if skipped else "")
                          + ". Nothing is scheduled this Tuesday. Ask Claude to add the next articles to briefing.py.")
    return "rotation empty"


# ---------------------------------------------------------------- Telegram commands
def _current(store) -> dict | None:
    if not store.r:
        return None
    week = store.r.get(K_LAST)
    raw = store.r.get(K_WEEK + week) if week else None
    return json.loads(raw) if raw else None


def handle(text: str, store) -> str | None:
    t = text.strip().lower()
    if not t.startswith("briefing"):
        return None
    cmd = t[len("briefing"):].strip()
    if cmd == "setup":
        base = os.getenv("PUBLIC_BASE_URL", "https://wln-replies.onrender.com")
        n = list_count(store) if _key() else None
        return (f"Member sync link for the Well-Led Network webhooks (New signup, Member removed, Account suspended):\n"
                f"{base}/wln/members/{member_token()}\n"
                f"Brevo list '{LIST_NAME}': {n if n is not None else 'not reachable'} members. Briefings start {START}.")
    cur = _current(store)
    if not cur:
        return f"No weekly briefing has been drafted yet. The first one is written on Monday {START}."
    when = datetime.fromisoformat(cur["send_at"]).strftime("%A %d %B, %H:%M")
    if cmd == "":
        return f"This week's briefing: {cur['briefing']['subject']}\nStatus: {cur['status']}, for {when}."
    if cmd == "hold":
        r = _brevo("PUT", f"/emailCampaigns/{cur['campaign_id']}/status", {"status": "suspended"})
        if not r.ok:
            return f"Could not hold it ({r.status_code}: {r.text[:200]}). It may already have gone."
        cur["status"] = "held"
        store.r.set(K_WEEK + cur["week"], json.dumps(cur))
        return "Held. This week's briefing will not go out. Reply briefing go to put it back on."
    if cmd in ("go", "send", "release"):
        send_at = datetime.fromisoformat(cur["send_at"])
        if datetime.now(UK) >= send_at:
            r = _brevo("POST", f"/emailCampaigns/{cur['campaign_id']}/sendNow")
            msg = "Sending now."
        else:
            r = _brevo("PUT", f"/emailCampaigns/{cur['campaign_id']}", {"scheduledAt": send_at.isoformat()})
            if not r.ok:  # some accounts need the status put back first
                _brevo("PUT", f"/emailCampaigns/{cur['campaign_id']}/status", {"status": "queued"})
                r = _brevo("PUT", f"/emailCampaigns/{cur['campaign_id']}", {"scheduledAt": send_at.isoformat()})
            msg = f"Back on. It goes out {when}."
        if not r.ok:
            return f"Could not put it back on ({r.status_code}: {r.text[:200]})."
        cur["status"] = "scheduled"
        store.r.set(K_WEEK + cur["week"], json.dumps(cur))
        return msg
    return "Briefing commands: briefing, briefing hold, briefing go, briefing setup."
