"""Well-Led Network Expert Sessions: booking alerts and the Guest Brief, so Yeukai knows who is coming
and can prepare good questions.

Flow (no extra tools, no extra cost):
  1. An approved guest books a Thursday slot on the Vectera booking page. Vectera puts the booking in the
     kajidoricollective@gmail.com Google Calendar, which Render already reads (GCAL_ICS_URL).
  2. Within about 10 minutes (the wln-keep-awake ping, UK daytime) Yeukai gets a Telegram
     "Expert Session booked" alert: guest, business, date and time, Vectera link. If the guest also
     cancels or moves, he is told.
  3. The guest is matched to their Clustdoc application by email (or name), and Claude writes a one-page
     Guest Brief from the application answers plus anything the guest typed on the booking form:
       who they are and why they are credible, their topic and biggest takeaway, their story, what they
       will teach (and any screen share), their FAQs and give-back, plus 6 to 8 questions in the session
       running order, one "what does a registered manager do on Monday" question, the Six Rights pillar
       it fits, and Yeukai's natural bridge into his own support at the end.
  4. The brief goes to Telegram (message + a .md file to keep) on the Tuesday at 4pm before the session,
     or within the hour if the guest books with less notice than that.
  5. Thursday 7am: a short reminder with the guest, topic and Vectera link (the brief is resent).
  Telegram replies: "briefs" lists upcoming bookings; "brief 123" resends the brief for Clustdoc 123.

Render settings: nothing new. Uses GCAL_ICS_URL, CLUSTDOC_API_TOKEN, ANTHROPIC_API_KEY, TELEGRAM_*.
Optional: EXPERT_CAL_MATCH (regex for the calendar entry title, default "expert session|vectera").
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import expert_sessions as es

UK = ZoneInfo("Europe/London")
BOOKED = "wln:expert_booked"      # hash calendar uid -> json booking
WAITLIST = "wln:expert_waitlist"  # hash Clustdoc id -> date added (first come, first served)
LAST_RUN = "wln:expert_brief_last_run"
_lock = threading.Lock()


def _match() -> re.Pattern:
    return re.compile(os.getenv("EXPERT_CAL_MATCH", r"expert session|vectera"), re.I)


BRIEF_SYSTEM = """You prepare Yeukai Kajidori for a recorded Well-Led Network Expert Session: a 45-minute
conversation with a guest expert, watched by UK registered managers, nominated individuals and care
providers. Yeukai is the host: 23 years in UK health and social care, 15 as a CQC Registered Manager
(Home Angels homecare, PLS mental health supported living, Support Horizons). His Framework is the
Well-Led and Safe Supported Living Framework, whose six pillars he calls the Six Rights of Supported
Living: pillar one Right Picture (oversight that sees the truth), two Right People (safe people, safely
recruited), three Right Skills (competence before allocation), four Right Choices (rights in their own
home), five Right Health (health that does not fall through the gaps), six Right Support (rested staff
and trusted partnerships).

Session running order: 1 introductions, 2 biggest takeaway, 3 a story from practice and how viewers can
apply it, 4 the guest's teaching slot (they may share a screen), 5 FAQs, 6 how people can reach them.

Write the Guest Brief in plain British English, no dashes, scannable on a phone. Use ONLY facts in the
material given; where something is missing, say "Not given: ask them at the start" rather than guessing.

Format exactly:
GUEST BRIEF: <name>, <business>
Session: <date and time>

WHO THEY ARE
<3 to 5 short lines: role, business, what they are known for, the one line that proves their credibility>

THEIR SESSION
Title or topic: ...
Biggest takeaway: ...
Story from practice: ...
What they will teach: ... (screen share or app: yes/no/not given)
FAQs they get asked: ...
Their give-back: ...

YOUR QUESTIONS (in running order)
1. ... (intro)
2. ... (takeaway)
... 6 to 8 in total, each one short, open and specific to this guest

THE MONDAY QUESTION
<one question that makes them say what a registered manager should do first thing on Monday>

FRAMEWORK LINK
<which of the Six Rights pillars this fits and one line Yeukai can say, e.g. "In the Well-Led and Safe
Supported Living Framework, this is pillar three, Right Skills: competence before allocation.">

YOUR BRIDGE AT THE END
<two sentences Yeukai can say after thanking them that lead naturally, never pushily, to his own support
on the Well-Led Network for anyone who wants help putting this into practice>

RAPPORT
<two short facts from their material worth mentioning warmly>

