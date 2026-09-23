"""The reply service: lets Yeukai approve topics by replying on Telegram.

Telegram sends each message Yeukai types to /telegram/<WEBHOOK_SECRET>. Only messages from
TELEGRAM_CHAT_ID are accepted. Replies understood:
  "1 3"       approve topics 1 and 3 from the latest morning list
  "queue"     show what is approved and waiting to be published
  "cancel 2"  withdraw topic 2 if it has not been published yet
  "help"      show these instructions

The scheduled Claude publishing task reads approved topics from /approvals and reports back to
/approvals/<id>/done (both need the APPROVALS_KEY). Nothing here publishes anything.
"""
from __future__ import annotations

import hmac
import os
import re
import threading
import time

from flask import Flask, abort, jsonify, request

import ai
import telegram_io as telegram
from store import Store

app = Flask(__name__)
store = Store()

HELP = (
    "How to use this chat:\n"
    "Reply with topic numbers from this morning's list, e.g. 1 3, to approve them for publishing.\n"
    "queue shows what is waiting.\n"
    "cancel 2 withdraws topic 2 if it has not gone live.\n"
    "write <link> asks for an article on any page you have found, e.g. write https://www.cqc.org.uk/news/... You can add a note after the link.\n"
    "Approved topics are written, checked and published at the next publishing run, and you get the live link here.\n\n"
    "Report radar (CQC outreach):\n"
    "Tap Open email under a draft to open it in your mail app, ready to send. You press Send.\n"
    "sent P1 P3 records that you sent those.\n"
    "leads shows today's radar list and what has been sent.\n"
    "remove name@example.com stops the radar ever drafting to that address again."
)


def _key_ok() -> bool:
    expected = os.getenv("APPROVALS_KEY", "")
    given = request.headers.get("X-Key") or request.args.get("key", "")
    return bool(expected) and hmac.compare_digest(expected, given)


def _draft_in_background(aid: str) -> None:
    a = store.get_approval(aid)
    if not a or a.get("draft"):
        return
    try:
        md, check, problem = ai.draft_and_check(a)
    except Exception as exc:  # report, never crash the web service
        md, check, problem = None, None, f"Drafting failed: {type(exc).__name__}: {str(exc)[:200]}"
    a = store.get_approval(aid) or a
    if md:
        a["draft"] = md
        a["fact_check"] = {k: check.get(k) for k in ("supported", "unsupported", "controversy_flags")} if check else None
        store.put_approval(a)
        flags = [f for f in (check or {}).get("controversy_flags", []) if f]
        telegram.send_document(
            f"{a['date']}-{a['number']}.md", md,
            f"Draft ready for approved topic {a['number']}: {a['headline']}\n"
            + (f"Flags: {'; '.join(flags)}\n" if flags else "")
            + "Reply cancel " + str(a["number"]) + " if you want to stop it.",
        )
    else:
        a["problem"] = problem
        a["status"] = "failed"  # never leave a draftless item waiting in the publishing queue
        store.put_approval(a)
        telegram.send_message(f"Topic {a['number']} ({a['headline']}): {problem}\nIt has been taken out of the queue. Check the link and send write <link> again.")


def handle_text(text: str) -> str:
    t = text.strip().lower()
    latest = store.latest()
    if t in ("help", "/start", "/help", "?"):
        return HELP
    if t in ("queue", "/queue", "list"):
        waiting = store.approvals("approved")
        if not waiting:
            return "Nothing is waiting to be published."
        return "Waiting to be published:\n" + "\n".join(
            f"{a['number']} ({a['date']}): {a['headline']}" + ("" if a.get("draft") else "  [draft in progress]")
            for a in waiting
        )
    if t in ("leads", "prospects", "radar"):
        lst = store.latest_prospects()
        if not lst:
            return "No radar list yet."
        return "Today's radar list:\n" + "\n".join(
            f"{p['code']} {p['location_name']} ({p['rating']}): {p['status']}" + ("" if p.get("can_email") else ", call or write") for p in lst)
    s = re.match(r"^(sent|done|send)\s+(.+)$", t)
    if s:
        return mark_codes_sent(s.group(2))
    rm = re.match(r"^(remove|suppress|unsubscribe)\s+(\S+@\S+)$", t)
    if rm:
        store.add_suppressed(rm.group(2).strip(".,;"))
        return f"Done. {rm.group(2)} will never be emailed by the radar."
    w = re.match(r"^(write|draft|article)\s+(https?://\S+)\s*(.*)$", text.strip(), re.I | re.S)
    if w:
        url = w.group(2).rstrip(".,;:!?)]}'\"")
        return request_article(url, w.group(3).strip().lstrip(".,;: ").strip())
    m = re.match(r"^(cancel|stop|withdraw)\s+(\d+)$", t)
    if m:
        n = int(m.group(2))
        for a in store.approvals("approved"):
            if a["number"] == n and latest and a["date"] == latest["date"]:
                a["status"] = "cancelled"
                store.put_approval(a)
                return f"Cancelled topic {n}: {a['headline']}. It will not be published."
        return f"I could not find an approved, unpublished topic {n} from the latest list."
    nums = [int(x) for x in re.findall(r"\d+", t)]
    if nums and re.fullmatch(r"[\d\s,and&+.]+", t):
        if not latest or not latest.get("topics"):
            return "There is no morning list to approve from yet."
        by_num = {x["number"]: x for x in latest["topics"]}
        already = {(a["date"], a["number"]) for a in store.approvals() if a.get("status") in ("approved", "published")}
        done, missing = [], []
        for n in dict.fromkeys(nums):
            topic = by_num.get(n)
            if not topic:
                missing.append(str(n))
                continue
            if (latest["date"], n) in already:
                done.append(f"{n} (already approved)")
                continue
            a = store.put_approval({**topic, "date": latest["date"], "status": "approved", "approved_at": int(time.time())})
            done.append(f"{n}: {topic['headline']}")
            if not topic.get("draft"):
                threading.Thread(target=_draft_in_background, args=(a["id"],), daemon=True).start()
        msg = ""
        if done:
            msg += "Approved for publishing:\n" + "\n".join(done) + "\n\nThey will go live at the next publishing run and I will send you the links."
        if missing:
            msg += ("\n\n" if msg else "") + f"Not on {latest['date']}'s list: {', '.join(missing)}"
        return msg
    return "I did not understand that. " + HELP


