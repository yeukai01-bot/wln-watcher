"""Automatic follow-ups for CQC report radar prospects, sent from Yeukai's own Gmail.

After a radar prospect has been sent the free Report Review, up to three short follow-ups go out as
replies in the same Gmail thread:
  day 2   did the review open, which of the three points matters most
  day 7   the two CQC clocks (action plan, rating review) and a free check of their draft
  day 28  the plan should be with CQC: one habit that makes the evidence build itself
  day 45  how is the action plan going? offer help to implement it (last note)

Safety rules:
  * A follow-up only goes if the first email is really in the Gmail Sent folder (checked over IMAP).
  * Any email from that person, or anyone at their (non free-mail) domain, since the first email stops the
    series for good. So does a bounce, a 'remove' request, or an address on the radar's suppression list.
  * Only services with a company number (can_email) are followed up, as for the first email.
  * Weekdays only. At 08:30 UK Yeukai gets the day's list on Telegram; at 10:30 the ones he did not hold go out.
  * At most 3 days' gap between follow-ups is enforced as a minimum, and at most DAILY_CAP sends a day.

Needs on Render: GMAIL_APP_PASSWORD (a Google app password for GMAIL_ACCOUNT). Without it nothing is sent.

Telegram:
  fu            what is planned and what has gone out
  fu check      work out today's list now (sends nothing)
  fu preview F2 shows the exact email for F2 on today's list
  hold F1 F3    keeps those off today's send (they come back tomorrow)
  fu stop F2    stops the series for that person for good (also: fu stop name@example.com)
  fu pause / fu resume   stops or restarts all follow-ups
  fu older      one-off: lists older radar prospects (before the 45-day window) who never replied, for a single
                "how is the action plan going?" email. Nothing is sent until "fu older go"; then 15 go each weekday
                at 10:45 (each re-checked for a reply first). "fu older stop" pauses it.
"""
from __future__ import annotations

import email
import email.header
import imaplib
import json
import os
import re
import smtplib
import ssl
import threading
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parsedate_to_datetime
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
ACCOUNT = os.getenv("GMAIL_ACCOUNT", "kajidoricollective@gmail.com")
STATE = "wln:followups"
BATCH = "wln:fu_batch"
FLAGS = "wln:fu_flags"
OLDER = "wln:fu_older"
OLDER_PER_DAY = int(os.getenv("FOLLOWUP_OLDER_PER_DAY", "15"))
ANNOUNCE_AT = (8, 30)
SEND_AT = (10, 30)
DAILY_CAP = int(os.getenv("FOLLOWUP_DAILY_CAP", "20"))
LOOKBACK_DAYS = 45
MIN_GAP_DAYS = 3
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "yahoo.com", "yahoo.co.uk",
             "live.com", "live.co.uk", "icloud.com", "me.com", "aol.com", "btinternet.com", "sky.com", "msn.com"}

SIGNATURE = ("Kind regards\nYeukai Kajidori\nFounder, Well-Led Network\nCreator of the Well-Led and Safe Supported Living "
             "Framework\nhttps://www.welllednetwork.com/blog\nWork with me: https://shor.by/wln-work-with-me\n"
             "The Kajidori Collective Ltd")
WHATSAPP_PS = ("P.S. Want quick answers to your CQC questions? Join the Well-Led Network, my free WhatsApp group for UK "
               "care leaders. Managers post their questions and I answer them there: https://shor.by/wln-whatsapp")
OPT_OUT = "If you'd rather not hear from me again, just reply 'remove' and I won't contact you."

STEPS = [("fu1", 2), ("fu2", 7), ("fu3", 28), ("fu4", 45)]
NEXT = {"fu1": "fu2", "fu2": "fu3", "fu3": "fu4"}
LAST = "fu4"
STEP_NAMES = {"fu1": "day 2, which point matters most", "fu2": "day 7, the CQC dates + free draft check",
              "fu3": "day 28, one habit once the plan is in", "fu4": "day 45, how is the plan going (last note)"}

_lock = threading.Lock()
_bg = {"t": 0.0}


def enabled() -> bool:
    return bool(os.getenv("GMAIL_APP_PASSWORD", "").strip())


# ------------------------------------------------------------------ storage (Redis or the local file)

def _get(store, key: str, default):
    if store.r:
        v = store.r.get(key)
        return json.loads(v) if v else default
    return store._load().get(key, default)