GAPS TO FILL
<anything missing that he should ask the guest before recording starts, or "None">"""


# ---------------------------------------------------------------- calendar
def _events() -> list[dict]:
    import cal_watch

    url = os.getenv("GCAL_ICS_URL", "").strip()
    if not url:
        raise RuntimeError("GCAL_ICS_URL not set")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    now = datetime.now(UK)
    m = _match()
    return [e for e in cal_watch.parse_ics(r.text)
            if e.get("uid") and e.get("start") and e.get("status") != "CANCELLED"
            and m.search((e.get("summary") or "") + " " + (e.get("description") or "") + " " + (e.get("location") or ""))
            and now - timedelta(hours=3) <= e["start"] <= now + timedelta(days=120)]


def _own() -> set[str]:
    return {e.strip().lower() for e in os.getenv(
        "CAL_OWN_EMAILS", "kajidoricollective@gmail.com,yeukai1@hotmail.co.uk,yeukai01@gmail.com").split(",")}


def _guest(ev: dict) -> dict:
    import cal_watch

    others = [a for a in ev.get("attendees", []) if a["email"].lower() not in _own()]
    g = others[0] if others else {"name": "", "email": ""}
    desc = ev.get("description") or ""
    if not g["email"]:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", desc)
        g["email"] = m.group(0) if m and m.group(0).lower() not in _own() else ""
    answers = cal_watch._answers(desc)
    if not g["name"]:
        g["name"] = next((a["value"] for a in answers if a["label"].lower() in ("name", "full name", "your name")), "")
    link = re.search(r"https://\S*vectera\S*", desc + " " + (ev.get("location") or ""))
    return {"name": g["name"], "email": g["email"], "answers": answers,
            "vectera": link.group(0).rstrip(").,>") if link else (ev.get("location") or "")}


# ---------------------------------------------------------------- Clustdoc matching
def _find_app(store, name: str, email: str) -> dict | None:
    """Look through approved Expert Session applications for this guest."""
    ids = []
    if store.r:
        ids = [k for k, v in store.r.hgetall(es.DONE).items() if str(v).startswith("approved")]
    best = None
    for aid in ids[-60:]:
        try:
            r = es._cd("GET", f"/applications/{aid}")
            if r.status_code != 200:
                continue
            app = r.json().get("data") or {}
        except Exception:
            continue
        c = app.get("contact") or {}
        if email and (c.get("email") or "").lower() == email.lower():
            return app
        full = f"{c.get('firstname') or ''} {c.get('lastname') or ''}".strip().lower()
        if name and full and (full == name.lower() or full.split()[0] == name.lower().split()[0]
                              and full.split()[-1] == name.lower().split()[-1]):
            best = best or app
    return best


def _business(answers: str) -> str:
    m = re.search(r"(?im)^(?:business|company|organisation|organization)[^:]*:\s*(.+)$", answers)
    return m.group(1).strip()[:80] if m else ""


# ---------------------------------------------------------------- the brief
def _when(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso).astimezone(UK)
        return d.strftime("%A %-d %B, %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    except Exception:
        return iso


def write_brief(b: dict) -> str:
    import ai

    material = [f"Session date and time: {_when(b['start'])}", f"Guest name: {b.get('name') or 'not given'}",
                f"Guest email: {b.get('email') or 'not given'}"]
    if b.get("app_answers"):
        material.append("\nClustdoc application answers:\n" + b["app_answers"])
    if b.get("booking_answers"):
        material.append("\nAnswers on the booking form:\n" + "\n".join(
            f"{a['label']}: {a['value']}" for a in b["booking_answers"]))
    if not b.get("app_answers"):
        material.append("\n(No Clustdoc application could be matched to this booking.)")
    return ai._ask(BRIEF_SYSTEM, "\n".join(material)[:14000], max_tokens=2500).strip()


def _send_brief(store, send, uid: str, b: dict, label: str = "") -> None:
    import telegram_io

    if not b.get("brief"):
        if b.get("app_id") and es._token():  # pick up anything the guest added since booking
            try:
                b["app_answers"] = es._answers(es._items(int(b["app_id"]))) or b.get("app_answers", "")
            except Exception:
                pass
        b["brief"] = write_brief(b)
    send((label + "\n\n" if label else "") + b["brief"] +
         (f"\n\nVectera: {b['vectera']}" if b.get("vectera") else ""))
    try:
        safe = re.sub(r"[^A-Za-z0-9]+", "-", b.get("name") or "guest").strip("-")
        telegram_io.send_document(f"Guest-Brief-{safe}.md", b["brief"], "Guest Brief to keep")
    except Exception as exc:
        print("[brief document failed]", exc)
    b["brief_sent"] = datetime.now(UK).isoformat()
    _save(store, uid, b)


def _save(store, uid: str, b: dict) -> None:
    if store.r:
        store.r.hset(BOOKED, uid, json.dumps(b))


def _all(store) -> dict[str, dict]:
    if not store.r:
        return {}
    return {k: json.loads(v) for k, v in store.r.hgetall(BOOKED).items()}


def _brief_due(start: datetime, now: datetime) -> bool:
    """Tuesday 4pm before the session; immediately if booked inside that window."""
    days_back = (start.weekday() - 1) % 7 or 7
    due = (start - timedelta(days=days_back)).replace(hour=16, minute=0, second=0, microsecond=0)
    if due > start:
        due -= timedelta(days=7)
    return now >= due


# ---------------------------------------------------------------- the check
def check(store, send, force: bool = False) -> str:
    if not store.r:
        return "skipped (no memory store)"
    if not force and not store.r.set(LAST_RUN, "1", nx=True, ex=8 * 60):
        return "skipped (ran recently)"
    if not _lock.acquire(blocking=False):
        return "already running"
    try:
        now = datetime.now(UK)
        events = _events()
        known = _all(store)
        first_run = not store.r.exists(BOOKED) and not store.r.exists(BOOKED + ":init")
        notes = []
        current = set()
        for ev in events:
            uid = ev["uid"]
            current.add(uid)
            start_iso = ev["start"].astimezone(UK).isoformat()
            b = known.get(uid)
            if first_run:
                _save(store, uid, {"start": start_iso, "skip": True})
                continue
            if b is None:
                g = _guest(ev)
                app = _find_app(store, g["name"], g["email"]) if es._token() else None
                answers = es._answers(es._items(app["id"])) if app else ""
                b = {"start": start_iso, "name": g["name"] or (es._name(app) if app else ""), "email": g["email"],
                     "vectera": g["vectera"], "booking_answers": g["answers"], "app_id": app.get("id") if app else None,
                     "app_answers": answers}
                if app:
                    store.r.hdel(WAITLIST, str(app["id"]))
                    es._tag(app["id"], "Expert Session - Booked")
                    try:
                        es._cd("PATCH", f"/applications/{app['id']}", json={"stage": "3. Approved - session booked"})
                    except Exception:
                        pass
                biz = _business(answers)
                send("Expert Session booked\n"
                     f"Guest: {b['name'] or 'name not shown'}{', ' + biz if biz else ''}\n"
                     f"When: {_when(start_iso)}\n"
                     + (f"Vectera: {b['vectera']}\n" if b.get("vectera") else "")
                     + (f"Clustdoc: {app['id']}\n" if app else
                        "Clustdoc: no approved application matched this guest's email, so check who they are.\n")
                     + "Your Guest Brief follows on the Tuesday before at 4pm"
                     + (" (sending it now, as the session is soon)." if _brief_due(ev["start"], now) else "."))
                _save(store, uid, b)
                notes.append(f"booked {uid}")
            elif b.get("skip"):
                continue
            elif b.get("start") != start_iso:
                send(f"Expert Session moved: {b.get('name') or 'guest'} is now {_when(start_iso)} "
                     f"(was {_when(b['start'])}).")
                b.update(start=start_iso, brief_sent=None, reminded=None)
                _save(store, uid, b)
                notes.append(f"moved {uid}")
            if b.get("skip"):
                continue
            if not b.get("brief_sent") and _brief_due(ev["start"], now):
                _send_brief(store, send, uid, b)
                notes.append(f"brief {uid}")
            if (not b.get("reminded") and ev["start"].date() == now.date() and now.hour >= 7
                    and ev["start"] > now):
                b["reminded"] = now.isoformat()
                _save(store, uid, b)
                send(f"Today at {ev['start'].astimezone(UK):%-I:%M%p}".replace("AM", "am").replace("PM", "pm")
                     + f": Expert Session with {b.get('name') or 'your guest'}."
                     + (f"\nVectera: {b['vectera']}" if b.get("vectera") else "")
                     + (f"\nAfter recording, reply: recorded {b['app_id']}" if b.get("app_id") else "")
                     + "\nYour Guest Brief is below again.")
                _send_brief(store, send, uid, b)
                notes.append(f"today {uid}")
        if first_run:
            store.r.set(BOOKED + ":init", "1")
            return f"first run: recorded {len(current)} existing entries, no alerts"
        for uid, b in known.items():
            if uid in current:
                continue
            try:
                future = datetime.fromisoformat(b["start"]) > now
            except Exception:
                future = False
            if future and not b.get("skip"):
                send(f"Expert Session cancelled: {b.get('name') or 'guest'}, {_when(b['start'])}. "
                     "That Thursday is free again." + _offer_waitlist(store, b["start"]))
                notes.append(f"cancelled {uid}")
            store.r.hdel(BOOKED, uid)
        return f"checked {len(events)} entries; " + (", ".join(notes) or "nothing new")
    finally:
        _lock.release()


# ---------------------------------------------------------------- waiting list
WAIT_TEXT = ("Good news: a Thursday Expert Session slot has just opened up on {when}.\n\n"
             "You're on our waiting list, so you're hearing first. Slots go to whoever books first, "
             "so if it suits you, book it now:\n{free}\n\nOr the Gift a Home option:\n{gift}\n\n"
             "If it doesn't suit, no need to reply. You stay on the list for the next one.")


def _offer_waitlist(store, start_iso: str) -> str:
    """Email everyone on the waiting list that a Thursday is free (first to book wins)."""
    if not store.r or not es._token():
        return ""
    wl = store.r.hgetall(WAITLIST)
    if not wl:
        return ""
    sent = 0
    for aid in sorted(wl, key=lambda k: wl[k]):
        r = es._cd("GET", f"/applications/{aid}")
        if r.status_code != 200:
            continue
        app = r.json().get("data") or {}
        out = es._email(app.get("contact") or {}, "A Well-Led Network Expert Session slot has opened",
                        WAIT_TEXT.format(when=_when(start_iso), free=es.FREE_URL, gift=es.GIFT_URL))
        sent += out == "emailed"
    return f"\nEmailed {sent} of {len(wl)} guest(s) on the waiting list about the free slot."


def _waitlist_add(store, aid: str) -> str:
    r = es._cd("GET", f"/applications/{aid}")
    if r.status_code != 200:
        return f"Couldn't find Clustdoc application {aid}."
    store.r.hset(WAITLIST, aid, datetime.now(UK).isoformat())
    es._tag(int(aid), "Expert Session - Waiting list")
    return (f"{es._name(r.json().get('data') or {})} ({aid}) is on the waiting list. "
            "They'll be emailed the moment a Thursday frees up.")


def handle(text: str, store, send) -> str | None:
    t = text.strip().lower()
    if t in ("waitlist", "waiting list"):
        wl = store.r.hgetall(WAITLIST) if store.r else {}
        return ("Waiting list (first come, first served):\n" + "\n".join(f"{k} since {v[:10]}" for k, v in
                sorted(wl.items(), key=lambda x: x[1]))) if wl else "Nobody is on the waiting list."
    w = re.match(r"^waitlist\s+(add|remove)\s+(\d+)$", t)
    if w and store.r:
        if w.group(1) == "remove":
            store.r.hdel(WAITLIST, w.group(2))
            return f"{w.group(2)} taken off the waiting list."
        return _waitlist_add(store, w.group(2))
    if t in ("briefs", "bookings experts", "expert bookings"):
        rows = sorted(((b["start"], b) for b in _all(store).values() if not b.get("skip")), key=lambda x: x[0])
        if not rows:
            return "No Expert Sessions booked yet."
        return "Booked Expert Sessions:\n" + "\n".join(
            f"{_when(s)}: {b.get('name') or 'guest'}" + (f" (brief {b['app_id']})" if b.get("app_id") else "")
            for s, b in rows)
    m = re.match(r"^brief\s+(\d+)$", t)
    if not m:
        return None
    for uid, b in _all(store).items():
        if str(b.get("app_id")) == m.group(1):
            threading.Thread(target=lambda: _send_brief(store, send, uid, b), daemon=True).start()
            return "Sending the Guest Brief now."
    return f"No booking found for Clustdoc {m.group(1)}. Reply briefs to see what is booked."


def check_in_background(store, send) -> None:
    threading.Thread(target=lambda: _safe(store, send), daemon=True).start()


def _safe(store, send) -> None:
    try:
        print("[expert brief]", check(store, send))
    except Exception as exc:
        print("[expert brief failed]", exc)
        if store.r and not store.r.set("wln:expert_brief_err", "1", nx=True, ex=6 * 3600):
            return
        try:
            send(f"Expert Session booking check hit a problem: {str(exc)[:300]}")
        except Exception:
            pass
