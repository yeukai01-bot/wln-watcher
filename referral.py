"""The Well-Led 1,000: Viral Loops referral campaign, wired to the Well-Led Network and Brevo.

How a referral works
  1. A care leader signs up on https://www.welllednetwork.com/refer (Viral Loops form, campaign 150443).
     Viral Loops posts a "participation" webhook here. We save them, and add or update them in the Brevo list
     "WLN — The Well-Led 1000" with their personal referral link, so the Brevo sequences can start.
  2. A referred person only counts once they actually JOIN the Well-Led Network community. The community's
     "New signup" webhook already reaches /wln/members (briefing.on_member_event), which calls on_wln_join().
     If that email signed up through someone's link, is new to the community and passes the fraud checks,
     we tell Viral Loops to count the referral (private API, "only authorised conversions" is on in Viral
     Loops, so nothing from a browser can fake one).
  3. Viral Loops posts a "milestoneReached" webhook at 1, 3, 5, 10 and 25 referrals. We update Brevo, deliver
     the reward once (never twice), email the participant, and tell Yeukai on Telegram. A reward that cannot be
     delivered (no code left, Brevo refused) is kept as pending, retried every morning and reported.

Safety
  Until REFERRAL_LIVE=1 is set in Render the campaign runs in TEST MODE: nothing is converted and no email goes
  to anyone, except addresses containing "+wl1000test" (used for the end-to-end test). Every action is logged.

Render settings needed (all in the wln-secrets group)
  VL_API_TOKEN       Viral Loops > campaign > Installation: private API token (Yeukai pastes it)
  VL_PUBLIC_TOKEN    optional; defaults to the campaign public id B23xj49HlLGc57S1zG8zfW83ZAA
  REFERRAL_LIVE      leave unset for test mode; "1" once Yeukai approves the launch
  WL1000_REWARD1_URL link to the Well-Led Governance Checklist (free library item)
  WL1000_AMBASSADOR_URL  where ambassadors pick their group session time (optional)

Rewards at 3, 5 and 10 are free (GBP 0) hidden paywalls in the Well-Led Network (795 Staff Card Set,
797 VIP Upgrade, 796 Toolkit). The reward email carries the private checkout link: tick the terms, Complete,
done. No codes to make or load. (The Well-Led Network has no coupon feature, checked 26 Nov 2026.)

Telegram commands
  refer              campaign status: participants, converted referrals, rewards delivered, pending
  refer report       the weekly report now
  refer pending      rewards waiting to be delivered, and why
  refer retry        try every pending reward again now
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
VL_CONVERT = "https://app.viral-loops.com/api/v3/campaign/participant/convert"
REFER_PAGE = "https://www.welllednetwork.com/refer"
LIST_NAME = "WLN — The Well-Led 1000"
TEST_MARK = "+wl1000test"

# Redis keys
K_P = "wl1000:p:"            # + email -> participant JSON
K_JOINED = "wl1000:joined:"  # + email -> ISO time the person joined the community (since launch)
K_CONVERTED = "wl1000:converted"   # set of emails credited as a referral
K_REJECTED = "wl1000:rejected"     # list of JSON {email, reason, at}
K_REWARD = "wl1000:reward:"  # + email + ":" + milestone -> JSON {status, at, code, why}
K_PENDING = "wl1000:pending"       # set of "email|milestone"
K_CODES = "wl1000:codes:"    # + pool -> list of unused codes
K_LIST = "wl1000:list_id"
K_STATS = "wl1000:stats"     # hash of counters
K_LOG = "wl1000:log"         # recent events (capped)

REWARDS = {
    1: {"name": "Well-Led Governance Checklist", "kind": "link",
        "url_env": "WL1000_REWARD1_URL", "default_url": "https://shor.by/wl1000-checklist"},
    3: {"name": "When the Systems Go Down: Staff Card Set", "kind": "claim",
        "checkout": "https://www.welllednetwork.com/paywall/795/checkout"},
    5: {"name": "Well-Led Live VIP Upgrade", "kind": "claim",
        "checkout": "https://www.welllednetwork.com/paywall/797/checkout",
        "terms": ("Your VIP upgrade is for Well-Led Live 2027 (Thursday 18 to Saturday 20 March 2027, 5pm to 8pm UK time), "
                  "plus the VIP implementation workshop and group Q&A. Claim it before 18 March 2027. "
                  "It is for you personally and cannot be swapped for cash.")},
    10: {"name": "72-Hour Care Service Continuity Toolkit", "kind": "claim",
         "checkout": "https://www.welllednetwork.com/paywall/796/checkout"},
    25: {"name": "Well-Led Network Ambassador", "kind": "ambassador"},
}
POOLS = ("cards", "vip", "toolkit")

# A short list of throwaway-address domains; anything here never counts as a referral.
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
    "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com", "tempinbox.com", "burnermail.io",
}

SIGNATURE_HTML = (
    "<p style=\"margin:24px 0 0\">Kind regards<br><strong>Yeukai Kajidori</strong><br>Founder, Well-Led Network<br>"
    "Creator of the Well-Led and Safe Supported Living Framework<br>"
    "<a href=\"https://www.welllednetwork.com/blog\">www.welllednetwork.com/blog</a><br>"
    "Work with me: <a href=\"https://shor.by/wln-work-with-me\">https://shor.by/wln-work-with-me</a><br>"
    "The Kajidori Collective Ltd</p>"
    "<p style=\"font-size:14px;color:#4b5563;margin-top:18px\">P.S. Want quick answers to your CQC questions? Join the "
    "Well-Led Network, my free WhatsApp group for UK care leaders. Managers post their questions and I answer them there: "
    "<a href=\"https://shor.by/wln-whatsapp\">https://shor.by/wln-whatsapp</a></p>"
)


# ---------------------------------------------------------------- helpers
def now() -> datetime:
    return datetime.now(UK)


def live() -> bool:
    return os.getenv("REFERRAL_LIVE", "").strip() == "1"


def is_test(email: str) -> bool:
    return TEST_MARK in (email or "").lower()


def may_act(email: str) -> bool:
    """Real actions (conversion, emails) only when live, or for clearly marked test addresses."""
    return live() or is_test(email)


def token() -> str:
    return hmac.new(os.getenv("WEBHOOK_SECRET", "").encode(), b"wl1000-viral-loops", hashlib.sha256).hexdigest()[:24]


def norm(email: str) -> str:
    return (email or "").strip().lower()


def canonical(email: str) -> str:
    """Same mailbox under different spellings (Gmail dots, +tags) counts as one person for fraud checks."""
    e = norm(email)
    if "@" not in e:
        return e
    local, _, dom = e.partition("@")
    if TEST_MARK not in e:  # marked test addresses stay distinct so one inbox can run the end-to-end test
        local = local.split("+", 1)[0]
    if dom in ("gmail.com", "googlemail.com"):
        local, dom = local.replace(".", ""), "gmail.com"
    return f"{local}@{dom}"


def _r(store):
    return store.r


def _log(store, text: str) -> None:
    line = f"{now():%d %b %H:%M} {text}"
    print("[wl1000]", line)
    if _r(store):
        store.r.lpush(K_LOG, line)
        store.r.ltrim(K_LOG, 0, 299)


def _inc(store, field: str, n: int = 1) -> None:
    if _r(store):
        store.r.hincrby(K_STATS, field, n)


def _get(store, key: str) -> dict | None:
    if not _r(store):
        return None
    raw = store.r.get(key)
    return json.loads(raw) if raw else None


def _set(store, key: str, value: dict) -> None:
    if _r(store):
        store.r.set(key, json.dumps(value))


def _find(d, names: tuple[str, ...]):
    if isinstance(d, dict):
        for k, v in d.items():
            if str(k).lower() in names and isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()
        for v in d.values():
            got = _find(v, names)
            if got:
                return got
    elif isinstance(d, list):
        for v in d:
            got = _find(v, names)
            if got:
                return got
    return ""


def ref_link(code: str) -> str:
    return f"{REFER_PAGE}?referralCode={code}" if code else REFER_PAGE


# ---------------------------------------------------------------- Brevo
ATTRS = {  # name -> Brevo type
    "WL1000_ROLE": "text", "WL1000_SERVICE": "text", "WL1000_SOURCE": "text", "WL1000_REFERRED_BY": "text",
    "WL1000_REF_CODE": "text", "WL1000_REF_LINK": "text", "WL1000_REF_COUNT": "float", "WL1000_MILESTONE": "float",
    "WL1000_REWARD_STATUS": "text", "WL1000_JOINED": "date", "WL1000_MEMBER": "text", "WL1000_CONSENT": "text",
    "WL1000_CUSTOMER": "text",
}


def _brevo(method: str, path: str, body: dict | None = None):
    import briefing

    return briefing._brevo(method, path, body)


def ensure_attributes(store) -> str:
    if _r(store) and store.r.get("wl1000:attrs_ok"):
        return "already set up"
    r = _brevo("GET", "/contacts/attributes")
    have = {a.get("name") for a in (r.json().get("attributes") or [])} if r.ok else set()
    made = []
    for name, typ in ATTRS.items():
        if name not in have:
            x = _brevo("POST", f"/contacts/attributes/normal/{name}", {"type": typ})
            if x.status_code in (200, 201, 204):
                made.append(name)
    if _r(store):
        store.r.set("wl1000:attrs_ok", "1")
    return f"created {len(made)} attributes" if made else "all attributes existed"


def list_id(store) -> int:
    import briefing

    if _r(store):
        cached = store.r.get(K_LIST)
        if cached:
            return int(cached)
    lid, offset = None, 0
    while lid is None:
        r = _brevo("GET", f"/contacts/lists?limit=50&offset={offset}")
        r.raise_for_status()
        lists = r.json().get("lists") or []
        lid = next((l["id"] for l in lists if l.get("name") == LIST_NAME), None)
        if len(lists) < 50:
            break
        offset += 50
    if lid is None:
        r = _brevo("GET", "/contacts/folders?limit=50&offset=0")
        r.raise_for_status()
        fid = next((f["id"] for f in r.json().get("folders") or [] if f.get("name") == briefing.FOLDER), None)
        if fid is None:
            r = _brevo("POST", "/contacts/folders", {"name": briefing.FOLDER})
            r.raise_for_status()
            fid = r.json()["id"]
        r = _brevo("POST", "/contacts/lists", {"name": LIST_NAME, "folderId": fid})
        r.raise_for_status()
        lid = r.json()["id"]
    if _r(store):
        store.r.set(K_LIST, str(lid))
    return int(lid)


def brevo_contact(email: str) -> dict | None:
    r = _brevo("GET", f"/contacts/{requests.utils.quote(email)}")
    return r.json() if r.ok else None


def is_existing_member(store, email: str) -> bool:
    """Already in the community before this sign-up: on the 'WLN Members' list and not a recent join we saw."""
    import briefing

    if _r(store) and store.r.get(K_JOINED + norm(email)):
        return False
    c = brevo_contact(email)
    if not c:
        return False
    try:
        return briefing.list_id(store) in (c.get("listIds") or [])
    except Exception:
        return False


def upsert(store, email: str, attrs: dict, add_to_list: bool = True) -> str:
    import briefing

    if not briefing._key():
        return "Brevo key missing"
    body = {"email": email, "updateEnabled": True, "attributes": {k: v for k, v in attrs.items() if v not in (None, "")}}
    if add_to_list:
        body["listIds"] = [list_id(store)]
    r = _brevo("POST", "/contacts", body)
    return "ok" if r.status_code in (200, 201, 204) else f"refused {r.status_code}: {r.text[:120]}"


def send_email(to: str, first: str, subject: str, body_html: str) -> str:
    import briefing

    page = f"""<!doctype html><html><body style="margin:0;background:#F4EBD7;font-family:Arial,Helvetica,sans-serif;color:#1f2937">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4EBD7"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px">
