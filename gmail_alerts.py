"""Alerts Yeukai on Telegram when a real person replies to the kajidoricollective Gmail inbox.

Make (Gmail > Watch emails > HTTP) posts each new inbox email to /gmail/inbound. This module
drops automated mail with simple rules, asks Claude whether a human is waiting on Yeukai, and
sends a short Telegram alert with a link straight to the thread. Nothing is replied to or sent.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

INBOX = "kajidoricollective@gmail.com"
OWN = {"kajidoricollective@gmail.com", "yeukai01@gmail.com", "yeukaibusinessshow@gmail.com", "yeukai1@hotmail.co.uk"}
AUTO_SENDERS = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?@|notify@|mailer-daemon|postmaster|bounce|newsletter|news@|"
    r"updates?@|digest@|marketing@|hello@mail\.|info@mail\.|support@.*(stripe|paypal|google|linkedin|facebook|zoom|calendly)|"
    r"@(linkedin|facebookmail|google|accounts\.google|youtube|substack|stripe|paypal|zoom|calendly|trafft|ezycourse|"
    r"mailchimp|sendgrid|hubspot|sam\.ai|samai|webinarjam|everwebinar|shorby|canva|medium|eventbrite|makemail|make)\.)",
    re.I,
)
AUTO_SUBJECT = re.compile(
    r"(receipt|invoice|your order|order confirmation|password|verify|verification|security alert|sign-?in|"
    r"newsletter|unsubscribe|webinar starts|reminder:|payment received|subscription|welcome to|out of office|"
    r"automatic reply|auto-?reply|delivery status|undeliverable)",
    re.I,
)

_seen: set[str] = set()


def inbound_key() -> str:
    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), b"gmail-inbound", hashlib.sha256).hexdigest()[:32]


def _addr(sender: str) -> str:
    m = re.search(r"<([^>]+)>", sender or "")
    return (m.group(1) if m else (sender or "")).strip().lower()


def rule_skip(sender: str, subject: str) -> str | None:
    a = _addr(sender)
    if not a:
        return "no sender"
    if a in OWN:
        return "own address"
    if AUTO_SENDERS.search(a):
        return "automated sender"
    if AUTO_SUBJECT.search(subject or ""):
        return "automated subject"
    return None


def screen(sender: str, subject: str, body: str) -> dict:
    """Ask Claude whether a real person needs Yeukai. Falls back to 'alert' if the call fails."""
    try:
        import ai

        out = ai._json(ai._ask(
            "You screen emails for Yeukai Kajidori, a UK care consultant (CQC compliance, the Well-Led Network). "
            "Decide if this email is from a real person (prospect, client, partner, colleague) who is waiting on him. "
            "Marketing, automated notices, receipts and cold sales pitches to him are not. "
            'Reply only with JSON: {"alert": true|false, "who": "name and organisation", '
            '"wants": "one short line on what they want", "urgent": true|false}',
            f"From: {sender}\nSubject: {subject}\n\n{(body or '')[:3000]}",
            max_tokens=300,
        ))
        if isinstance(out, dict):
            return out
    except Exception:
        pass
    return {"alert": True, "who": sender, "wants": subject, "urgent": False}


def gmail_link(thread: str) -> str:
    if thread:
        return f"https://mail.google.com/mail/u/?authuser={INBOX}#inbox/{thread}"
    return f"https://mail.google.com/mail/u/?authuser={INBOX}#inbox"


def handle(sender: str, subject: str, body: str, thread: str, send) -> str:
    key = thread or f"{_addr(sender)}|{subject}"
    if key in _seen:
        return "duplicate"
    _seen.add(key)
    why = rule_skip(sender, subject)
    if why:
        return f"skipped: {why}"
    s = screen(sender, subject, body)
    if not s.get("alert"):
        return "skipped: not a person waiting"
    head = "URGENT reply waiting" if s.get("urgent") else "New reply waiting"
    send(
        f"{head} in kajidoricollective Gmail\n\n"
        f"From: {s.get('who') or sender}\n"
        f"Subject: {subject}\n"
        f"Wants: {s.get('wants') or '(see email)'}\n\n"
        f"Open: {gmail_link(thread)}"
    )
    return "alerted"
