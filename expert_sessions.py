"""Well-Led Network Expert Sessions: automatic guest approval from Clustdoc.

How it works (no extra tools, no extra cost):
  1. Guests apply in Clustdoc, process "Well-Led Network Expert Sessions - Guest Application".
  2. Every 10 minutes in UK daytime (the wln-keep-awake ping) this module looks for submitted
     applications in that process (at most every 15 minutes).
  3. Once an application has been submitted for 24 hours, Claude reads the answers and checks the
     topic is UK health and social care (CQC, Ofsted-regulated children's services, NHS, care
     workforce and so on).
  4. On topic: every item is approved in Clustdoc, a "Book your Expert Session" box with both
     Vectera links (free, and the Edge Housing charitable gift) is added to the guest's portal, the
     guest is emailed the same links through Brevo, and Yeukai gets a one-line Telegram note.
  5. Off topic or unsure: nothing is approved. Yeukai gets the topic and the reason on Telegram and
     replies "guest approve 123" or "guest decline 123".

Render settings (wln-secrets):
  CLUSTDOC_API_TOKEN        required, Clustdoc > Settings > Developers > API (applications + tags scopes)
  EXPERT_TEMPLATE_ID        optional, default 374233
  EXPERT_SENDER_EMAIL       optional, a Brevo-verified sender, default support@welllednetwork.com
  EXPERT_LIVE_FROM          optional, ignore applications created before this date, default 2026-10-28
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
API = "https://app.clustdoc.com/api/v2"
BREVO = "https://api.brevo.com/v3"

DONE = "wln:expert_done"            # hash: application id -> outcome
HELD = "wln:expert_held"            # hash: application id -> json (waiting for Yeukai)
LAST_RUN = "wln:expert_last_run"
WAIT_HOURS = 24
APPROVED_AT = "wln:expert_approved_at"   # hash: application id -> ISO time approved
BOOKED_APPS = "wln:expert_booked_apps"   # hash: application id -> ISO time a booking was matched
CHASE = "wln:expert_chase"               # hash: application id -> json {"n": nudges done, "stop": bool}
# Approved but not booked (31 Oct 2026, Kevin Humphrys): nothing followed up, so a guest could quietly drop off.
# Days after approval: first email nudge, second email nudge, then a Telegram note to Yeukai to call them.
CHASE_DAYS = (3, 7, 10)
_lock = threading.Lock()

FREE_URL = "https://shor.by/expert-session-free"   # Shorby, tracks clicks; goes to the free Vectera booking page
GIFT_URL = "https://shor.by/expert-session-gift"   # Shorby, tracks clicks; goes to the Gift a Home booking page

BOOKING_TEXT = f"""Congratulations, your application has been approved.

Your Expert Session is a relaxed 45-minute recorded conversation with Yeukai Kajidori, shared with registered managers and care leaders across the UK on the Well-Led Network.

Sessions are recorded on Thursdays between 4pm and 5pm (UK time), one guest per week, first come first served. Choose the option that suits you and pick your Thursday:

Option 1, free:
{FREE_URL}

Option 2, Gift a Home (a charitable gift, amount shown on the booking page):
{GIFT_URL}
Everything in the free session, and 100% of your gift goes to Edge Housing, an Oxford charity giving homes to people who are homeless. In health and social care we spend our working lives supporting people at their most vulnerable; this is a simple way to carry that care beyond our own services. Contributing guests also receive a promotion pack, the edited recording, a written feature linking to your organisation and, where there's a genuine fit, a warm introduction.

Both options give you the same session and the same welcome. Book as soon as you can, as Thursdays go quickly.

The booking page shows Thursdays up to 90 days ahead. If none of those suits you, or they are all taken, just reply to this email with "waiting list". You'll be added straight away and emailed the moment a Thursday frees up, first come, first served.

HOW YOUR SESSION WILL RUN (45 minutes)

1. Introductions (about 5 minutes)
Yeukai welcomes you and introduces you to the audience. You share a little about who you are and the work you do.

2. Your biggest takeaway (about 5 minutes)
The one thing you most want viewers and listeners to walk away with.