def _set(store, key: str, value) -> None:
    if store.r:
        store.r.set(key, json.dumps(value))
        return
    d = store._load()
    d[key] = value
    store._save(d)


def states(store) -> dict:
    return _get(store, STATE, {})


def put_state(store, s: dict) -> None:
    with _lock:
        allst = states(store)
        allst[s["email"]] = s
        _set(store, STATE, allst)


def flags(store) -> dict:
    return _get(store, FLAGS, {})


# ------------------------------------------------------------------ dates

def parse_report_date(text: str) -> date | None:
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            pass
    return None


def add_working_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def nice(d: date) -> str:
    return f"{d.day} {d:%B}"


# ------------------------------------------------------------------ the emails

def _hello(s: dict) -> str:
    words = [w for w in (s.get("name") or "").replace(".", "").split() if w.lower() not in ("mr", "mrs", "ms", "miss", "dr", "mx")]
    first = words[0].strip() if words else ""
    return f"Hi {first}," if first else f"Hello {s.get('service') or 'there'} team,"


def build(step: str, s: dict, today: date) -> str | None:
    """The body for one follow-up, or None if it no longer makes sense to send it."""
    parts = [_hello(s)]
    if step == "fu1":
        if s.get("review_url"):
            parts += ["I know the week after a CQC report lands is a heavy one, so here is the free review I sent you "
                      f"again, in case it got buried:\n\n{s['review_url']}"]
        else:
            parts += ["I know the week after a CQC report lands is a heavy one, so I wanted to check the free review I "
                      "sent you reached you. If you can't find it, just reply and I'll send it straight back."]
        parts += ["It picks out the three things I would fix first in the next 30 days. If you have a minute, I'd love "
                  "to know which of the three feels most pressing for you.",
                  "If it's easier to talk than type, reply with a time that suits and I'll call you. Twenty minutes is plenty."]
    elif step == "fu2":
        rd = parse_report_date(s.get("report_date", ""))
        if not rd:
            return None
        ap, rr = rd + timedelta(days=28), add_working_days(rd, 15)
        points = []
        if ap > today:
            points.append(f"Action plan. Where CQC found breaches, they expect your plan within 28 days. "
                          f"For you that's about {nice(ap)}.")
        if rr > today:
            points.append(f"Rating review. If you believe CQC didn't follow its own process, you can ask for a review "
                          f"within 15 working days. For you that's about {nice(rr)}.")
        if not points:
            return None
        lead = "two clocks" if len(points) == 2 else "a clock that is still running"
        parts += [f"One quick thing so it doesn't pass you by. Your report was published on {nice(rd)}, and that "
                  f"starts {lead}:",
                  "\n".join(f"{i}. {p}" for i, p in enumerate(points, 1)),
                  "Your report letter confirms the exact dates.",
                  "The plans that land well with CQC do three things. They lead with what you've already changed since "
                  "the inspection. They give every action a named owner and a date. And they say how you'll show the "
                  "change has stuck.",
                  "If it would help, send me your draft before you submit and I'll send back written comments, free of charge."]
    elif step == "fu3":
        parts += ["By now your action plan is probably with CQC. Well done for getting it in.",
                  "One habit makes the next part much easier: every Friday, take ten minutes with each action owner, "
                  "ask \"what changed this week, and where is the proof?\", and file that proof straight away against "
                  "the action it belongs to. When CQC comes back, the evidence is already sitting there, instead of a "
                  "last-minute scramble.",
                  "If you'd like a hand setting that up for your team, just reply and we'll find 20 minutes."]
    elif step == "fu4":
        parts += ["I wanted to check in now your action plan has had a few weeks to bed in. How are you getting on with it?",
                  "In my experience plans usually stall in one of three places: the same one or two people own every action, "
                  "the work is happening but nobody is filing the evidence, or the wider team still doesn't know what has "
                  "changed. If any of that sounds familiar, you're not alone, and it's very fixable.",
                  "That's exactly the work I do with providers: helping them deliver the plan they've written, week by week, "
                  "so that when CQC returns the improvement is clear to see. It's built on the same habits that took a "
                  "service I led from a local authority embargo back to green.",
                  f"If you'd like to see how that could look for {s.get('service') or 'your service'}, everything is here: "
                  "https://shor.by/wln-work-with-me",
                  "Or just reply \"yes\" and we'll find 20 minutes to talk it through. This is my last note for now, "
                  "and either way I wish you and the team well."]
    elif step == "old":
        parts += [f"A while back I sent you a short, free review of the CQC report for {s.get('service') or 'your service'}. "
                  "I wanted to check in now some time has passed. How are you getting on with the action plan?",
                  "In my experience plans usually stall in one of three places: the same one or two people own every action, "
                  "the work is happening but nobody is filing the evidence, or the wider team still doesn't know what has "
                  "changed. If any of that sounds familiar, you're not alone, and it's very fixable.",
                  "That's exactly the work I do with providers: helping them deliver the plan they've written, week by week, "
                  "so that when CQC returns the improvement is clear to see. It's built on the same habits that took a "
                  "service I led from a local authority embargo back to green.",
                  f"If you'd like to see how that could look for {s.get('service') or 'your service'}, everything is here: "
                  "https://shor.by/wln-work-with-me",
                  "Or just reply \"yes\" and we'll find 20 minutes to talk it through. I won't follow up again after this, "
                  "and either way I wish you and the team well."]
    else:
        return None
    if step not in (LAST, "old"):
        parts.append(OPT_OUT)
    parts += [SIGNATURE, WHATSAPP_PS]
    return "\n\n".join(parts)