<tr><td style="background:#142B4C;color:#ffffff;padding:18px 28px;font-size:13px;letter-spacing:1px;border-radius:8px 8px 0 0">WLN &middot; THE WELL-LED 1,000</td></tr>
<tr><td style="padding:28px;font-size:16px;line-height:1.6">
<p>Hi {html.escape(first or 'there')},</p>{body_html}{SIGNATURE_HTML}</td></tr>
<tr><td style="padding:16px 28px 24px;font-size:12px;color:#6b7280;line-height:1.5">You are getting this because you joined The Well-Led 1,000
referral campaign. Campaign terms: <a href="{REFER_PAGE}#terms" style="color:#6b7280">{REFER_PAGE}#terms</a><br>
The Kajidori Collective Ltd, trading as Well-Led Network. Registered in England and Wales, company number 17556769.
71-75 Shelton Street, Covent Garden, London WC2H 9JQ.</td></tr></table></td></tr></table></body></html>"""
    r = _brevo("POST", "/smtp/email", {"sender": briefing.SENDER, "replyTo": {"email": briefing.REPLY_TO},
                                       "to": [{"email": to, "name": first or to}], "subject": subject,
                                       "htmlContent": page, "tags": ["wl1000"]})
    return "sent" if r.status_code in (200, 201, 202) else f"refused {r.status_code}: {r.text[:120]}"


# ---------------------------------------------------------------- Viral Loops
def vl_convert(email: str) -> str:
    tok, pub = os.getenv("VL_API_TOKEN", "").strip(), os.getenv("VL_PUBLIC_TOKEN", "B23xj49HlLGc57S1zG8zfW83ZAA").strip()
    if not tok:
        return "VL_API_TOKEN not set in Render"
    body = {"user": {"email": email}}
    if pub:
        body["publicToken"] = pub
    r = requests.post(VL_CONVERT, json=body, timeout=30,
                      headers={"apiToken": tok, "accept": "application/json", "content-type": "application/json"})
    return "ok" if r.ok else f"refused {r.status_code}: {r.text[:160]}"


# ---------------------------------------------------------------- events
def on_participation(store, payload: dict, send) -> dict:
    user = payload.get("user") or {}
    ref = payload.get("referrer") or {}
    email, first = norm(user.get("email")), (user.get("firstname") or "").strip()
    if not email:
        return {"ok": True, "result": "no email"}
    existing = _get(store, K_P + email)
    referrer = norm(ref.get("email"))
    role = _find(user, ("role", "professional_role", "professionalrole", "jobrole"))
    service = _find(user, ("service", "service_type", "servicetype", "caretype"))
    consent = _find(user, ("consent", "gdpr", "marketing", "marketingconsent", "newsletter"))
    member = "existing" if is_existing_member(store, email) else "new"
    p = existing or {}
    p.update({"email": email, "first": first, "last": (user.get("lastname") or "").strip(),
              "code": user.get("referralCode") or p.get("code", ""), "referrer": referrer or p.get("referrer", ""),
              "referrer_code": ref.get("referralCode") or p.get("referrer_code", ""), "role": role or p.get("role", ""),
              "service": service or p.get("service", ""), "member_at_signup": p.get("member_at_signup") or member,
              "joined_campaign": p.get("joined_campaign") or now().isoformat(), "test": is_test(email)})
    _set(store, K_P + email, p)
    if not existing:
        _inc(store, "participants")
        _inc(store, "participants_referred" if referrer else "participants_organic")
    res = upsert(store, email, {
        "FIRSTNAME": p["first"], "LASTNAME": p["last"], "WL1000_ROLE": p["role"], "WL1000_SERVICE": p["service"],
        "WL1000_SOURCE": "referral" if p["referrer"] else "organic", "WL1000_REFERRED_BY": p["referrer"],
        "WL1000_REF_CODE": p["code"], "WL1000_REF_LINK": ref_link(p["code"]), "WL1000_MEMBER": p["member_at_signup"],
        "WL1000_JOINED": now().strftime("%Y-%m-%d"), "WL1000_REF_COUNT": p.get("count", 0),
        "WL1000_CONSENT": "yes" if str(consent).lower() in ("true", "1", "yes", "on") else ("no" if consent else ""),
    })
    _log(store, f"joined campaign: {email} ({'referred by ' + referrer if referrer else 'organic'}, {p['member_at_signup']} member) Brevo {res}")
    # Joined the community a little BEFORE filling in the form? Credit it now.
    if p["referrer"] and _r(store) and store.r.get(K_JOINED + email):
        credit(store, email, send)
    return {"ok": True, "result": res}


def fraud_reason(store, p: dict) -> str:
    email, referrer = p["email"], p.get("referrer", "")
    if not referrer:
        return "not referred"
    if canonical(email) == canonical(referrer):
        return "self-referral"
    if email.split("@")[-1] in DISPOSABLE:
        return "disposable email address"
    if p.get("member_at_signup") == "existing":
        return "already a Well-Led Network member"
    if _r(store) and store.r.sismember(K_CONVERTED, canonical(email)):
        return "already credited"
    return ""


def credit(store, email: str, send) -> str:
    p = _get(store, K_P + norm(email))
    if not p:
        return "not a campaign participant"
    why = fraud_reason(store, p)
    if why:
        if why != "not referred" and _r(store):
            store.r.lpush(K_REJECTED, json.dumps({"email": email, "referrer": p.get("referrer"), "reason": why,
                                                  "at": now().isoformat()}))
            _inc(store, "rejected")
        _log(store, f"not credited: {email} ({why})")
        return why
    if not may_act(email):
        _log(store, f"TEST MODE, would credit {p['referrer']} for {email}")
        return "test mode"
    res = vl_convert(email)
    if res == "ok":
        if _r(store):
            store.r.sadd(K_CONVERTED, canonical(email))
        _inc(store, "converted")
        _log(store, f"credited: {p['referrer']} for {email}")
    else:
        _log(store, f"conversion FAILED for {email}: {res}")
        send(f"The Well-Led 1,000: could not credit {p['referrer']} for {email} ({res}). It will be tried again tomorrow.")
        if _r(store):
            store.r.sadd(K_PENDING, f"{email}|convert")
    return res


def on_wln_join(store, email: str, send) -> str:
    """Called for every new Well-Led Network sign-up (from briefing.on_member_event)."""
    email = norm(email)
    if not email or not _r(store):
        return "skipped"
    store.r.set(K_JOINED + email, now().isoformat(), ex=60 * 60 * 24 * 30)
    _inc(store, "community_joins")
    p = _get(store, K_P + email)
    if not p:
        return "not in the campaign"
    upsert(store, email, {"WL1000_MEMBER": "joined"}, add_to_list=False)
    return credit(store, email, send)


def on_milestone(store, payload: dict, send) -> dict:
    user = payload.get("user") or {}
    email, first = norm(user.get("email")), (user.get("firstname") or "").strip()
    count = int(user.get("referralCount") or (user.get("stats") or {}).get("referralCountTotal") or 0)
    need = int(((payload.get("milestone") or {}).get("referralsNeeded")) or count)
    p = _get(store, K_P + email) or {"email": email, "first": first}
    p["count"], p["sharing"] = count, user.get("sharingUrl") or p.get("sharing", "")
    _set(store, K_P + email, p)
    upsert(store, email, {"WL1000_REF_COUNT": count, "WL1000_MILESTONE": need}, add_to_list=False)
    _log(store, f"milestone {need}: {email} ({count} referrals)")
    return {"ok": True, "result": deliver(store, email, need, send)}


def _pop_code(store, pool: str) -> str:
    return store.r.lpop(K_CODES + pool) if _r(store) else ""


def deliver(store, email: str, milestone: int, send) -> str:
    """Deliver one reward exactly once. Safe to call again: a delivered reward is never re-sent."""
    reward = REWARDS.get(milestone)
    if not reward:
        return f"no reward set for {milestone}"
    key = K_REWARD + f"{email}:{milestone}"
    done = _get(store, key) or {}
    if done.get("status") == "delivered":
        return "already delivered"
    p = _get(store, K_P + email) or {"email": email}
    first = p.get("first", "")
    if not may_act(email):
        _log(store, f"TEST MODE, would deliver {reward['name']} to {email}")
        return "test mode"
    code, link = done.get("code", ""), ""
    if reward["kind"] == "code":
        code = code or _pop_code(store, reward["pool"])
        if not code:
            return _pending(store, email, milestone, f"no {reward['pool']} codes left", send)
        link = reward["checkout"]
    elif reward["kind"] == "link":
        link = os.getenv(reward["url_env"], reward["default_url"])
    elif reward["kind"] == "claim":
        link = reward["checkout"]
    subject, body = reward_email(reward, milestone, p, code, link)
    res = send_email(email, first, subject, body)
    if res != "sent":
        _set(store, key, {"status": "pending", "code": code, "why": res, "at": now().isoformat()})
        return _pending(store, email, milestone, f"email {res}", send, keep_code=True)
    _set(store, key, {"status": "delivered", "code": code, "at": now().isoformat()})
    if _r(store):
        store.r.srem(K_PENDING, f"{email}|{milestone}")
    _inc(store, f"reward_{milestone}")
    upsert(store, email, {"WL1000_REWARD_STATUS": f"{milestone}: delivered"}, add_to_list=False)
    _log(store, f"reward {milestone} delivered to {email}")
    if milestone == 25:
        send(f"The Well-Led 1,000: {p.get('first') or email} ({email}) has introduced 25 care leaders and is now a "
             f"Well-Led Network Ambassador. Their welcome pack has gone out; they will choose a group session time.")
    elif milestone >= 5:
        send(f"The Well-Led 1,000: {email} reached {milestone} referrals and was sent {reward['name']}.")
    return "delivered"


def _pending(store, email: str, milestone: int, why: str, send, keep_code: bool = False) -> str:
    if _r(store):
        store.r.sadd(K_PENDING, f"{email}|{milestone}")
        if not keep_code:
            _set(store, K_REWARD + f"{email}:{milestone}", {"status": "pending", "why": why, "at": now().isoformat()})
    upsert(store, email, {"WL1000_REWARD_STATUS": f"{milestone}: pending"}, add_to_list=False)
    _log(store, f"reward {milestone} PENDING for {email}: {why}")
    send(f"The Well-Led 1,000: reward for {milestone} referrals is waiting for {email} ({why}). "
         f"It is retried every morning. " + ("Load more codes with: refer codes <pool> CODE1 CODE2" if "codes" in why else ""))
    return "pending"


def retry_pending(store, send) -> str:
    if not _r(store):
        return "no store"
    items = list(store.r.smembers(K_PENDING))
    out = []
    for item in items:
        email, _, what = item.partition("|")
        if what == "convert":
            res = credit(store, email, lambda *_: None)
            if res == "ok":
                store.r.srem(K_PENDING, item)
            out.append(f"{email} convert: {res}")
        elif what.isdigit():
            out.append(f"{email} reward {what}: {deliver(store, email, int(what), lambda *_: None)}")
    return f"retried {len(items)}" + ("\n" + "\n".join(out) if out else "")


# ---------------------------------------------------------------- reward emails (his voice, British English)
def reward_email(reward: dict, milestone: int, p: dict, code: str, link: str) -> tuple[str, str]:
    e = html.escape
    nxt = next((m for m in sorted(REWARDS) if m > milestone), None)
    count = p.get("count") or milestone
    share = p.get("sharing") or ref_link(p.get("code", ""))
    next_line = (f"<p>You have introduced {count} care leaders so far. At {nxt} you unlock "
                 f"<strong>{e(REWARDS[nxt]['name'])}</strong>. Your personal link is still the same: "
                 f"<a href=\"{e(share)}\">{e(share)}</a></p>") if nxt else ""
    if reward["kind"] == "ambassador":
        book = os.getenv("WL1000_AMBASSADOR_URL", "https://shor.by/wln-work-with-me")
        body = (f"<p>Twenty-five care leaders have joined the Well-Led Network because of you. That is a team's worth of "
                f"managers, owners and nominated individuals who now have somewhere to bring their questions. Thank you.</p>"
                f"<p>I would like to welcome you as a <strong>Well-Led Network Ambassador</strong>. Here is what that means:</p>"
                f"<ul><li>An Ambassador badge for your LinkedIn profile and email signature (attached in your welcome pack).</li>"
                f"<li>A place in the Ambassadors' group leadership session with me, held in small groups. "
                f"Pick a time here: <a href=\"{e(book)}\">{e(book)}</a></li>"
                f"<li>The chance to lead a discussion in the community on a topic you know well.</li>"
                f"<li>Recognition in our community updates, only if you are happy for me to name you. Just reply yes or no.</li></ul>"
                f"<p>None of this needs regular meetings. It is a thank you, and an open door.</p>")
        return "You are now a Well-Led Network Ambassador", body
    if reward["kind"] == "link":
        body = (f"<p>Your first introduction has joined the Well-Led Network. Thank you for passing it on.</p>"
                f"<p>Here is your reward, the <strong>{e(reward['name'])}</strong>:</p>"
                f"<p style=\"margin:22px 0\"><a href=\"{e(link)}\" style=\"display:inline-block;background:#142B4C;color:#ffffff;"
                f"text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:bold\">Get your checklist</a></p>"
                f"<p>Use it with your deputy or senior team this week: go through it line by line and mark anything you could "
                f"not show an inspector today. That list is your next month's work.</p>{next_line}")
        return f"Your {reward['name']} is ready", body
    terms = f"<p style=\"font-size:14px;color:#4b5563\">{e(reward['terms'])}</p>" if reward.get("terms") else ""
    if reward["kind"] == "claim":
        body = (f"<p>You have now introduced {count} care leaders to the Well-Led Network. Thank you.</p>"
                f"<p>You have unlocked the <strong>{e(reward['name'])}</strong>. It is free for you, and it takes "
                f"about 30 seconds to claim:</p>"
                f"<p style=\"margin:22px 0\"><a href=\"{e(link)}\" style=\"display:inline-block;background:#142B4C;color:#ffffff;"
                f"text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:bold\">Claim my {e(reward['name'])}</a></p>"
                f"<p>Sign in with this email address, tick the terms box and press Complete. The total shows Free, "
                f"and no card is needed. This link is just for you, so please don't share it.</p>{terms}{next_line}")
        return f"You unlocked the {reward['name']}", body
    body = (f"<p>You have now introduced {count} care leaders to the Well-Led Network. Thank you.</p>"
            f"<p>You have unlocked the <strong>{e(reward['name'])}</strong>. Here is how to claim it:</p>"
            f"<ol><li>Open this page: <a href=\"{e(link)}\">{e(link)}</a></li>"
            f"<li>Enter your personal code <strong style=\"font-size:18px;letter-spacing:1px\">{e(code)}</strong> at checkout. "
            f"The total comes to nothing.</li><li>Sign in with this email address and it is yours.</li></ol>"
            f"<p>The code works once and is for you only.</p>{terms}{next_line}")
    return f"You unlocked the {reward['name']}", body


# ---------------------------------------------------------------- reporting
def stats(store) -> dict:
    s = store.r.hgetall(K_STATS) if _r(store) else {}
    s = {k: int(v) for k, v in s.items()}
    s["pending"] = store.r.scard(K_PENDING) if _r(store) else 0
    s["codes"] = {p: store.r.llen(K_CODES + p) for p in POOLS} if _r(store) else {}
    s["live"] = live()
    return s


def report(store) -> str:
    import briefing

    s = stats(store)
    try:
        members = briefing.list_count(store)
    except Exception:
        members = None
    rewards = ", ".join(f"{m}: {s.get(f'reward_{m}', 0)}" for m in sorted(REWARDS))
    participants = s.get("participants", 0)
    referrers = len({_get(store, k).get("referrer") for k in store.r.scan_iter(K_P + "*")
                     if (_get(store, k) or {}).get("referrer")}) if _r(store) else 0
    lines = [
        "THE WELL-LED 1,000, weekly report" + ("" if s["live"] else " (TEST MODE, not launched)"),
        f"Community members (Brevo 'WLN Members'): {members if members is not None else 'unknown'} of 1,000",
        f"New community joins seen since launch: {s.get('community_joins', 0)}",
        f"Referrals credited (joined the community): {s.get('converted', 0)}",
        f"Campaign participants: {participants} ({s.get('participants_organic', 0)} signed up themselves, "
        f"{s.get('participants_referred', 0)} came through a link)",
        f"People who have referred at least one person: {referrers}",
        f"Rewards delivered by milestone: {rewards}",
        f"Rewards waiting: {s['pending']}" + (" (say: refer pending)" if s["pending"] else ""),
        f"Referrals rejected (self, duplicate, disposable, existing member): {s.get('rejected', 0)}",
    ]
    low = [k for k, v in s["codes"].items() if v < 5 and any(r.get("pool") == k for r in REWARDS.values())]
    if low:
        lines.append("NEEDS ATTENTION: fewer than 5 codes left for " + ", ".join(low) + ". Make more in the Well-Led Network "
                     "and load them with: refer codes <pool> CODE1 CODE2 ...")
    return "\n".join(lines)


def daily(store, send) -> str:
    """Morning job: retry anything pending; Monday sends the weekly report."""
    out = retry_pending(store, send)
    if now().weekday() == 0 and _r(store) and store.r.get("wl1000:launched_report") != now().strftime("%Y-%m-%d"):
        store.r.set("wl1000:launched_report", now().strftime("%Y-%m-%d"))
        if store.r.hlen(K_STATS):
            send(report(store))
    return out


# ---------------------------------------------------------------- webhook + Telegram
def on_webhook(store, payload: dict, send) -> dict:
    if _r(store):
        store.r.set("wl1000:last_raw", json.dumps(payload)[:20000])
    kind = (payload.get("type") or "").lower()
    if kind == "participation":
        return on_participation(store, payload, send)
    if kind == "milestonereached":
        return on_milestone(store, payload, send)
    if kind == "rewardredeemed":
        _log(store, f"reward marked redeemed in Viral Loops: {(payload.get('user') or {}).get('email')}")
        return {"ok": True}
    if kind in ("conversion", "converted"):
        _log(store, f"conversion confirmed by Viral Loops: {(payload.get('user') or {}).get('email')}")
        return {"ok": True}
    return {"ok": True, "result": f"ignored type {kind!r}"}


def handle(text: str, store, send) -> str | None:
    t = (text or "").strip()
    low = t.lower()
    if not low.startswith("refer"):
        return None
    parts = t.split()
    if low in ("refer", "refer status"):
        s = stats(store)
        return (f"The Well-Led 1,000 ({'LIVE' if s['live'] else 'TEST MODE'}): {s.get('participants', 0)} participants, "
                f"{s.get('converted', 0)} referrals credited, {s.get('rejected', 0)} rejected, {s['pending']} rewards waiting. "
                f"Say refer report for the full picture.")
    if low == "refer report":
        return report(store)
    if low == "refer retry":
        return retry_pending(store, send)
    if low == "refer pending":
        items = sorted(store.r.smembers(K_PENDING)) if _r(store) else []
        if not items:
            return "Nothing waiting."
        rows = []
        for it in items:
            email, _, m = it.partition("|")
            why = (_get(store, K_REWARD + f"{email}:{m}") or {}).get("why", "") if m.isdigit() else "referral credit"
            rows.append(f"{email}: {m} ({why})")
        return "\n".join(rows)
    if low.startswith("refer codes"):
        if len(parts) == 2:
            return "Codes left: " + ", ".join(f"{k} {v}" for k, v in stats(store)["codes"].items())
        pool = parts[2].lower()
        if pool not in POOLS:
            return f"Pool must be one of: {', '.join(POOLS)}"
        codes = [c.strip(",;") for c in parts[3:] if c.strip(",;")]
        if not codes:
            return "Add the codes after the pool name, separated by spaces."
        if _r(store):
            store.r.rpush(K_CODES + pool, *codes)
        return f"Loaded {len(codes)} {pool} codes. {store.r.llen(K_CODES + pool) if _r(store) else 0} available."
    if low == "refer setup":
        return (f"Viral Loops webhook URL (paste into the campaign's Webhook integration, all four events):\n"
                f"https://wln-replies.onrender.com/vl/{token()}\n\nBrevo: {ensure_attributes(store)}; list id {list_id(store)}.")
    return ("refer: status. refer report: weekly report now. refer pending: rewards waiting. refer retry: try them again. "
            "refer setup: webhook link.")