3. A story from practice (about 10 minutes)
A real example of how this has been applied in a similar care setting, what changed as a result, and how viewers and listeners can apply it in their own situation.

4. Your teaching slot (about 15 minutes)
Your time to showcase your expertise in depth. You are welcome to share your screen to show slides, a tool, an app or a template.

5. Questions (about 7 minutes)
Yeukai puts the questions care leaders most often ask on your topic to you.

6. How people can reach you (about 3 minutes)
Where people can find you and work with you, including your listing in the Well-Led Business Services Marketplace.

To prepare: think about your one big takeaway and your story from practice. If you plan to share your screen, have it open and ready before we start. The session takes place in Vectera, in your browser, with nothing to install, and the link is in your booking confirmation."""

CHECK_SYSTEM = """You screen guest applications for the Well-Led Network Expert Sessions, recorded conversations
for registered managers and care leaders in UK health and social care.

In scope: anything a UK health or social care leader would find useful. That includes adult social care,
supported living, domiciliary care, care homes, mental health, learning disability and autism services,
children's homes and other Ofsted-regulated children's services, the NHS and primary care, CQC and Ofsted
regulation and inspection, safeguarding, governance and leadership in care, the care workforce
(recruitment, retention, training, wellbeing), care technology and AI used in care, commissioning and
funding of care, care business set-up and growth, housing for people with care and support needs.

Out of scope: topics with no real link to health and social care, for example oil and energy markets,
global warming in general, crypto, general marketing or business advice that never touches care.
A general topic counts as in scope only if the application clearly applies it to health or social care.

