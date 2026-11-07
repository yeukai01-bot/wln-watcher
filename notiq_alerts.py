"""Telegram alerts for replies to the NotiQ cold email agent (Yeukai Kajidori agent).

NotiQ (API & MCP > Webhooks) posts each event to /notiq/<key>. The key comes from APPROVALS_KEY,
so no new secret is needed on Render; type "notiq key" in Telegram to get the full webhook link.

Events handled:
  reply.received        a lead replied. Claude reads it and says whether the agent will answer it
                        or whether it needs Yeukai (sensitive, angry, wants him personally, or the
                        agent has already used its 3 replies with this lead).
  account.disconnected  a sending inbox dropped off, so emails have stopped from it.
  lead.tagged           ignored.
Nothing is replied to or sent from here.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

INBOX = "https://app.notiq.io/inbox"
ACCOUNTS = "https://app.notiq.io/accounts"
AGENT_REPLY_LIMIT = 3
COUNT_KEY = "notiq:replies:"
SEEN_KEY = "notiq:seen"

_counts: dict[str, int] = {}
_seen: set[str] = set()


def webhook_key() -> str:
    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), b"notiq-webhook", hashlib.sha256).hexdigest()[:32]


def webhook_url() -> str:
    base = os.getenv("RENDER_EXTERNAL_URL", "https://wln-replies.onrender.com").rstrip("/")
    return f"{base}/notiq/{webhook_key()}"


def _first_time(store, event_id: str) -> bool:
    if not event_id:
        return True
    if store is not None and getattr(store, "r", None):
        return bool(store.r.sadd(SEEN_KEY, event_id))
    if event_id in _seen:
        return False
    _seen.add(event_id)
    return True


def _bump(store, lead: str) -> int:
    if store is not None and getattr(store, "r", None):
        return int(store.r.incr(COUNT_KEY + lead))
    _counts[lead] = _counts.get(lead, 0) + 1
    return _counts[lead]


AUTO = re.compile(r"(out of (the )?office|automatic reply|auto-?reply|annual leave|on leave until|"
                  r"no longer (work|with)|has left|undeliverable|delivery status|mailbox (is )?full)", re.I)


def screen(name: str, company: str, subject: str, text: str) -> dict:
    """Ask Claude what this reply is and whether Yeukai must step in. Falls back to 'needs you'."""
    try:
        import ai

        out = ai._json(ai._ask(
            "You triage replies to cold emails sent by Yeukai Kajidori's email agent. Yeukai is a UK CQC "
            "consultant and founder of the free Well-Led Network; the emails offer a free CQC Inspection "
            "Readiness Scorecard, the WhatsApp group, or 'reply yes for the three gaps'. The agent answers "
            "ordinary replies itself, but it does NOT answer: complaints, anger, legal threats, safeguarding "
            "concerns, a person at risk, a death, anything sensitive, or someone asking for Yeukai personally "
            "or for a price he must quote. Classify the reply. "
            'Reply only with JSON: {"kind": "hot"|"question"|"tuesday"|"not_interested"|"auto"|"sensitive"|"other", '
            '"needs_yeukai": true|false, "summary": "one short line on what they said or want", '
            '"next": "one short line on what Yeukai should do, or \'nothing, the agent will reply\'"}. '
            "hot = wants help, a call, the three gaps, or is clearly interested in working together.",
            f"From: {name} ({company})\nSubject: {subject}\n\n{(text or '')[:3500]}",
            max_tokens=300,
        ))
        if isinstance(out, dict):
            return out
    except Exception:
        pass
    return {"kind": "other", "needs_yeukai": True, "summary": (text or subject or "")[:200],
            "next": "Read it in the NotiQ inbox"}


def handle(store, payload: dict, send) -> str:
    etype = (payload or {}).get("type", "")
    data = (payload or {}).get("data") or {}
    if not _first_time(store, payload.get("id", "")):
        return "duplicate"

    if etype == "account.disconnected":
        send("NotiQ: a sending inbox has disconnected, so no emails are going from it.\n\n"
             f"Reconnect it here: {ACCOUNTS}\n(Account id: {data.get('accountId') or 'not given'})")
        return "alerted: disconnected"

    if etype != "reply.received":
        return "ignored"

    name = data.get("fullName") or data.get("from") or "Someone"
    company = data.get("companyName") or ""
    email = data.get("email") or ""
    text = data.get("messageText") or ""
    subject = data.get("subject") or ""
    lead = data.get("leadId") or email or name
    n = _bump(store, lead)

    if AUTO.search(f"{subject}\n{text[:600]}"):
        return "skipped: auto reply"

    s = screen(name, company, subject, text)
    kind = s.get("kind", "other")
    if kind == "auto":
        return "skipped: auto reply"

    handed_over = n > AGENT_REPLY_LIMIT
    needs_you = bool(s.get("needs_yeukai")) or handed_over or kind == "sensitive"

    if kind == "sensitive":
        head = "NEEDS YOU: sensitive reply, the agent will not answer it"
    elif handed_over:
        head = f"NEEDS YOU: reply {n} from this lead, the agent has handed it to you"
    elif kind == "hot":
        head = "HOT LEAD replied" + (" (needs you)" if needs_you else ", the agent is answering")
    elif needs_you:
        head = "NEEDS YOU: email reply"
    elif kind == "not_interested":
        head = "Not interested (the agent will confirm and stop)"
    elif kind == "tuesday":
        head = "Wants the Tuesday briefing (the agent will send the join link)"
    else:
        head = "Email reply, the agent is answering"

    nxt = s.get("next") or ""
    if needs_you and (not nxt or nxt.lower().startswith("nothing")):
        nxt = "Reply to them yourself in the NotiQ inbox"
    if not needs_you and not nxt:
        nxt = "Nothing, the agent will reply"

    snippet = re.sub(r"\s+", " ", text).strip()
    if len(snippet) > 500:
        snippet = snippet[:500].rsplit(" ", 1)[0] + "..."

    send(
        f"{head}\n\n"
        f"From: {name}" + (f", {company}" if company else "") + (f" <{email}>" if email else "") + "\n"
        f"Said: {s.get('summary') or snippet[:200]}\n"
        f"Next: {nxt}\n\n"
        f"Their words: {snippet}\n\n"
        f"Open the NotiQ inbox: {INBOX}"
    )
    return "alerted: " + kind