def subject_for(s: dict) -> str:
    subj = (s.get("orig_subject") or "").strip() or f"Your CQC report, {s.get('service', '')}".strip(", ")
    return subj if re.match(r"^re:", subj, re.I) else f"Re: {subj}"


# ------------------------------------------------------------------ Gmail (IMAP to check, SMTP to send)

def _imap():
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
    m.login(ACCOUNT, os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", ""))
    typ, boxes = m.list()
    allmail = None
    for b in boxes or []:
        line = b.decode(errors="ignore")
        if "\\All" in line:
            allmail = line.split(' "/" ')[-1].strip()
            break
    m.select(allmail or '"[Gmail]/All Mail"', readonly=True)
    return m


def _search(m, raw: str) -> list[bytes]:
    typ, data = m.search(None, "X-GM-RAW", '"' + raw.replace('"', "") + '"')
    return (data[0].split() if data and data[0] else []) if typ == "OK" else []


def _headers(m, num: bytes) -> email.message.Message:
    typ, data = m.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE TO FROM)])")
    return email.message_from_bytes(data[0][1])


def find_first_email(m, addr: str, since: date) -> dict | None:
    """The first email Yeukai sent to this address on or after since (from his Sent mail)."""
    nums = _search(m, f"in:sent to:{addr} after:{since:%Y/%m/%d}")
    best = None
    for n in nums:
        h = _headers(m, n)
        try:
            when = parsedate_to_datetime(h.get("Date"))
        except Exception:
            continue
        if best is None or when < best["when"]:
            best = {"when": when, "msgid": (h.get("Message-ID") or "").strip(), "subject": h.get("Subject") or ""}
    if not best:
        return None
    subj = str(email.header.make_header(email.header.decode_header(best["subject"])))
    return {"first_sent": int(best["when"].timestamp()), "orig_msgid": best["msgid"], "orig_subject": subj}


def has_reply(m, addr: str, since_ts: int) -> str | None:
    """Why the series must stop (a reply, a bounce), or None."""
    since = datetime.fromtimestamp(since_ts, UK).date() - timedelta(days=1)
    dom = addr.split("@")[-1].lower()
    who = f"from:{addr}" if dom in FREE_MAIL else f"from:{dom}"
    if _search(m, f"{who} -in:sent after:{since:%Y/%m/%d}"):
        return "replied"
    if _search(m, f"from:mailer-daemon {addr} after:{since:%Y/%m/%d}"):
        return "bounced"
    return None