def mark_codes_sent(which: str) -> str:
    lst = store.latest_prospects()
    if not lst:
        return "There is no radar list yet."
    by_code = {p["code"].lower(): p for p in lst}
    if which.strip() in ("all", "everything", "them all"):
        codes = [p["code"].lower() for p in lst if p.get("can_email")]
    else:
        codes = [c if c.startswith("p") else "p" + c for c in re.findall(r"p?\d+", which)]
    done, missing = [], []
    for c in dict.fromkeys(codes):
        p = by_code.get(c)
        if not p:
            missing.append(c.upper())
            continue
        p["status"] = "sent"
        p["sent_at"] = int(time.time())
        store.put_prospect(p)
        done.append(f"{p['code']} {p['location_name']}")
    msg = ("Recorded as sent:\n" + "\n".join(done)) if done else "Nothing recorded."
    if missing:
        msg += "\n\nNot on today's radar list: " + ", ".join(missing)
    return msg


SAM_COLUMNS = ["First Name", "Last Name", "Company", "Email", "Phone", "Website", "City", "Region", "Lead Source",
               "CQC Rating", "Last Inspection Date", "KLOE Finding", "CQC Provider ID", "CQC Report URL", "Service Type",
               "Company Number", "Contact Route", "Notes"]


def radar_token(day: str) -> str:
    import hashlib

    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), f"radar:{day}".encode(), hashlib.sha256).hexdigest()[:24]


