"""Sends the weekday radar follow-ups (and the older one-off check-ins) from Gmail.

Runs as the Render cron job wln-followups-send. It lives in a cron job, not the reply service,
because Render blocks outbound mail ports (465 and 587) on free web services: on 10 Nov 2026
19 of 20 follow-ups timed out from wln-replies. Paid (Starter) cron jobs can reach smtp.gmail.com.
The reply service still builds and announces the list at 08:30; this only sends it.
"""
from __future__ import annotations

import sys

import followups
import telegram_io as telegram
from store import Store


def main() -> int:
    store = Store()
    try:
        print("[radar follow-ups]", followups.tick(store, telegram.send_message, can_send=True))
    except Exception as exc:
        print("radar follow-ups failed:", type(exc).__name__, exc)
        try:
            telegram.send_message(f"Radar follow-ups hit a problem and sent nothing: {type(exc).__name__}: {str(exc)[:200]}")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