Reply with JSON only:
{"decision": "approve" | "review", "topic": "the topic in under 15 words", "reason": "one sentence"}
Use "approve" only when the topic is clearly in scope. Use "review" when it is out of scope, unclear,
promotional with no real expertise to share, or the answers are too thin to judge."""


# ---------------------------------------------------------------- Clustdoc
def _token() -> str:
    return os.getenv("CLUSTDOC_API_TOKEN", "").strip()


def _cd(method: str, path: str, **kw):
    """Clustdoc call. 31 Oct 2026: a GET came back 200 with a blank/non-JSON body ("Expecting value: line 1
    column 1"), which stopped the whole check. Retry a GET once after a short pause when the body is not JSON."""
    for attempt in (1, 2):
        r = requests.request(method, f"{API}{path}", timeout=30,
                             headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json"}, **kw)
        if method.upper() != "GET" or r.status_code != 200 or attempt == 2:
            return r
        try:
            r.json()
            return r
        except ValueError:
            print(f"[clustdoc] non-JSON reply on {path} ({r.headers.get('content-type', '')}): {r.text[:120]!r}; retrying")
            time.sleep(5)
    return r


def _tpl_of(app: dict) -> str:
    t = app.get("template_id") or app.get("templateId")
    if not t and isinstance(app.get("template"), dict):
        t = app["template"].get("id")
    return str(t or "")


def _status_of(app: dict) -> str:
    st = app.get("status")
    if isinstance(st, dict):
        st = st.get("name") or st.get("label") or st.get("value") or ""
    return str(st or "").lower()


def _date_of(app: dict, *keys: str) -> datetime | None:
    for k in keys:
        v = app.get(k)
        if v:
            try:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _submitted() -> list[dict]:
    """Submitted Expert Session applications.

    31 Oct 2026: Kevin Humphrys (20774777, submitted 30 Oct) was never found by the API filters
    (status=submitted / created_after), so he sat unapproved. Now the list asks Clustdoc for the template
    only and filters here: right template, created on or after EXPERT_LIVE_FROM, status submitted/complete.
    An application whose template cannot be read is skipped (never risk emailing an old podcast guest)."""
    tpl = os.getenv("EXPERT_TEMPLATE_ID", "374233")
    live_from = datetime.fromisoformat(os.getenv("EXPERT_LIVE_FROM", "2026-10-28")).replace(tzinfo=timezone.utc)
    out, seen, page = [], [], 1
    while page <= 5:
        r = _cd("GET", "/applications", params={"template_id": tpl, "per_page": 100, "page": page,
                                                "sort": "-updated_at"})
        if r.status_code != 200:
            raise RuntimeError(f"Clustdoc list failed ({r.status_code}): {r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Clustdoc sent a non-JSON reply twice for the applications list "
                               f"({r.headers.get('content-type', '')}): {r.text[:120]!r}")
        rows = data.get("data") or []
        if page == 1 and rows:
            print("[expert sessions] list keys:", sorted(rows[0].keys())[:30])
        for app in rows:
            aid, t, st = app.get("id"), _tpl_of(app), _status_of(app)
            created = _date_of(app, "created_at", "createdAt")
            why = ""
            if t != tpl:
                why = f"template {t or 'unknown'}"
            elif created and created < live_from:
                why = "created before live date"
            elif not any(w in st for w in ("submit", "complete")):
                why = f"status {st or 'unknown'}"
            seen.append(f"{aid}:{'ok' if not why else why}")
            if not why:
                out.append(app)
        if not (data.get("links") or {}).get("next"):
            break
        page += 1
    print(f"[expert sessions] listed {len(seen)}, submitted {len(out)}: " + ", ".join(seen[:15]))
    return out


def _items(app_id: int) -> list[dict]:
    r = _cd("GET", f"/applications/{app_id}/application-items", params={"include": "content", "per_page": 100})
    r.raise_for_status()
    return r.json().get("data") or []


def _flatten(obj, prefix: str = "", out: list | None = None, depth: int = 0) -> list[str]:
    out = [] if out is None else out
    if depth > 6 or len(out) > 200:
        return out
    if isinstance(obj, dict):
        label = obj.get("label") or obj.get("name") or obj.get("title")
        value = obj.get("value", obj.get("answer", obj.get("values")))
        if label and value not in (None, "", []):
            out.append(f"{_clean(label, 200)}: {_clean(value if not isinstance(value, list) else ', '.join(map(str, value)), 1500)}")
            return out
        for k, v in obj.items():
            _flatten(v, f"{prefix}{k}.", out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _flatten(v, prefix, out, depth + 1)
    elif isinstance(obj, str) and len(obj) > 2 and prefix:
        out.append(f"{prefix.rstrip('.')}: {_clean(obj, 1500)}")
    return out


def _clean(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _answers(items: list[dict]) -> str:
    lines = []
    for it in items:
        if it.get("type") in ("form",) or it.get("content"):
            got = _flatten(it.get("content"))
            if got:
                lines.append(f"[{_clean(it.get('title'), 120)}]")
                lines += got
    return "\n".join(lines)[:12000]


def _approve_items(app_id: int, items: list[dict]) -> str:
    ids = [it["id"] for it in items if it.get("id") and not str(it.get("status", "")).startswith("2")
           and it.get("type") not in ("header", "separator", "textbox")]
    if not ids:
        return "nothing to approve"
    r = _cd("POST", f"/applications/{app_id}/application-items/bulk-approve", json={"items": ids})
    if r.status_code == 200:
        return f"{len(ids)} items approved"
    # one by one, so a single awkward item doesn't block the rest
    ok = sum(1 for i in ids if _cd("POST", f"/application-items/{i}/approve").status_code == 200)
    return f"{ok} of {len(ids)} items approved"


def add_portal_item(app_id: int, kind: str, title: str, text: str,
                    files: list[tuple[str, bytes, str]] | None = None, field: str = "") -> tuple[bool, str]:
    """Adds a step to the guest's portal: graphics go in with the step (so they land in its Files),
    the text goes in the step's instructions, and the step is then made visible and titled."""
    try:
        r = _cd("GET", f"/applications/{app_id}/application-items", params={"per_page": 100})
        if r.status_code == 200 and any((i.get("title") or "") == title for i in r.json().get("data") or []):
            return True, f"'{title}' already in their portal"
    except Exception:
        pass
    body = {"type": kind, "title": title, "instructions": _html(text)}
    c, note = None, ""
    if files:
        headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
        for fld in ([field] if field else ["attachments[]", "files[]", "file"]):
            c = requests.post(f"{API}/applications/{app_id}/application-items", headers=headers, data=body,
                              files=[(fld, (n, d, m)) for n, d, m in files], timeout=120)
            if c.status_code in (200, 201):
                note = f"{len(files)} file(s) sent as {fld}"
                break
            note = f"files not accepted ({c.status_code} {c.text[:100]})"
    if c is None or c.status_code not in (200, 201):
        c = _cd("POST", f"/applications/{app_id}/application-items", json=body)
    if c.status_code not in (200, 201):
        return False, f"'{title}' not added ({c.status_code} {c.text[:120]})"
    iid = (c.json().get("data") or {}).get("id")
    notes = [note] if note else []
    shown = False
    for method in ("PATCH", "PUT"):
        p = _cd(method, f"/application-items/{iid}", json={"title": title, "visible_to_client": True,
                                                            "instructions": _html(text)})
        if p.status_code in (200, 201):
            shown = bool((p.json().get("data") or {}).get("visible_to_client", True))
            break
        notes.append(f"{method} {p.status_code} {p.text[:100]}")
    vis = "visible to the guest" if shown else "ADDED BUT HIDDEN from the guest"
    return shown, f"'{title}' added to their portal, {vis}" + (f"; {'; '.join(notes)}" if notes else "")


def _html(text: str) -> str:
    """Instructions are what the guest sees on the step; keep paragraphs and make links clickable."""
    return "<p>&nbsp;</p>".join(f"<p>{_link(p).replace(chr(10), '<br>')}</p>" for p in text.split("\n\n"))


def _add_booking_box(app_id: int) -> str:
    return add_portal_item(app_id, "fileshare", "Book your Expert Session", BOOKING_TEXT)[1]


def _tag(app_id: int, name: str) -> None:
    try:
        _cd("POST", f"/applications/{app_id}/tags", json={"name": name})
    except Exception:
        pass


# ---------------------------------------------------------------- email (Brevo, already in Render)
def _email(contact: dict, subject: str, text: str) -> str:
    key = os.getenv("BREVO_API_KEY", "").strip()
    to = (contact or {}).get("email")
    if not key:
        return "no email (BREVO_API_KEY not set)"
    if not to:
        return "no email (no address on the application)"
    first = (contact or {}).get("firstname") or ""
    body = f"Hi {first},\n\n{text}\n\nWarm regards\nYeukai Kajidori\nThe Kajidori Collective\nhttps://www.welllednetwork.com/blog"
    html = "<br>".join(_link(l) for l in body.split("\n"))
    r = requests.post(f"{BREVO}/smtp/email", timeout=30,
                      headers={"api-key": key, "accept": "application/json", "content-type": "application/json"},
                      json={"sender": {"name": "Yeukai Kajidori, Well-Led Network",
                                       "email": os.getenv("EXPERT_SENDER_EMAIL", "support@welllednetwork.com")},
                            "to": [{"email": to, "name": f"{first} {(contact or {}).get('lastname') or ''}".strip() or to}],
                            "replyTo": {"email": "kajidoricollective@gmail.com"},
                            "subject": subject, "textContent": body, "htmlContent": html})
    return "emailed" if r.status_code in (200, 201) else f"email not sent (Brevo said {r.status_code}: {r.text[:120]})"


def add_to_list(contact: dict) -> str:
    """Puts the guest in the Brevo list 'WLN Expert Session Guests' (list #32), creating or updating them."""
    key = os.getenv("BREVO_API_KEY", "").strip()
    to = (contact or {}).get("email")
    if not key or not to:
        return "not added to the Brevo guest list (no key or no email)"
    lid = int(os.getenv("EXPERT_BREVO_LIST", "32"))
    r = requests.post(f"{BREVO}/contacts", timeout=30,
                      headers={"api-key": key, "accept": "application/json", "content-type": "application/json"},
                      json={"email": to, "listIds": [lid], "updateEnabled": True,
                            "attributes": {"FIRSTNAME": (contact or {}).get("firstname") or "",
                                           "LASTNAME": (contact or {}).get("lastname") or ""}})
    return "added to the Brevo guest list" if r.status_code in (200, 201, 204) else \
        f"not added to the Brevo guest list ({r.status_code}: {r.text[:100]})"


def _link(line: str) -> str:
    line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"(https://\S+)", r'<a href="\1">\1</a>', line)


# ---------------------------------------------------------------- AI check
def _judge(answers: str) -> dict:
    import ai

    if not answers.strip():
        return {"decision": "review", "topic": "(no answers found)", "reason": "The form answers could not be read."}
    try:
        reply = ai._ask(CHECK_SYSTEM, "Application answers:\n\n" + answers, max_tokens=400)
        d = ai._json(reply)
        if d.get("decision") not in ("approve", "review"):
            d["decision"] = "review"
        return d
    except Exception as exc:
        return {"decision": "review", "topic": "(check failed)", "reason": f"The AI check failed: {str(exc)[:120]}"}


# ---------------------------------------------------------------- outcomes
def _name(app: dict) -> str:
    c = app.get("contact") or {}
    n = f"{c.get('firstname') or ''} {c.get('lastname') or ''}".strip()
    return n or c.get("email") or app.get("title") or f"application {app.get('id')}"


def approve(store, app: dict, items: list[dict] | None = None, note: str = "") -> str:
    app_id = app["id"]
    items = items if items is not None else _items(app_id)
    steps = [_approve_items(app_id, items), _add_booking_box(app_id)]
    _tag(app_id, "Expert Session - Approved")
    try:  # move the guest along the tracker; harmless if Clustdoc expects a different stage value
        st = _cd("PATCH", f"/applications/{app_id}", json={"stage": "3. Approved - session booked"})
        steps.append("stage set to 3. Approved" if st.status_code == 200 else f"stage not changed ({st.status_code})")
    except Exception:
        pass
    steps.append(_email(app.get("contact") or {}, "You're approved: book your Well-Led Network Expert Session",
                        BOOKING_TEXT))
    steps.append(add_to_list(app.get("contact") or {}))
    if store.r:
        store.r.hset(DONE, str(app_id), "approved " + datetime.now(UK).strftime("%d %b %H:%M"))
        store.r.hset(APPROVED_AT, str(app_id), datetime.now(UK).isoformat())
        store.r.hdel(HELD, str(app_id))
    return f"Expert Session guest approved{note}: {_name(app)} ({app_id})\n" + "; ".join(steps)


def decline(store, app_id: int) -> str:
    r = _cd("GET", f"/applications/{app_id}")
    if r.status_code != 200:
        return f"Couldn't find Clustdoc application {app_id} ({r.status_code})."
    app = r.json().get("data") or {}
    _tag(app_id, "Expert Session - Not this time")
    text = ("Thank you for applying to be an Expert Session guest on the Well-Led Network. The sessions focus on "
            "UK health and social care, and on this occasion your topic isn't the right fit for this audience.\n\n"
            "You're very welcome to apply again with a topic that connects your expertise to care services, and in "
            "the meantime the articles and tools on the Well-Led Network are free to use.")
    sent = _email(app.get("contact") or {}, "Your Well-Led Network Expert Session application", text)
    if store.r:
        store.r.hset(DONE, str(app_id), "declined " + datetime.now(UK).strftime("%d %b %H:%M"))
        store.r.hdel(HELD, str(app_id))
    return f"Declined {_name(app)} ({app_id}); tagged in Clustdoc; {sent}."


def check(store, send, force: bool = False) -> str:
    if not _token():
        return "skipped (CLUSTDOC_API_TOKEN not set)"
    if not force and store.r:
        if not store.r.set(LAST_RUN, "1", nx=True, ex=15 * 60):
            return "skipped (ran in the last 15 minutes)"
    with _lock:
        now = datetime.now(timezone.utc)
        notes = []
        for app in _submitted():
            aid = str(app.get("id"))
            if store.r and (store.r.hexists(DONE, aid) or store.r.hexists(HELD, aid)):
                continue
            sub = app.get("submitted_at") or app.get("updated_at")
            try:
                sub_dt = datetime.fromisoformat(str(sub).replace("Z", "+00:00"))
            except Exception:
                sub_dt = now
            if now - sub_dt < timedelta(hours=WAIT_HOURS):
                continue
            items = _items(app["id"])
            verdict = _judge(_answers(items))
            if verdict["decision"] == "approve":
                msg = approve(store, app, items)
                send(msg + f"\nTopic: {verdict.get('topic', '')}")
                notes.append(f"approved {aid}")
            else:
                if store.r:
                    store.r.hset(HELD, aid, json.dumps({"name": _name(app), "topic": verdict.get("topic"),
                                                        "reason": verdict.get("reason"), "at": now.isoformat()}))
                _tag(app["id"], "Expert Session - Needs Yeukai")
                send("Expert Session application needs your decision\n"
                     f"{_name(app)} (Clustdoc {aid})\n"
                     f"Topic: {verdict.get('topic', '')}\n"
                     f"Why it was held: {verdict.get('reason', '')}\n"
                     f"Application: {app.get('public_url') or 'open it in Clustdoc'}\n\n"
                     f"Reply: guest approve {aid}  or  guest decline {aid}")
                notes.append(f"held {aid}")
        try:
            notes += chase(store, send)
        except Exception as exc:
            print("[expert chase failed]", exc)
        return ", ".join(notes) or "nothing due"


# ---------------------------------------------------------------- approved but not booked
NUDGE_1 = """Just a friendly follow-up: your Well-Led Network Expert Session is approved and your place is ready. It only needs a Thursday.

Book the free session here (pick any Thursday that suits, 4pm UK time):
{free}

Or the Gift a Home option, where 100% of your gift goes to Edge Housing:
{gift}

It takes about two minutes. If none of the Thursdays suit you, just reply "waiting list" and I'll offer you the next one that frees up. If something has come up and now isn't the right time, reply "later" and I'll check back in a few months. No pressure either way."""

NUDGE_2 = """I didn't want your Expert Session to slip through the cracks, so this is my last reminder.

Registered managers and care leaders across the UK will watch your session, and it links straight to your listing in the Well-Led Business Services Marketplace, so it keeps working for you long after we record.

Book your Thursday here:
{free}

Gift a Home option:
{gift}

Or simply reply with a Thursday that works for you and I'll book it for you myself."""


def _approved_at(store, aid: str, done_val: str) -> datetime | None:
    v = store.r.hget(APPROVED_AT, aid)
    if v:
        try:
            return datetime.fromisoformat(v)
        except Exception:
            pass
    m = re.match(r"approved (\d{1,2} \w{3} \d{1,2}:\d{2})", done_val or "")
    if not m:
        return None
    now = datetime.now(UK)
    try:
        d = datetime.strptime(f"{m.group(1)} {now.year}", "%d %b %H:%M %Y").replace(tzinfo=UK)
    except ValueError:
        return None
    return d.replace(year=now.year - 1) if d > now + timedelta(days=1) else d


def _is_booked(store, aid: str, app: dict | None) -> bool:
    if store.r.hexists(BOOKED_APPS, aid):
        return True
    c = (app or {}).get("contact") or {}
    email = (c.get("email") or "").lower()
    full = f"{c.get('firstname') or ''} {c.get('lastname') or ''}".strip().lower()
    for v in store.r.hgetall("wln:expert_booked").values():  # expert_brief.BOOKED
        try:
            b = json.loads(v)
        except Exception:
            continue
        if b.get("skip"):
            continue
        if str(b.get("app_id") or "") == aid or (email and (b.get("email") or "").lower() == email) \
                or (full and (b.get("name") or "").strip().lower() == full):
            store.r.hset(BOOKED_APPS, aid, datetime.now(UK).isoformat())
            return True
    return False


def chase(store, send, force: bool = False) -> list[str]:
    """Follow up approved guests who haven't booked: email nudges on CHASE_DAYS[0] and [1], then a
    Telegram note to Yeukai on CHASE_DAYS[2]. Weekdays 9am to 6pm UK only; one step per guest per run.
    Stops for anyone booked, on the waiting list, or stopped with 'guest stop 123'."""
    if not store.r:
        return []
    now = datetime.now(UK)
    if not force and (now.weekday() > 4 or not 9 <= now.hour < 18):
        return []
    notes = []
    for aid, val in store.r.hgetall(DONE).items():
        if not str(val).startswith("approved"):
            continue
        state = json.loads(store.r.hget(CHASE, aid) or "{}")
        if state.get("stop") or state.get("n", 0) >= 3 or store.r.hexists("wln:expert_waitlist", aid):
            continue
        at = _approved_at(store, aid, val)
        if not at:
            continue
        step = state.get("n", 0)
        if now - at < timedelta(days=CHASE_DAYS[step]):
            continue
        r = _cd("GET", f"/applications/{aid}")
        if r.status_code != 200:
            continue
        app = r.json().get("data") or {}
        if _is_booked(store, aid, app):
            continue
        name, contact = _name(app), app.get("contact") or {}
        if step < 2:
            text = (NUDGE_1 if step == 0 else NUDGE_2).format(free=FREE_URL, gift=GIFT_URL)
            subject = ("Your Expert Session: pick your Thursday" if step == 0
                       else "Last reminder: your Well-Led Network Expert Session")
            out = _email(contact, subject, text)
            if out != "emailed":
                send(f"Expert Session follow-up to {name} ({aid}) was NOT sent: {out}")
                continue
            send(f"Expert Session follow-up {step + 1} of 2 emailed to {name} ({aid}): approved "
                 f"{(now - at).days} days ago, not booked yet.\nReply 'guest stop {aid}' to stop, "
                 f"or 'guest booked {aid}' if they've booked another way.")
        else:
            send(f"Expert Session guest still not booked: {name} ({aid}), approved {(now - at).days} days ago, "
                 f"two reminders sent. Worth a quick call or WhatsApp.\nEmail: {contact.get('email') or 'not given'}"
                 + (f"\nPhone: {contact.get('phone')}" if contact.get("phone") else "")
                 + f"\nReply 'waitlist add {aid}', 'guest booked {aid}' or 'guest stop {aid}'.")
        state["n"] = step + 1
        state["last"] = now.isoformat()
        store.r.hset(CHASE, aid, json.dumps(state))
        notes.append(f"chase{step + 1} {aid}")
    return notes


def on_guest_reply(store, sender: str, subject: str, body: str, send) -> str:
    """An email reply reached kajidoricollective Gmail. If it is from an approved Expert Session guest:
    'waiting list' puts them on the waiting list; 'later' stops the follow-ups; anything else pauses the
    automatic follow-ups (a real conversation has started) and tells Yeukai what they said."""
    if not store.r or not _token():
        return ""
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", sender or "")
    email = (m.group(0) if m else "").lower()
    if not email:
        return ""
    for aid, val in store.r.hgetall(DONE).items():
        if not str(val).startswith("approved"):
            continue
        r = _cd("GET", f"/applications/{aid}")
        if r.status_code != 200:
            continue
        app = r.json().get("data") or {}
        if ((app.get("contact") or {}).get("email") or "").lower() != email:
            continue
        # only the new part of the reply, not the quoted email underneath
        new = re.split(r"(?im)^(?:>|on .{5,80}wrote:|-----original message|from:\s)", body or "")[0]
        low = new.lower()
        name = _name(app)
        state = json.loads(store.r.hget(CHASE, aid) or "{}")
        if "waiting list" in low or "waitlist" in low:
            store.r.hset("wln:expert_waitlist", aid, datetime.now(UK).isoformat())
            _tag(int(aid), "Expert Session - Waiting list")
            send(f"{name} ({aid}) replied 'waiting list', so they're on the Expert Session waiting list now. "
                 "They'll be emailed the moment a Thursday frees up.")
            return "guest waitlisted"
        state["stop"] = True
        store.r.hset(CHASE, aid, json.dumps(state))
        said = re.sub(r"\s+", " ", new).strip()[:400] or "(no text)"
        if re.search(r"\blater\b|not now|not the right time", low):
            send(f"{name} ({aid}) replied that now isn't the right time. Automatic follow-ups stopped.\n"
                 f"They said: {said}")
            return "guest later"
        if not _is_booked(store, aid, app):
            send(f"Expert Session guest replied: {name} ({aid}), not booked yet. Automatic follow-ups paused "
                 f"while you talk.\nThey said: {said}\n\nIf they name a Thursday, book it for them in Vectera "
                 f"(the host view shows every slot). Reply 'guest restart {aid}' to resume the reminders.")
        return "guest reply"
    return ""


def _chase_status(store) -> str:
    rows = []
    for aid, val in store.r.hgetall(DONE).items():
        if not str(val).startswith("approved"):
            continue
        state = json.loads(store.r.hget(CHASE, aid) or "{}")
        if _is_booked(store, aid, None):
            s = "booked"
        elif store.r.hexists("wln:expert_waitlist", aid):
            s = "waiting list"
        elif state.get("stop"):
            s = "follow-ups stopped"
        else:
            s = f"not booked, {state.get('n', 0)} of 2 reminders sent"
        rows.append(f"{aid}: {s} ({val})")
    return "Approved guests:\n" + "\n".join(rows) if rows else "No approved guests yet."


def handle(text: str, store) -> str | None:
    """Telegram replies: 'guest approve 123', 'guest decline 123', 'guests'."""
    t = text.strip().lower()
    if t in ("guests", "guest", "experts"):
        if not store.r:
            return "No memory store."
        held = store.r.hgetall(HELD)
        if not held:
            return "No Expert Session applications are waiting for you."
        return "Waiting for your decision:\n" + "\n".join(
            f"{k}: {json.loads(v).get('name')} - {json.loads(v).get('topic')}" for k, v in held.items())
    if t in ("approved", "guests approved", "guest chase", "not booked"):
        return _chase_status(store) if store.r else "No memory store."
    cm = re.match(r"^guest\s+(booked|stop|restart)\s+(\d+)$", t)
    if cm and store.r:
        what, aid = cm.group(1), cm.group(2)
        if what == "booked":
            store.r.hset(BOOKED_APPS, aid, datetime.now(UK).isoformat())
            return f"Noted: {aid} is booked, so no more follow-ups will go to them."
        state = json.loads(store.r.hget(CHASE, aid) or "{}")
        state["stop"] = what == "stop"
        if what == "restart":
            state["n"] = 0
            store.r.hset(APPROVED_AT, aid, datetime.now(UK).isoformat())
        store.r.hset(CHASE, aid, json.dumps(state))
        return (f"Follow-ups stopped for {aid}." if what == "stop"
                else f"Follow-ups restarted for {aid}: first reminder in {CHASE_DAYS[0]} days if they haven't booked.")
    pm = re.match(r"^guest\s+probe\s+(\d+)$", t)
    if pm:
        return probe(int(pm.group(1)))
    m = re.match(r"^guest\s+(approve|decline)\s+(\d+)$", t)
    if not m:
        return None
    if not _token():
        return "CLUSTDOC_API_TOKEN isn't set in Render yet."
    action, aid = m.group(1), int(m.group(2))
    if action == "decline":
        return decline(store, aid)
    r = _cd("GET", f"/applications/{aid}")
    if r.status_code != 200:
        return f"Couldn't find Clustdoc application {aid} ({r.status_code})."
    return approve(store, r.json().get("data") or {"id": aid}, note=" by you")


def probe(app_id: int) -> str:
    """Diagnostic on a test application: can Render fetch the guest's profile photo?"""
    import expert_promo as ep
    log: list = []
    data = ep._guest_file(_items(app_id), "photo", log)
    out = ("Photo found, " + str(len(data)) + " bytes" if data else "Photo NOT found") + "\n" + "\n".join(log)
    print("[probe]", out[:3000])
    return "Probe results:\n" + out[:3500]


def check_in_background(store, send) -> None:
    threading.Thread(target=lambda: _safe(store, send), daemon=True).start()


def _safe(store, send) -> None:
    try:
        print("[expert sessions]", check(store, send))
    except Exception as exc:
        print("[expert sessions failed]", exc)
        try:  # a Clustdoc listing hiccup must not stop the follow-ups
            print("[expert chase]", chase(store, send))
        except Exception as exc2:
            print("[expert chase failed]", exc2)
        if store.r and not store.r.set("wln:expert_err", "1", nx=True, ex=6 * 3600):
            return
        try:
            send(f"Expert Sessions check hit a problem: {str(exc)[:300]}")
        except Exception:
            pass