def send_email(s: dict, body: str) -> str:
    msg = EmailMessage()
    msg["From"] = formataddr(("Yeukai Kajidori", ACCOUNT))
    msg["To"] = s["email"]
    msg["Subject"] = subject_for(s)
    msg["Message-ID"] = make_msgid(domain="mail.gmail.com")
    if s.get("orig_msgid"):
        msg["In-Reply-To"] = s["orig_msgid"]
        msg["References"] = " ".join(x for x in (s.get("refs") or [s["orig_msgid"]]) if x)
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(ACCOUNT, os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", ""))
        smtp.send_message(msg)
    return msg["Message-ID"]


# ------------------------------------------------------------------ planning

def next_step(s: dict, today: date) -> str | None:
    if s.get("stopped"):
        return None
    first = datetime.fromtimestamp(s["first_sent"], UK).date()
    age = (today - first).days
    done = s.get("steps", {})
    last = max([v for v in done.values() if isinstance(v, int)] or [s["first_sent"]])
    if (today - datetime.fromtimestamp(last, UK).date()).days < MIN_GAP_DAYS and done:
        return None
    for step, day in STEPS:
        if step in done:
            continue
        if age < day:
            return None
        nxt = dict(STEPS).get(NEXT.get(step, ""), 999)
        if step != LAST and age >= nxt:
            continue  # catching up: skip a step whose moment has passed and go to the next one
        if build(step, s, today) is None:
            continue
        return step
    return None


def plan(store, today: date | None = None, imap_factory=None) -> list[dict]:
    """Bring every radar prospect's state up to date and return today's due follow-ups."""
    today = today or datetime.now(UK).date()
    suppressed = set(e.lower() for e in store.suppressed())
    allst = states(store)
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    candidates = {}
    for p in store.prospects():
        e = (p.get("email") or "").strip().lower()
        if not e or not p.get("can_email") or p.get("created", 0) < cutoff:
            continue
        candidates.setdefault(e, p)
    due = []
    m = (imap_factory or _imap)()
    try:
        for e, p in candidates.items():
            s = allst.get(e) or {"email": e, "steps": {}}
            s.update({"name": p.get("registered_manager") or s.get("name", ""), "service": p.get("location_name", ""),
                      "report_date": p.get("report_date", ""), "review_url": p.get("review_url", ""),
                      "code": p.get("code", ""), "prospect_id": p.get("id")})
            if s.get("stopped"):
                continue
            if e in suppressed:
                s["stopped"] = "asked not to be contacted"
                put_state(store, s)
                continue
            if not s.get("first_sent"):
                rd = parse_report_date(p.get("report_date", "")) or datetime.fromtimestamp(p.get("created", time.time()), UK).date()
                found = find_first_email(m, e, rd - timedelta(days=1))
                if not found:
                    continue  # the first email has not gone yet, so no follow-up
                s.update(found)
            why = has_reply(m, e, s["first_sent"])
            if why:
                s["stopped"] = why
                put_state(store, s)
                continue
            put_state(store, s)
            step = next_step(s, today)
            if step:
                due.append({"email": e, "step": step})
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return due[:DAILY_CAP]


# ------------------------------------------------------------------ one-off check-in for older prospects

def older_candidates(store, imap_factory=None) -> list[str]:
    """Radar prospects from before the 45-day window who were emailed, never replied and never had a follow-up."""
    suppressed = set(e.lower() for e in store.suppressed())
    allst = states(store)
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    cands = {}
    for p in store.prospects():
        e = (p.get("email") or "").strip().lower()
        if e and p.get("can_email") and p.get("created", 0) < cutoff and e not in suppressed:
            cands.setdefault(e, p)
    out = []
    m = (imap_factory or _imap)()
    try:
        for e, p in cands.items():
            s = allst.get(e) or {"email": e, "steps": {}}
            if s.get("stopped") or s.get("steps"):
                continue
            s.update({"name": p.get("registered_manager") or s.get("name", ""), "service": p.get("location_name", ""),
                      "report_date": p.get("report_date", ""), "review_url": p.get("review_url", ""),
                      "code": p.get("code", ""), "prospect_id": p.get("id")})
            if not s.get("first_sent"):
                rd = parse_report_date(p.get("report_date", "")) or datetime.fromtimestamp(p.get("created", time.time()), UK).date()
                found = find_first_email(m, e, rd - timedelta(days=1))
                if not found:
                    continue
                s.update(found)
            why = has_reply(m, e, s["first_sent"])
            if why:
                s["stopped"] = why
                put_state(store, s)
                continue
            put_state(store, s)
            out.append(e)
    finally:
        try:
            m.logout()
        except Exception:
            pass
    out.sort(key=lambda e: states(store).get(e, {}).get("first_sent", 0), reverse=True)
    return out


def send_older(store, send, today: date, imap_factory=None, sender=None) -> str:
    o = _get(store, OLDER, {})
    o["last_run"] = today.isoformat()
    _set(store, OLDER, o)
    done = set(o.get("done", []))
    todo = [e for e in o.get("queue", []) if e not in done][:OLDER_PER_DAY]
    if not todo:
        o["approved"] = False
        _set(store, OLDER, o)
        return "older batch finished"
    sender = sender or send_email
    allst, sent, skipped = states(store), [], []
    m = (imap_factory or _imap)()
    try:
        for e in todo:
            done.add(e)
            s = allst.get(e)
            if not s or s.get("stopped") or s.get("steps"):
                continue
            why = has_reply(m, e, s["first_sent"])
            if why:
                s["stopped"] = why
                put_state(store, s)
                skipped.append(f"{s.get('name') or e} ({why})")
                continue
            try:
                mid = sender(s, build("old", s, today))
            except Exception as exc:
                skipped.append(f"{s.get('name') or e} (send failed: {type(exc).__name__})")
                continue
            s.setdefault("steps", {})["old"] = int(time.time())
            s["stopped"] = "older one-off check-in sent"
            s["refs"] = [x for x in (s.get("refs") or [s.get("orig_msgid")]) if x] + [mid]
            put_state(store, s)
            sent.append(f"{s.get('name') or e}, {s.get('service', '')}")
            time.sleep(20)
    finally:
        try:
            m.logout()
        except Exception:
            pass
    o["done"] = sorted(done)
    left = len([e for e in o.get("queue", []) if e not in done])
    if not left:
        o["approved"] = False
    _set(store, OLDER, o)
    send(("Older one-off check-ins sent from Gmail:\n" + "\n".join(sent) if sent else "No older check-ins sent today.")
         + ("\n\nNot sent: " + ", ".join(skipped) if skipped else "")
         + (f"\n\n{left} still to go, next weekday at 10:45." if left else "\n\nThat was the last of the older list."))
    return f"older sent {len(sent)}"


# ------------------------------------------------------------------ the daily rhythm

def _batch_text(store, batch: dict, heading: str) -> str:
    allst = states(store)
    lines = []
    for it in batch["items"]:
        s = allst.get(it["email"], {})
        mark = "  [held]" if it.get("held") else ("  [sent]" if it.get("sent") else "")
        lines.append(f"{it['code']}. {s.get('name') or '(no name)'}, {s.get('service', '')} ({it['email']}): "
                     f"{STEP_NAMES[it['step']]}{mark}")
    return heading + "\n" + "\n".join(lines)


def announce(store, send, today: date) -> str:
    due = plan(store, today)
    batch = {"date": today.isoformat(), "items": [{**d, "code": f"F{i}"} for i, d in enumerate(due, 1)], "sent_done": False}
    _set(store, BATCH, batch)
    if not due:
        return "no follow-ups due"
    send(_batch_text(store, batch, f"Radar follow-ups going out at 10:30 today ({len(due)}), as replies in each Gmail thread:")
         + "\n\nReply hold F2 to keep one back today, fu stop F2 to end that series, fu preview F1 to read one. "
           "Anyone who has replied, bounced or asked to be removed is already taken off.")
    return f"announced {len(due)}"


def send_batch(store, send, today: date, imap_factory=None, sender=None) -> str:
    batch = _get(store, BATCH, {})
    if batch.get("date") != today.isoformat() or batch.get("sent_done"):
        return "nothing to send"
    batch["sent_done"] = True
    _set(store, BATCH, batch)
    allst, sent, skipped = states(store), [], []
    sender = sender or send_email
    m = (imap_factory or _imap)()
    try:
        for it in batch["items"]:
            if it.get("held"):
                continue
            s = allst.get(it["email"])
            if not s or s.get("stopped") or it["step"] in s.get("steps", {}):
                continue
            why = has_reply(m, s["email"], s["first_sent"])  # last-minute check
            if why:
                s["stopped"] = why
                put_state(store, s)
                skipped.append(f"{it['code']} ({why})")
                continue
            body = build(it["step"], s, today)
            if not body:
                continue
            try:
                mid = sender(s, body)
            except Exception as exc:
                skipped.append(f"{it['code']} (send failed: {type(exc).__name__})")
                continue
            s.setdefault("steps", {})[it["step"]] = int(time.time())
            s["refs"] = [x for x in (s.get("refs") or [s.get("orig_msgid")]) if x] + [mid]
            put_state(store, s)
            it["sent"] = True
            sent.append(f"{it['code']} {s.get('name') or s['email']}, {s.get('service', '')}")
            time.sleep(20)  # space the sends out
    finally:
        try:
            m.logout()
        except Exception:
            pass
    _set(store, BATCH, batch)
    if sent or skipped:
        send(("Radar follow-ups sent from Gmail:\n" + "\n".join(sent) if sent else "No radar follow-ups sent today.")
             + ("\n\nNot sent: " + ", ".join(skipped) if skipped else ""))
    return f"sent {len(sent)}"


def tick(store, send, now: datetime | None = None) -> str:
    now = now or datetime.now(UK)
    if not enabled() or flags(store).get("paused") or now.weekday() >= 5:
        return "off"
    today = now.date()
    batch = _get(store, BATCH, {})
    hm = (now.hour, now.minute)
    if hm >= ANNOUNCE_AT and batch.get("date") != today.isoformat():
        return announce(store, send, today)
    if hm >= SEND_AT and batch.get("date") == today.isoformat() and not batch.get("sent_done"):
        return send_batch(store, send, today)
    older = _get(store, OLDER, {})
    if hm >= (SEND_AT[0], SEND_AT[1] + 15) and older.get("approved") and older.get("last_run") != today.isoformat():
        return send_older(store, send, today)
    return "waiting"


def check_in_background(store, send, every_s: int = 540) -> None:
    with _lock:
        if time.time() - _bg["t"] < every_s:
            return
        _bg["t"] = time.time()

    def go():
        try:
            print("[radar follow-ups]", tick(store, send))
        except Exception as exc:
            print("radar follow-ups failed:", type(exc).__name__, exc)
            try:
                send(f"Radar follow-ups hit a problem and sent nothing: {type(exc).__name__}: {str(exc)[:200]}")
            except Exception:
                pass

    threading.Thread(target=go, daemon=True).start()


# ------------------------------------------------------------------ Telegram commands

def _item(batch: dict, code: str) -> dict | None:
    code = code.upper() if code.upper().startswith("F") else "F" + code
    return next((i for i in batch.get("items", []) if i["code"] == code), None)


def handle(text: str, store) -> str | None:
    t = (text or "").strip()
    low = t.lower()
    if not (re.match(r"^(fu|followups?)\b", low) or re.match(r"^hold\s+f\d", low)):
        return None
    if not enabled():
        return "Radar follow-ups are built but not switched on: add GMAIL_APP_PASSWORD in Render (wln-secrets)."
    batch = _get(store, BATCH, {})
    today = datetime.now(UK).date()
    h = re.match(r"^hold\s+(.+)$", low)
    if h:
        out = []
        for c in re.findall(r"f?\d+", h.group(1)):
            it = _item(batch, c)
            if it and batch.get("date") == today.isoformat():
                it["held"] = True
                out.append(it["code"])
        _set(store, BATCH, batch)
        return ("Held today: " + ", ".join(out) + ". They come back on tomorrow's list.") if out else "None of those are on today's list."
    st = re.match(r"^(?:fu|followups?)\s+stop\s+(\S+)$", low)
    if st:
        key = st.group(1)
        allst = states(store)
        if "@" in key:
            s = allst.get(key.strip(".,;")) or {"email": key.strip(".,;"), "steps": {}}
        else:
            it = _item(batch, key)
            s = allst.get(it["email"]) if it else None
        if not s:
            return "I could not find that one."
        s["stopped"] = "stopped by Yeukai"
        put_state(store, s)
        return f"Stopped for good: {s.get('name') or s['email']}, {s.get('service', '')}."
    pv = re.match(r"^(?:fu|followups?)\s+preview\s+(f?\d+)$", low)
    if pv:
        it = _item(batch, pv.group(1))
        s = states(store).get(it["email"]) if it else None
        if not s:
            return "That code is not on today's list."
        return f"To: {s['email']}\nSubject: {subject_for(s)}\n\n{build(it['step'], s, today) or '(nothing to send)'}"
    if re.match(r"^(?:fu|followups?)\s+pause$", low):
        f = flags(store); f["paused"] = True; _set(store, FLAGS, f)
        return "All radar follow-ups paused. fu resume starts them again."
    if re.match(r"^(?:fu|followups?)\s+resume$", low):
        f = flags(store); f["paused"] = False; _set(store, FLAGS, f)
        return "Radar follow-ups back on."
    if re.match(r"^(?:fu|followups?)\s+check$", low):
        def go():
            import telegram_io
            try:
                due = plan(store, today)
                allst = states(store)
                if not due:
                    telegram_io.send_message("Follow-up check: nothing due today.")
                else:
                    telegram_io.send_message("Follow-up check (nothing sent):\n" + "\n".join(
                        f"{allst[d['email']].get('name') or d['email']}, {allst[d['email']].get('service', '')}: {STEP_NAMES[d['step']]}"
                        for d in due))
            except Exception as exc:
                telegram_io.send_message(f"Follow-up check failed: {type(exc).__name__}: {str(exc)[:200]}")
        threading.Thread(target=go, daemon=True).start()
        return "Checking Gmail for who is due a follow-up. It takes a minute; nothing will be sent."
    if re.match(r"^(?:fu|followups?)\s+older\s+go$", low):
        o = _get(store, OLDER, {})
        if not o.get("queue"):
            return "There is no older list yet. Send fu older first to see who is on it."
        o["approved"] = True
        _set(store, OLDER, o)
        left = [e for e in o["queue"] if e not in o.get("done", [])]
        return (f"Approved. {len(left)} older prospects will get the one-off check-in, {OLDER_PER_DAY} each weekday at "
                "10:45, each re-checked for a reply first. fu older stop pauses it.")
    if re.match(r"^(?:fu|followups?)\s+older\s+stop$", low):
        o = _get(store, OLDER, {})
        o["approved"] = False
        _set(store, OLDER, o)
        return "Older one-off check-ins paused. fu older go starts them again."
    if re.match(r"^(?:fu|followups?)\s+older$", low):
        def go_older():
            import telegram_io
            try:
                emails = older_candidates(store)
                o = _get(store, OLDER, {})
                o.update({"queue": emails, "approved": False})
                o.setdefault("done", [])
                _set(store, OLDER, o)
                allst = states(store)
                if not emails:
                    telegram_io.send_message("Older radar prospects: nobody found who was emailed and never replied.")
                    return
                lines = [f"{i}. {allst[e].get('name') or '(no name)'}, {allst[e].get('service', '')} ({e}), first email "
                         f"{datetime.fromtimestamp(allst[e]['first_sent'], UK):%d %b}" for i, e in enumerate(emails, 1)]
                telegram_io.send_message(
                    f"Older radar prospects who got your free review and never replied ({len(emails)}). Each would get ONE "
                    "email, as a reply in the original Gmail thread: how is the action plan going, the three places plans "
                    "stall, and your offer to help them implement it.\n\n"
                    + "\n\n".join("\n".join(lines[i:i + 25]) for i in range(0, len(lines), 25))
                    + f"\n\nNothing has been sent. Reply fu older go to send them, {OLDER_PER_DAY} a weekday at 10:45. "
                      "fu stop name@example.com takes anyone off first. fu older preview 1 shows the email.")
            except Exception as exc:
                telegram_io.send_message(f"Older prospects check failed: {type(exc).__name__}: {str(exc)[:200]}")
        threading.Thread(target=go_older, daemon=True).start()
        return "Looking through your Gmail for older radar prospects who never replied. The list comes in a minute or two; nothing will be sent."
    opv = re.match(r"^(?:fu|followups?)\s+older\s+preview\s+(\d+)$", low)
    if opv:
        o = _get(store, OLDER, {})
        q = o.get("queue", [])
        n = int(opv.group(1))
        if not 1 <= n <= len(q):
            return "That number is not on the older list."
        s = states(store).get(q[n - 1])
        return f"To: {s['email']}\nSubject: {subject_for(s)}\n\n{build('old', s, today)}"
    # status
    allst = states(store)
    active = [s for s in allst.values() if s.get("first_sent") and not s.get("stopped")]
    stopped = [s for s in allst.values() if s.get("stopped")]
    msg = f"Radar follow-ups: {len(active)} in the series, {len(stopped)} stopped (replied, bounced or removed)."
    if flags(store).get("paused"):
        msg += " PAUSED (fu resume to restart)."
    if batch.get("items"):
        msg += "\n\n" + _batch_text(store, batch, f"List for {batch['date']}:")
    return msg
