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
_lock = threading.Lock()

FREE_URL = "https://www.vectera.com/book/the-kajidori-care-brief/u/yeukai/t/option-1-well-led-network-expert-session-free/"
GIFT_URL = ("https://www.vectera.com/book/the-kajidori-care-brief/u/yeukai/t/"
            "option-2-well-led-network-expert-session-gift-a-home-charitable-gift-67/")

BOOKING_TEXT = f"""Congratulations, your application has been approved.

Your Expert Session is a relaxed 45-minute recorded conversation with Yeukai Kajidori, shared with registered managers and care leaders across the UK on the Well-Led Network.

Sessions are recorded on Thursdays between 4pm and 5pm (UK time), one guest per week, first come first served. Choose the option that suits you and pick your Thursday:

Option 1, free:
{FREE_URL}

Option 2, Gift a Home (a charitable gift, amount shown on the booking page):
{GIFT_URL}
Everything in the free session, and 100% of your gift goes to Edge Housing, an Oxford charity giving homes to people who are homeless. In health and social care we spend our working lives supporting people at their most vulnerable; this is a simple way to carry that care beyond our own services. Contributing guests also receive a promotion pack, the edited recording, a written feature linking to your organisation and, where there's a genuine fit, a warm introduction.

Both options give you the same session and the same welcome. Book as soon as you can, as Thursdays go quickly."""

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
    r = requests.request(method, f"{API}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json"}, **kw)
    return r


def _submitted() -> list[dict]:
    tpl = os.getenv("EXPERT_TEMPLATE_ID", "374233")
    live_from = os.getenv("EXPERT_LIVE_FROM", "2026-10-28")
    out, page = [], 1
    while page <= 5:
        r = _cd("GET", "/applications", params={"template_id": tpl, "status": "submitted", "per_page": 100,
                                                "page": page, "created_after": live_from, "sort": "-updated_at"})
        if r.status_code != 200:
            raise RuntimeError(f"Clustdoc list failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        out += data.get("data") or []
        if not (data.get("links") or {}).get("next"):
            break
        page += 1
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
    if store.r:
        store.r.hset(DONE, str(app_id), "approved " + datetime.now(UK).strftime("%d %b %H:%M"))
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
        return ", ".join(notes) or "nothing due"


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
    """Test run on a test application: one sample pack per upload format, to see which fills Files."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (540, 675), (20, 33, 61)).save(buf, "PNG")
    png = buf.getvalue()
    out = []
    for fld in ("attachments[]", "files[]", "file"):
        out.append(add_portal_item(app_id, "fileshare", f"Pack test {fld}", "Sample pack text.",
                                   [("test-graphic.png", png, "image/png")], field=fld)[1])
    print("[probe]", " | ".join(out))
    return "Probe results:\n" + "\n".join(out)


def check_in_background(store, send) -> None:
    threading.Thread(target=lambda: _safe(store, send), daemon=True).start()


def _safe(store, send) -> None:
    try:
        print("[expert sessions]", check(store, send))
    except Exception as exc:
        print("[expert sessions failed]", exc)
        if store.r and not store.r.set("wln:expert_err", "1", nx=True, ex=6 * 3600):
            return
        try:
            send(f"Expert Sessions check hit a problem: {str(exc)[:300]}")
        except Exception:
            pass
