"""SalesBlink -> Telegram alerts for the Well-Led Network outreach.

Runs as a Render cron job every 15 minutes. Stateless: each run looks only at
the 15-minute slot that has just ended, so nothing is sent twice.

Every run:
  - a Telegram message for each new reply (who, which campaign, first lines)
  - a Telegram warning if any email bounced in the slot

Once a day (the 17:00 UK run):
  - a short summary: sent / replies / bounces today, campaign status
    (running or paused), each mailbox's health score and warm-up state.

Env vars (from the wln-secrets group):
  SALESBLINK_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional: DRY_RUN=1 prints messages instead of sending them;
FORCE_SUMMARY=1 sends the daily summary on this run (for testing).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

API = "https://run.salesblink.io/api/public/v1.0.0"
SLOT = 15 * 60  # seconds
UK = ZoneInfo("Europe/London")
SUMMARY_HOUR_UK = 17

SB_KEY = os.environ.get("SALESBLINK_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def sb_get(path, **params):
    params = {k: v for k, v in params.items() if v is not None}
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": SB_KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode("utf-8") or "{}")
    # Responses are either the payload itself or {"success":..,"data":..}
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("items", "results", "replies", "data"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def telegram(text):
    if DRY_RUN or not (TG_TOKEN and TG_CHAT):
        print("--- TELEGRAM ---\n" + text)
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    urllib.request.urlopen(req, timeout=30).read()


def ms(ts):
    return int(ts * 1000)


def uk_time(ms_value):
    try:
        return datetime.fromtimestamp(int(ms_value) / 1000, UK).strftime("%a %d %b %H:%M")
    except Exception:
        return ""


def clean(text, limit=400):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_replies(start_ms, end_ms):
    replies = as_list(sb_get("/replies", **{"from": start_ms, "to": end_ms, "per_page": 100}))
    sent = 0
    for r in replies:
        t = int(r.get("time") or 0)
        if t and not (start_ms <= t < end_ms):
            continue  # stay inside this slot so nothing is reported twice
        msg = (
            "📩 New reply in SalesBlink\n\n"
            f"From: {r.get('email', 'unknown')}\n"
            f"Campaign: {r.get('sequence_name') or r.get('sequence') or '-'}\n"
            f"Time: {uk_time(t)}\n\n"
            f"{clean(r.get('message'))}\n\n"
            "Reply from the SalesBlink Inbox: https://run.salesblink.io/"
        )
        telegram(msg)
        sent += 1
    return sent


def check_bounces(start_ms, end_ms):
    stats = sb_get("/analytics/overall", **{"from": start_ms, "to": end_ms}) or {}
    bounces = int(stats.get("bounces") or 0)
    if bounces:
        telegram(
            f"⚠️ SalesBlink: {bounces} email(s) bounced in the last 15 minutes.\n"
            "The campaign pauses itself after 2 bounces to protect the mailbox. "
            "Check it here: https://run.salesblink.io/"
        )
    return bounces


def daily_summary(now_uk):
    day_start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    stats = sb_get("/analytics/overall", **{"from": ms(day_start.timestamp()), "to": ms(now_uk.timestamp())}) or {}

    lines = [f"📊 SalesBlink daily check, {now_uk.strftime('%a %d %b')}", ""]
    lines.append(
        f"Today: {stats.get('sent', 0)} sent, {stats.get('replies', 0)} replies, "
        f"{stats.get('bounces', 0)} bounces"
    )

    lines.append("")
    lines.append("Campaigns:")
    for s in as_list(sb_get("/sequences", limit=100)):
        if s.get("archived"):
            continue
        state = "paused" if s.get("paused") else "running"
        lines.append(f"• {s.get('name')}: {state}, {s.get('sent', 0)} sent, {s.get('replies', 0)} replies in total")

    lines.append("")
    lines.append("Mailboxes:")
    for snd in as_list(sb_get("/senders", limit=100)):
        email = snd.get("smtpImapEmail") or snd.get("smtpUsername") or snd.get("id")
        health = snd.get("healthScore")
        threshold = snd.get("pauseColdEmailsHealthThreshold")
        warm = "warm-up on" if snd.get("warmupEnabled") else "WARM-UP OFF"
        ready = ""
        if health is not None and threshold:
            ready = " (ready to send)" if health >= threshold else f" (needs {threshold} to send)"
        lines.append(f"• {email}: health {health}{ready}, {warm}")

    telegram("\n".join(lines))


def main():
    if not SB_KEY:
        print("SALESBLINK_API_KEY is missing", file=sys.stderr)
        sys.exit(1)

    now = time.time()
    slot_end = int(now // SLOT) * SLOT
    slot_start = slot_end - SLOT
    start_ms, end_ms = ms(slot_start), ms(slot_end)

    problems = []
    for name, fn in (("replies", check_replies), ("bounces", check_bounces)):
        try:
            n = fn(start_ms, end_ms)
            print(f"{name}: {n}")
        except Exception as e:  # keep going so one failure doesn't hide the other
            problems.append(f"{name}: {e}")

    slot_end_uk = datetime.fromtimestamp(slot_end, UK)
    summary_slot = slot_end_uk.hour == SUMMARY_HOUR_UK and slot_end_uk.minute == 0
    if summary_slot or os.environ.get("FORCE_SUMMARY") == "1":
        try:
            daily_summary(datetime.now(UK))
        except Exception as e:
            problems.append(f"summary: {e}")

    if problems:
        print("\n".join(problems), file=sys.stderr)
        # Only shout on Telegram about errors once a day, at the summary run
        if summary_slot:
            telegram("SalesBlink alerts hit an error:\n" + "\n".join(problems))
        sys.exit(1)


if __name__ == "__main__":
    main()
