"""Trafft booking alerts on Telegram.

Trafft (ybc.admin.trafft.com > Integrations > Webhooks) posts to /trafft/<event> on the reply service:
  booked       a new appointment           -> "New booking" message with the booking form answers
  canceled     an appointment was canceled -> "Cancelled" message
  rescheduled  an appointment moved        -> "Rescheduled" message with the new time
  reminder     Trafft's "Appointment Schedule" webhook, set to fire before each call -> "Call soon" message
  status       status changed (approved, rejected, no-show...)

Every booking is kept in the shared store, so the morning run can list the day's calls and the
Telegram command "bookings" can list what is coming up.

Trafft does not publish its payload field names, so the parser looks for them by meaning
(start time, customer name, service...) wherever they sit in the JSON. If TRAFFT_TOKEN is set in
Render, requests must carry that token (Trafft sends its Verification Token in the Authorization header).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
BOOKINGS = "wln:bookings"
LAST_RAW = "wln:trafft_last"
ADMIN = "https://ybc.admin.trafft.com/appointments"


# ---------------------------------------------------------------- reading the payload
def _flatten(obj, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _find(flat: dict, *wanted: str, avoid: tuple[str, ...] = ()) -> str:
    """First non-empty value whose key (normalised, last path parts) matches a wanted name."""
    for w in wanted:
        w = _norm(w)
        for k, v in flat.items():
            if v in (None, "", [], {}):
                continue
            parts = [_norm(p) for p in re.split(r"[.\[\]]+", k) if p]
            tail = "".join(parts[-2:]) if len(parts) > 1 else parts[-1] if parts else ""
            last = parts[-1] if parts else ""
            nk = _norm(k)
            if any(a in nk for a in avoid):
                continue
            if re.search(r"custom(?!er)", nk):  # booking form answers are handled separately
                continue
            if last == w or tail == w or nk.endswith(w):
                return str(v).strip()
    return ""


def _custom_fields(payload) -> list[tuple[str, str]]:
    """Booking form answers. Trafft sends them as a list or dict under a key containing 'custom'."""
    found: list[tuple[str, str]] = []

    def walk(o, inside=False):
        if isinstance(o, dict):
            label = next((o[k] for k in o if _norm(k) in ("label", "name", "title", "question", "fieldname", "fieldlabel")
                          and isinstance(o[k], str)), None)
            value = next((o[k] for k in o if _norm(k) in ("value", "answer", "fieldvalue") and o[k] not in (None, "")), None)
            if inside and label and value is not None:
                found.append((label.strip(), ", ".join(map(str, value)) if isinstance(value, list) else str(value).strip()))
                return
            for k, v in o.items():
                nk = _norm(k)
                is_custom = "custom" in nk and "customer" not in nk
                now_inside = inside or is_custom
                if now_inside and isinstance(v, (str, int, float)) and not isinstance(v, bool) and not is_custom:
                    found.append((str(k), str(v)))
                else:
                    walk(v, now_inside)
        elif isinstance(o, list):
            for v in o:
                walk(v, inside)

    walk(payload)
    seen, out = set(), []
    for lab, val in found:
        if val and (lab, val) not in seen:
            seen.add((lab, val))
            out.append((lab, val))
    return out


def _parse_time(value: str, tz_name: str = "") -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d{10}(\d{3})?", v):  # unix seconds or milliseconds
        n = int(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UK)
    v = v.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M", "%B %d, %Y %I:%M %p",
                "%B %d, %Y %H:%M", "%d %B %Y %H:%M", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v, fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:  # Trafft works in the company time zone (London)
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name)) if tz_name else dt.replace(tzinfo=UK)
        except Exception:
            dt = dt.replace(tzinfo=UK)
    return dt.astimezone(UK)


def parse(payload: dict) -> dict:
    flat = _flatten(payload)
    tz = _find(flat, "timezone", "timeZone", "customerTimeZone")
    start_raw = _find(flat, "appointmentStartDateTime", "startDateTime", "appointmentStart", "bookingStart", "start_date_time",
                      "startsAt", "start", avoid=("employee", "service", "workhours"))
    if not start_raw:
        d = _find(flat, "appointmentStartDate", "startDate", "date")
        t = _find(flat, "appointmentStartTime", "startTime", "time")
        start_raw = f"{d} {t}".strip()
    start = _parse_time(start_raw, tz if "/" in tz else "")
    first = _find(flat, "customerFirstName", "customer.firstName", "firstName", avoid=("employee",))
    last = _find(flat, "customerLastName", "customer.lastName", "lastName", avoid=("employee",))
    name = _find(flat, "customerFullName", "customer.fullName", "customerName", avoid=("employee",)) or f"{first} {last}".strip()
    appt_id = _find(flat, "appointmentId", "appointment.id", "bookingId", "uuid", "id")
    b = {
        "id": appt_id or f"t{int(time.time())}",
        "service": _find(flat, "serviceName", "service.name", "service"),
        "name": name,
        "first_name": first or (name.split(" ")[0] if name else ""),
        "email": _find(flat, "customerEmail", "customer.email", "email", avoid=("employee", "company", "location")),
        "phone": _find(flat, "customerPhone", "customer.phone", "phone", avoid=("employee", "company", "location")),
        "status": _find(flat, "appointmentStatus", "status"),
        "start_raw": start_raw,
        "start": start.isoformat() if start else "",
        "meet": _find(flat, "googleMeetUrl", "meetingUrl", "zoomJoinUrl", "joinUrl", "onlineMeetingUrl", "meetLink"),
        "answers": _custom_fields(payload),
    }
    return b


# ---------------------------------------------------------------- storing
def _r(store):
    return store.r


def save(store, b: dict) -> None:
    if _r(store):
        store.r.hset(BOOKINGS, b["id"], json.dumps(b))
        # keep the store tidy: drop bookings more than 60 days old
        cutoff = datetime.now(UK) - timedelta(days=60)
        for k, v in store.r.hgetall(BOOKINGS).items():
            try:
                s = json.loads(v).get("start")
                if s and datetime.fromisoformat(s) < cutoff:
                    store.r.hdel(BOOKINGS, k)
            except Exception:
                pass
        return
    d = store._load()
    d.setdefault("bookings", {})[b["id"]] = b
    store._save(d)


def get(store, bid: str) -> dict | None:
    if _r(store):
        v = store.r.hget(BOOKINGS, bid)
        return json.loads(v) if v else None
    return store._load().get("bookings", {}).get(bid)


def all_bookings(store) -> list[dict]:
    if _r(store):
        items = [json.loads(v) for v in store.r.hgetall(BOOKINGS).values()]
    else:
        items = list(store._load().get("bookings", {}).values())
    return sorted(items, key=lambda b: b.get("start") or "")


def keep_raw(store, event: str, payload) -> None:
    try:
        txt = json.dumps({"event": event, "at": int(time.time()), "payload": payload})[:20000]
        if _r(store):
            store.r.set(LAST_RAW, txt)
    except Exception:
        pass


# ---------------------------------------------------------------- messages
def when(b: dict) -> str:
    if b.get("start"):
        dt = datetime.fromisoformat(b["start"]).astimezone(UK)
        return dt.strftime("%A %d %B, %-I:%M%p").replace("AM", "am").replace("PM", "pm") + " (UK)"
    return b.get("start_raw") or "time not given"


def _details(b: dict) -> str:
    lines = [f"{b.get('service') or 'Booking'}", f"When: {when(b)}", f"Who: {b.get('name') or 'name not given'}"]
    if b.get("email"):
        lines.append(f"Email: {b['email']}")
    if b.get("phone"):
        lines.append(f"Phone: {b['phone']}")
    for lab, val in b.get("answers", []):
        lines.append(f"{lab}: {val}")
    if b.get("meet"):
        lines.append(f"Join: {b['meet']}")
    return "\n".join(lines)


def _is_cqc_call(b: dict) -> bool:
    return bool(re.search(r"enrol|cqc|accelerator|strategy|readiness", (b.get("service") or ""), re.I))


def message(event: str, b: dict, old: dict | None = None) -> str:
    if event == "booked":
        prep = ("\n\nBefore the call: open their latest CQC report and find the finding they are most worried about."
                if _is_cqc_call(b) else "")
        return "New booking\n\n" + _details(b) + prep + f"\nAll bookings: {ADMIN}"
    if event == "canceled":
        return "Cancelled\n\n" + _details(b) + "\n\nThat slot is free again."
    if event == "rescheduled":
        was = f"\nWas: {when(old)}" if old and old.get("start") and old.get("start") != b.get("start") else ""
        return "Rescheduled\n\n" + _details(b) + was
    if event == "reminder":
        tip = ""
        if _is_cqc_call(b):
            tip = ("\n\n" + b["brief_short"] + "\nThe full call brief was sent when they booked.") if b.get("brief_short") \
                else "\n\nHave their CQC report open and the Accelerator checkout link ready."
        return "Call in 1 hour\n\n" + _details(b) + tip
    if event == "status":
        return f"Booking status changed to {b.get('status') or 'unknown'}\n\n" + _details(b)
    return f"Trafft update ({event})\n\n" + _details(b)


def handle(store, event: str, payload: dict, send) -> dict:
    keep_raw(store, event, payload)
    b = parse(payload if isinstance(payload, dict) else {"data": payload})
    old = get(store, b["id"])
    if old:  # keep answers from the original booking if a later event leaves them out
        if not b["answers"]:
            b["answers"] = old.get("answers", [])
        for k in ("name", "email", "phone", "service", "meet", "start", "start_raw", "brief_short"):
            b[k] = b.get(k) or old.get(k, "")
    if event == "canceled":
        b["status"] = "canceled"
    elif event in ("booked", "rescheduled") and not b.get("status"):
        b["status"] = "approved"
    if event != "reminder" or not old:
        save(store, {**(old or {}), **b})
    send(message(event, b, old))
    if event == "booked" and _is_cqc_call(b) and re.search(r"enrol", b.get("service") or "", re.I):
        import call_brief
        import telegram_io

        call_brief.start(b, store, telegram_io)
    return b


def upcoming(store, days: int = 14) -> list[dict]:
    now = datetime.now(UK)
    out = []
    for b in all_bookings(store):
        if not b.get("start") or "cancel" in (b.get("status") or "").lower() or "reject" in (b.get("status") or "").lower():
            continue
        s = datetime.fromisoformat(b["start"])
        if now - timedelta(minutes=30) <= s <= now + timedelta(days=days):
            out.append(b)
    return out


def list_text(store) -> str:
    items = upcoming(store)
    if not items:
        return "No calls booked in the next two weeks."
    return "Coming up:\n" + "\n".join(f"- {when(b)}: {b.get('name') or '?'} ({b.get('service') or 'booking'})" for b in items)


def todays_calls(store) -> str:
    """For the morning run: today's calls, or an empty string when there are none."""
    today = datetime.now(UK).date()
    items = [b for b in upcoming(store, 1) if datetime.fromisoformat(b["start"]).astimezone(UK).date() == today]
    if not items:
        return ""
    return "Your calls today\n\n" + "\n\n".join(_details(b) for b in items)
