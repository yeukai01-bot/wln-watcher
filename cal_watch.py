"""SAM.AI booking alerts on Telegram, read from Google Calendar.

SAM.AI has no webhooks, but every SAM.AI booking is written into the kajidoricollective@gmail.com
Google Calendar. This module reads that calendar's private iCal address (GCAL_ICS_URL in Render,
from Google Calendar > Settings > the calendar > "Secret address in iCal format") and turns each
new SAM.AI booking into the same Telegram alerts Trafft bookings already get:
  new booking       -> "New booking" message with the booking form answers (+ call brief for Enrolment Calls)
  booking removed   -> "Cancelled" message
  time changed      -> "Rescheduled" message
  about an hour out -> "Call in 1 hour" reminder

It runs at most every 4 minutes, triggered by the wln-keep-awake ping (every 10 minutes, UK daytime),
and on demand at /calendar/check (X-Key header). The very first run only records what is already in the
calendar, so old events never flood the chat.
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
SEEN = "wln:cal_seen"          # uid -> {"start": iso, "reminded": bool}
LAST_RUN = "wln:cal_last_run"
SAM_ADMIN = "https://go.sam.ai/scheduler"
# Calendar entries that count as bookings: the SAM.AI meeting types.
MATCH = re.compile(r"enrol|strategy call|accelerator", re.I)
_lock = threading.Lock()


# ---------------------------------------------------------------- reading the iCal feed
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _dt(prop: str, value: str) -> datetime | None:
    value = value.strip()
    tz = re.search(r"TZID=([^;:]+)", prop)
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if "T" in value:
            d = datetime.strptime(value, "%Y%m%dT%H%M%S")
            return d.replace(tzinfo=ZoneInfo(tz.group(1)) if tz else UK)
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UK)
    except Exception:
        return None


def parse_ics(text: str) -> list[dict]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"attendees": []}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        prop, value = line.split(":", 1)
        name = prop.split(";", 1)[0].upper()
        if name == "UID":
            cur["uid"] = value.strip()
        elif name == "SUMMARY":
            cur["summary"] = _unescape(value).strip()
        elif name == "DESCRIPTION":
            cur["description"] = _unescape(value)
        elif name == "LOCATION":
            cur["location"] = _unescape(value).strip()
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
        elif name == "DTSTART":
            cur["start"] = _dt(prop, value)
        elif name == "ATTENDEE":
            cn = re.search(r"CN=\"?([^;:\"]+)", prop)
            email = value.replace("mailto:", "").replace("MAILTO:", "").strip()
            cur["attendees"].append({"name": cn.group(1).strip() if cn else "", "email": email})
        elif name == "X-GOOGLE-CONFERENCE":
            cur["meet"] = value.strip()
    return events


# ---------------------------------------------------------------- turning an event into a booking payload
def _answers(description: str) -> list[dict]:
    out = []
    for line in (description or "").splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        m = re.match(r"^([^:]{3,160}\?|[A-Z][^:]{1,60}):\s*(.+)$", line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if value.lower().startswith("http") and "link" not in label.lower():
            continue
        out.append({"label": label, "value": value})
    return out


def _meet(ev: dict) -> str:
    if ev.get("meet"):
        return ev["meet"]
    m = re.search(r"https://meet\.google\.com/[a-z\-]+", (ev.get("description") or "") + " " + (ev.get("location") or ""))
    return m.group(0) if m else ""


def to_payload(ev: dict, own_emails: set[str]) -> dict:
    guests = [a for a in ev.get("attendees", []) if a["email"].lower() not in own_emails]
    guest = guests[0] if guests else {"name": "", "email": ""}
    desc = ev.get("description") or ""
    answers = _answers(desc)

    def pick(*labels):
        for a in answers:
            if any(l in a["label"].lower() for l in labels):
                return a["value"]
        return ""

    summary = ev.get("summary") or ""
    service = "Enrolment Call: RI to Good Accelerator" if re.search(r"enrol|accelerator", summary + desc, re.I) else \
        ("Free CQC Strategy Call" if re.search(r"strategy", summary + desc, re.I) else summary)
    name = guest["name"] or pick("name") or re.sub(r".*(with|-|:)\s*", "", summary).strip()
    return {
        "appointmentId": "sam-" + ev["uid"],
        "serviceName": service,
        "customerFullName": name,
        "customerEmail": guest["email"] or pick("email"),
        "customerPhone": pick("phone"),
        "appointmentStartDateTime": ev["start"].astimezone(UK).isoformat() if ev.get("start") else "",
        "googleMeetUrl": _meet(ev),
        "customFields": [a for a in answers if not re.search(r"^(name|email|phone)$", a["label"], re.I)],
    }


# ---------------------------------------------------------------- the check
def _get_seen(store) -> dict | None:
    v = store.r.get(SEEN) if store.r else store._load().get("cal_seen")
    if v is None:
        return None
    return json.loads(v) if isinstance(v, str) else v


def _set_seen(store, seen: dict) -> None:
    if store.r:
        store.r.set(SEEN, json.dumps(seen))
    else:
        d = store._load()
        d["cal_seen"] = seen
        store._save(d)


def _trafft_same_time(store, start: datetime) -> bool:
    """Trafft bookings also land in the calendar; skip them so they are not announced twice."""
    import bookings

    for b in bookings.all_bookings(store):
        if str(b.get("id", "")).startswith("sam-") or not b.get("start"):
            continue
        try:
            if abs((datetime.fromisoformat(b["start"]) - start).total_seconds()) < 120:
                return True
        except Exception:
            pass
    return False


def check(store, send, force: bool = False) -> str:
    url = os.getenv("GCAL_ICS_URL", "").strip()
    if not url:
        return "GCAL_ICS_URL not set"
    if not _lock.acquire(blocking=False):
        return "already running"
    try:
        now = time.time()
        if not force and store.r:
            last = float(store.r.get(LAST_RUN) or 0)
            if now - last < 240:
                return "ran recently"
            store.r.set(LAST_RUN, str(now))
        import bookings

        r = requests.get(url, timeout=30)
        r.raise_for_status()
        own = {e.strip().lower() for e in os.getenv("CAL_OWN_EMAILS", "kajidoricollective@gmail.com,yeukai1@hotmail.co.uk,yeukai01@gmail.com").split(",")}
        nowdt = datetime.now(UK)
        events = [e for e in parse_ics(r.text)
                  if e.get("uid") and e.get("start") and MATCH.search((e.get("summary") or "") + " " + (e.get("description") or ""))
                  and nowdt - timedelta(hours=2) <= e["start"] <= nowdt + timedelta(days=90)]
        seen = _get_seen(store)
        first_run = seen is None
        seen = seen or {}
        current = {}
        notes = []
        for ev in events:
            uid = ev["uid"]
            cancelled = ev.get("status") == "CANCELLED"
            if cancelled:
                continue
            current[uid] = True
            start_iso = ev["start"].astimezone(UK).isoformat()
            prev = seen.get(uid)
            if first_run:
                seen[uid] = {"start": start_iso, "reminded": False, "skip": True}
                continue
            if prev is None:
                if _trafft_same_time(store, ev["start"]):
                    seen[uid] = {"start": start_iso, "reminded": True, "skip": True}
                    continue
                seen[uid] = {"start": start_iso, "reminded": False}
                bookings.handle(store, "booked", to_payload(ev, own), send)
                notes.append(f"new {uid}")
            elif not prev.get("skip") and prev.get("start") != start_iso:
                seen[uid] = {**prev, "start": start_iso, "reminded": False}
                bookings.handle(store, "rescheduled", to_payload(ev, own), send)
                notes.append(f"moved {uid}")
            # reminder about an hour before
            mins = (ev["start"] - nowdt).total_seconds() / 60
            if not seen[uid].get("skip") and not seen[uid].get("reminded") and 35 <= mins <= 75:
                seen[uid]["reminded"] = True
                bookings.handle(store, "reminder", to_payload(ev, own), send)
                notes.append(f"reminder {uid}")
        # bookings that disappeared (or were marked cancelled) before their start time
        for uid, info in list(seen.items()):
            if uid in current:
                continue
            try:
                start = datetime.fromisoformat(info.get("start"))
            except Exception:
                start = None
            if start and start > nowdt and not info.get("skip") and not info.get("sam_cancelled"):
                b = bookings.get(store, "sam-" + uid) or {}
                bookings.handle(store, "canceled", {"appointmentId": "sam-" + uid, "serviceName": b.get("service", ""),
                                                    "customerFullName": b.get("name", "")}, send)
                notes.append(f"cancelled {uid}")
            del seen[uid]
        if not first_run:
            notes += _sam_status(store, send, seen, nowdt)
        _set_seen(store, seen)
        if first_run:
            return f"first run: recorded {len(seen)} existing calendar bookings, no alerts sent"
        return "checked " + str(len(events)) + " events; " + (", ".join(notes) or "nothing new")
    finally:
        _lock.release()


# ---------------------------------------------------------------- SAM.AI status (cancellations made inside SAM.AI)
SAM_API = "https://go.sam.ai/api/v1/scheduler/appointments"
SAM_WARNED = "wln:sam_token_warned"


def _sam_start(a: dict) -> datetime | None:
    try:
        tz = ZoneInfo(a.get("timezone") or "Europe/London")
    except Exception:
        tz = UK
    try:
        d = datetime.strptime(f"{a['scheduled_date']} {a['start_time'][:5]}", "%Y-%m-%d %H:%M")
        return d.replace(tzinfo=tz).astimezone(UK)
    except Exception:
        return None


def _sam_status(store, send, seen: dict, nowdt: datetime) -> list[str]:
    """SAM.AI does not remove the Google Calendar entry when a booking is cancelled inside SAM.AI,
    so read SAM.AI's own list of appointments (SAM_API_TOKEN in Render) and announce cancellations."""
    token = os.getenv("SAM_API_TOKEN", "").strip()
    if not token:
        return []
    import bookings

    try:
        r = requests.get(SAM_API, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=30)
    except Exception as exc:
        print(f"[sam status failed] {exc}")
        return []
    if r.status_code in (401, 403):
        last = float(store.r.get(SAM_WARNED) or 0) if store.r else 0
        if time.time() - last > 24 * 3600:
            send("SAM.AI connection needs refreshing: cancellations made inside SAM.AI cannot be seen until the "
                 "SAM_API_TOKEN in Render is renewed. New bookings and calendar changes still arrive as normal.")
            if store.r:
                store.r.set(SAM_WARNED, str(time.time()))
        return []
    if r.status_code != 200:
        return []
    data = r.json().get("data")
    items = data.get("data") if isinstance(data, dict) else data
    notes = []
    for a in items or []:
        status = (a.get("status") or "").lower()
        if "cancel" not in status:
            continue
        start = _sam_start(a)
        if not start or start <= nowdt:
            continue
        for uid, info in seen.items():
            if info.get("skip") or info.get("sam_cancelled"):
                continue
            try:
                same = abs((datetime.fromisoformat(info["start"]) - start).total_seconds()) < 120
            except Exception:
                same = False
            if not same:
                continue
            info["sam_cancelled"] = True
            info["reminded"] = True
            b = bookings.get(store, "sam-" + uid) or {}
            bookings.handle(store, "canceled", {"appointmentId": "sam-" + uid, "serviceName": b.get("service", "") or a.get("title", ""),
                                                "customerFullName": b.get("name", "")}, send)
            send("SAM.AI cancelled this booking but may leave it in your Google Calendar. "
                 "You can delete that calendar entry; no further reminders will be sent for it.")
            notes.append(f"sam-cancelled {uid}")
    return notes


def check_in_background(store, send) -> None:
    threading.Thread(target=lambda: _safe(store, send), daemon=True).start()


def _safe(store, send) -> None:
    try:
        check(store, send)
    except Exception as exc:
        print(f"[calendar check failed] {type(exc).__name__}: {exc}")