def prospects_csv(items: list[dict]) -> str:
    import csv
    import io
    from datetime import datetime as _dt

    suppressed = set(store.suppressed())
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(SAM_COLUMNS)
    for p in items:
        if p.get("email", "") in suppressed:
            continue
        first, _, last = (p.get("registered_manager") or "").partition(" ")
        try:
            iso = _dt.strptime(p.get("report_date", ""), "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = ""
        route = "Email allowed (limited company)" if p.get("can_email") else "Call or write"
        notes = (f"CQC report radar {p.get('code', '')}. Registered manager: {p.get('registered_manager') or 'not listed'}. "
                 f"Provider: {p.get('provider_name', '')}.\n\nDraft email\nSubject: {p.get('subject', '')}\n\n{p.get('body', '')}")
        wr.writerow([first, last, p.get("location_name", ""), p.get("email", "") if p.get("can_email") else "",
                     p.get("phone", ""), p.get("website", ""), p.get("town", ""), p.get("region", ""), "CQC report radar",
                     p.get("rating", ""), iso, "; ".join(p.get("weak_key_questions") or []), p.get("provider_id", ""),
                     p.get("cqc_page", ""), p.get("service_type", ""), p.get("companies_house", ""), route, notes])
    return buf.getvalue()


@app.get("/radar/<day>/<token>.csv")
def radar_csv(day: str, token: str):
    """Today's radar list as a CSV laid out for the sam.ai Import Wizard (Leads, folder CQC Report Radar)."""
    from flask import Response

    if not os.getenv("APPROVALS_KEY") or not hmac.compare_digest(token, radar_token(day)):
        abort(404)
    items = [p for p in store.prospects() if p.get("radar_date") == day]
    return Response(prospects_csv(items), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=cqc-radar-{day}.csv"})


@app.get("/m/<pid>")
def open_email(pid: str):
    """One tap from Telegram: opens the drafted email in Yeukai's own mail app. He presses Send."""
    from urllib.parse import quote

    from flask import redirect

    p = store.get_prospect(pid)
    if not p or not p.get("can_email") or p.get("email", "") in set(store.suppressed()):
        abort(404)
    url = f"mailto:{p['email']}?subject={quote(p['subject'])}&body={quote(p['body'])}"
    return redirect(url, code=302)


def _page_title(url: str) -> str:
    import requests
    from bs4 import BeautifulSoup

    try:
        import sources

        r = requests.get(url, headers=sources.BROWSER_UA, timeout=30)
        if r.status_code >= 400:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = (h1.get_text(" ", strip=True) if h1 else "") or (soup.title.get_text(strip=True) if soup.title else "")
        return title[:200]
    except Exception:
        return ""


def request_article(url: str, note: str) -> str:
    """Yeukai asks for an article on a page he found. It is added to today's list and approved at once."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")
    latest = store.latest() or {"date": today, "topics": []}
    if latest.get("date") != today:
        latest = {"date": today, "topics": []}
    number = max([x["number"] for x in latest["topics"]] + [0]) + 1
    title = _page_title(url)
    if not title or re.search(r"couldn.t find|not found|page not found|error 404", title, re.I):
        return (f"I could not open that page: {url}\nPlease check the link (copy it straight from the address bar) and send write <link> again.")
    topic = {
        "number": number, "headline": f"Article on: {title}", "angle": note or "Explain what this means for adult social care providers in England and what to do now.",
        "product": "", "lead_magnet": "", "key_date": "", "source": "Requested by Yeukai", "source_title": title,
        "url": url, "draft": None,
    }
    latest["topics"].append(topic)
    store.save_latest(latest)
    a = store.put_approval({**topic, "date": today, "status": "approved", "approved_at": int(time.time())})
    threading.Thread(target=_draft_in_background, args=(a["id"],), daemon=True).start()
    return (f"Got it. Topic {number}: {title}\nI am drafting it now and will send the draft here. "
            f"It will go live at the next publishing run. Reply cancel {number} to stop it.")


@app.post("/telegram/<secret>")
def telegram_webhook(secret: str):
    if not hmac.compare_digest(secret, os.getenv("WEBHOOK_SECRET", "\x00")):
        abort(404)
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    if not msg.get("text") or chat != os.getenv("TELEGRAM_CHAT_ID"):
        return "ok"  # ignore anyone else who finds the bot
    telegram.send_message(handle_text(msg["text"]))
    return "ok"


@app.get("/approvals")
def list_approvals():
    if not _key_ok():
        abort(403)
    return jsonify([a for a in store.approvals("approved")])


@app.post("/approvals/<aid>/done")
def mark_done(aid: str):
    if not _key_ok():
        abort(403)
    a = store.get_approval(aid)
    if not a:
        abort(404)
    body = request.get_json(silent=True) or {}
    a["status"] = "published" if body.get("live_url") else body.get("status", "failed")
    a["live_url"] = body.get("live_url", "")
    a["short_url"] = body.get("short_url", "")
    a["note"] = body.get("note", "")
    store.put_approval(a)
    if a["status"] == "published":
        telegram.send_message(
            f"Live: {a['headline']}\n{a['live_url']}" + (f"\nShort link: {a['short_url']}" if a["short_url"] else "")
            + (f"\n{a['note']}" if a["note"] else "")
        )
    else:
        telegram.send_message(f"Not published: {a['headline']}\n{a['note']}")
    return jsonify({"ok": True})


@app.get("/prospects")
def list_prospects():
    if not _key_ok():
        abort(403)
    status = request.args.get("status", "approved_to_send")
    suppressed = set(store.suppressed())
    return jsonify([p for p in store.prospects(status) if p.get("email", "") not in suppressed])


@app.post("/prospects/<pid>/sent")
def mark_sent(pid: str):
    if not _key_ok():
        abort(403)
    p = store.get_prospect(pid)
    if not p:
        abort(404)
    body = request.get_json(silent=True) or {}
    ok = body.get("status", "sent") == "sent"
    p["status"] = "sent" if ok else "send_failed"
    p["sent_at"] = int(time.time())
    p["note"] = body.get("note", "")
    store.put_prospect(p)
    telegram.send_message(
        (f"Sent: {p['code']} {p['location_name']} ({p['email']})" if ok else f"Could not send {p['code']} {p['location_name']}: {p['note']}"))
    return jsonify({"ok": True})


@app.post("/suppress")
def suppress():
    if not _key_ok():
        abort(403)
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or [body.get("email", "")]
    for e in emails:
        store.add_suppressed(e)
    if any(emails):
        telegram.send_message("Asked not to be contacted again, now blocked: " + ", ".join(e for e in emails if e))
    return jsonify({"ok": True, "suppressed": len(store.suppressed())})


@app.get("/")
def health():
    return "Well-Led Network reply service is running."


def register_webhook() -> None:
    """On Render, point the Telegram bot at this service so replies arrive here."""
    import requests

    base, token, secret = os.getenv("RENDER_EXTERNAL_URL"), os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("WEBHOOK_SECRET")
    if not (base and token and secret):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            data={"url": f"{base}/telegram/{secret}", "allowed_updates": '["message","edited_message"]'},
            timeout=20,
        )
    except Exception as exc:
        print("webhook registration failed:", exc)


register_webhook()
