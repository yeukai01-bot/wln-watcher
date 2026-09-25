"""Printed letters for report radar leads, posted through Intelliprint (UK hybrid mail).

Nothing is posted automatically. Yeukai replies "letters" on Telegram to see the waiting letters (each with a
preview link), then "post L1 L3" or "post all". Only then is each letter sent to Intelliprint, which prints it
and hands it to Royal Mail (2nd class, 84p + VAT per one-page letter at the time of writing).

The very first "post" runs in Intelliprint's test mode (no charge, nothing posted) so the set-up can be checked;
the next "post" is real. The trial stops on LETTERS_UNTIL (default 1 October 2026) unless that date is changed.

Env: INTELLIPRINT_API_KEY (from the Intelliprint dashboard), CQC_API_KEY (for postal addresses),
LETTERS_UNTIL (YYYY-MM-DD), LETTERS_POSTAGE (uk_second_class or uk_first_class), LETTERS_DAYS (default 7).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

API = "https://api.intelliprint.net/v1"
BATCH_KEY = "wln:letter_batch"
TESTED_KEY = "letters:tested"
CALL_LINK = "tfft.io/CRIkyvF"
MOBILE = os.getenv("LETTER_PHONE", "07875 400753")
REPLY_EMAIL = os.getenv("LETTER_EMAIL", "kajidoricollective@gmail.com")
ARTICLE = {
    "Inadequate": "welllednetwork.com/blog/rated-inadequate-what-happens-next",
    "Requires improvement": "welllednetwork.com/blog/audits-missed-what-the-inspector-found",
}


def _until() -> date:
    try:
        return date.fromisoformat(os.getenv("LETTERS_UNTIL", "2026-10-01"))
    except ValueError:
        return date(2026, 10, 1)


def trial_open() -> bool:
    return date.today() <= _until()


# ---------------------------------------------------------------- addresses
def _title(name: str) -> str:
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in name.split())


def ensure_address(p: dict) -> bool:
    """Adds the service's postal address from the CQC register (public data). Returns True if it has one."""
    if p.get("postcode") and p.get("address_lines"):
        return True
    key = os.getenv("CQC_API_KEY", "")
    if not key or not p.get("location_id"):
        return False
    try:
        r = requests.get(f"https://api.service.cqc.org.uk/public/v1/locations/{p['location_id']}",
                         headers={"Ocp-Apim-Subscription-Key": key, "User-Agent": "Mozilla/5.0"}, timeout=40)
        r.raise_for_status()
        loc = r.json()
    except Exception:
        return False
    lines = [loc.get(k) or "" for k in ("postalAddressLine1", "postalAddressLine2", "postalAddressTownCity", "postalAddressCounty")]
    p["address_lines"] = [x.strip() for x in lines if x and x.strip()]
    p["postcode"] = (loc.get("postalCode") or "").strip()
    return bool(p["postcode"] and p["address_lines"])


def address_for(p: dict) -> dict:
    manager = _title(p.get("registered_manager") or "")
    name = f"{manager}, Registered Manager" if manager else "The Registered Manager"
    line = ", ".join([p.get("location_name", "")] + p.get("address_lines", []))
    return {"name": name, "line": line, "postcode": p.get("postcode", ""), "country": "GB"}


# ---------------------------------------------------------------- the letter
def _weak_sentence(p: dict) -> str:
    weak = p.get("weak_key_questions") or []
    if not weak:
        return f"The overall rating was {p.get('rating', '')}."
    groups: dict[str, list[str]] = {}
    for item in weak:
        kq, _, rating = item.partition(": ")
        groups.setdefault(rating, []).append(kq)
    parts = []
    for rating, kqs in groups.items():
        names = kqs[0] if len(kqs) == 1 else ", ".join(kqs[:-1]) + " and " + kqs[-1]
        parts.append(f"{names} {'was' if len(kqs) == 1 else 'were'} rated {rating}")
    s = "; ".join(parts)
    return s[0].upper() + s[1:] + "."


def letter_paragraphs(p: dict) -> tuple[str, list[str]]:
    manager = _title(p.get("registered_manager") or "")
    greeting = f"Dear {manager.split()[0]}," if manager else "Dear Registered Manager,"
    when = p.get("report_date") or "recently"
    when = f"on {when}" if re.match(r"\d", when) else when
    paras = [
        f"I read the CQC report for {p.get('location_name', 'your service')}, published {when}. {_weak_sentence(p)} "
        "I know from experience how heavy the weeks after a report like this can feel, for you and for the whole team.",
        "I was a CQC Registered Manager for 15 years and led two services to Good. Today I run The Well-Led Network, "
        "a free community where care leaders in England share what actually works.",
        "I would like to offer you a free 30-minute call, one registered manager to another, to talk through the report "
        "and plan your first 30 days. There is no cost and no obligation.",
    ]
    return greeting, paras


def letter_html(p: dict) -> str:
    greeting, paras = letter_paragraphs(p)
    e = html.escape
    article = ARTICLE.get(p.get("rating", ""), ARTICLE["Requires improvement"])
    body = "".join(f"<p>{e(x)}</p>" for x in paras)
    return f"""<div style="font-family: Georgia, 'Times New Roman', serif; font-size: 11.5pt; line-height: 1.5; color: #1a1a1a;">
<p style="text-align:right; font-size:10pt; color:#444;">Yeukai Kajidori<br>The Well-Led Network<br>welllednetwork.com<br>{e(REPLY_EMAIL)}<br>{e(MOBILE)}<br><br>{datetime.now():%d %B %Y}</p>
<p>{e(greeting)}</p>
{body}
<p><b>Book a time that suits you:</b> {e(CALL_LINK)}<br>
<b>Or call or text me:</b> {e(MOBILE)}<br>
<b>Free article you may find useful:</b> {e(article)}</p>
<p>Kind regards,</p>
<p><b>Yeukai Kajidori</b><br>Founder, The Well-Led Network</p>
<p style="font-size:8.5pt; color:#555; margin-top:18pt;">I found your service's details on the public CQC register. If you would prefer not to hear from me again,
email {e(REPLY_EMAIL)} with the word "remove" and I will not contact you.</p>
</div>"""


def preview_token(pid: str) -> str:
    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), f"letter:{pid}".encode(), hashlib.sha256).hexdigest()[:20]


# ---------------------------------------------------------------- batch handling
def waiting(store) -> list[dict]:
    days = int(os.getenv("LETTERS_DAYS", "7"))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    suppressed = set(store.suppressed())
    out = []
    for p in store.prospects():
        if p.get("radar_date", "") < since or p.get("letter_status") in ("posted", "skipped"):
            continue
        if p.get("email") and p["email"] in suppressed:
            continue
        out.append(p)
    return out


def _save_batch(store, ids: list[str]) -> None:
    if store.r:
        store.r.set(BATCH_KEY, json.dumps(ids))
    else:
        d = store._load()
        d["letter_batch"] = ids
        store._save(d)


def _batch(store) -> list[str]:
    if store.r:
        v = store.r.get(BATCH_KEY)
        return json.loads(v) if v else []
    return store._load().get("letter_batch", [])


def list_letters(store, base_url: str) -> str:
    if not trial_open():
        return f"The letters trial ended on {_until():%d %B %Y}. Change LETTERS_UNTIL on Render to carry on."
    items = waiting(store)
    if not items:
        return "No letters waiting. New radar leads appear here each morning."
    lines, ids, no_addr = [], [], []
    for p in items:
        if not ensure_address(p):
            no_addr.append(p.get("location_name", ""))
            continue
        store.put_prospect(p)
        ids.append(p["id"])
        n = len(ids)
        flag = " (large organisation)" if p.get("large_org") else ""
        lines.append(f"L{n}. {p['location_name']}, {p['postcode']}{flag}\n   To: {address_for(p)['name']}\n"
                     f"   Read it: {base_url}/letter/{p['id']}/{preview_token(p['id'])}")
    _save_batch(store, ids)
    msg = (f"Letters ready to post ({len(ids)}), from the last {os.getenv('LETTERS_DAYS', '7')} days of radar leads. "
           f"About 84p + VAT each, 2nd class, printed and posted by Intelliprint.\n\n" + "\n".join(lines)
           + "\n\nReply 'post L1 L3' or 'post all' to send. 'skip L2' drops one for good.")
    if no_addr:
        msg += "\n\nNo postal address on the CQC register for: " + ", ".join(no_addr)
    return msg


def _pick(store, which: str) -> tuple[list[dict], list[str]]:
    ids = _batch(store)
    if not ids:
        return [], ["Reply 'letters' first to see the list."]
    if which.strip() in ("all", "everything", "them all"):
        nums = list(range(1, len(ids) + 1))
    else:
        nums = [int(x) for x in re.findall(r"l?(\d+)", which)]
    chosen, missing = [], []
    for n in dict.fromkeys(nums):
        p = store.get_prospect(ids[n - 1]) if 0 < n <= len(ids) else None
        (chosen.append(p) if p else missing.append(f"L{n}"))
    return chosen, missing


def skip(store, which: str) -> str:
    chosen, missing = _pick(store, which)
    for p in chosen:
        p["letter_status"] = "skipped"
        store.put_prospect(p)
    msg = ("No letter will be sent to: " + ", ".join(p["location_name"] for p in chosen)) if chosen else "Nothing skipped."
    return msg + (f"\nNot on the list: {', '.join(missing)}" if missing else "")


def _create_print(p: dict, test: bool) -> dict:
    key = os.getenv("INTELLIPRINT_API_KEY", "")
    a = address_for(p)
    data = {
        "type": "letter", "content": letter_html(p), "confirmed": "true", "testmode": "true" if test else "false",
        "reference": f"CQC radar {p.get('location_name', '')}"[:60],
        "recipients[0][address][name]": a["name"], "recipients[0][address][line]": a["line"],
        "recipients[0][address][postcode]": a["postcode"], "recipients[0][address][country]": "GB",
        "postage[service]": os.getenv("LETTERS_POSTAGE", "uk_second_class"), "postage[ideal_envelope]": "c5",
    }
    r = None
    for auth in (f"Bearer {key}", key):
        r = requests.post(f"{API}/prints", headers={"Authorization": auth}, data=data, timeout=90)
        if r.status_code != 401:
            break
    if r.status_code >= 400:
        raise RuntimeError(f"Intelliprint said {r.status_code}: {r.text[:300]}")
    return r.json()


def _cost(job: dict) -> str:
    c = job.get("cost") or {}
    if isinstance(c, dict):
        amt = c.get("after_tax") or c.get("amount") or c.get("total")
        return f"£{amt}" if amt is not None else ""
    return f"£{c}" if c else ""


def post(store, which: str) -> str:
    if not trial_open():
        return f"The letters trial ended on {_until():%d %B %Y}. Nothing was posted."
    if not os.getenv("INTELLIPRINT_API_KEY"):
        return "Letters are not switched on yet: add your Intelliprint API key to Render as INTELLIPRINT_API_KEY."
    chosen, missing = _pick(store, which)
    if not chosen:
        return "Nothing posted. " + " ".join(missing)
    test = not store.has(TESTED_KEY)
    if test:
        p = chosen[0]
        try:
            job = _create_print(p, test=True)
        except Exception as exc:
            return f"Test letter failed, nothing was posted or charged.\n{exc}"
        store.add_many([TESTED_KEY])
        return (f"Test run passed for {p['location_name']} (Intelliprint test mode: no charge, nothing posted"
                + (f", it would cost {_cost(job)}" if _cost(job) else "") + "). You can see it in your Intelliprint dashboard.\n\n"
                f"Reply 'post {which}' again to post for real.")
    done, failed = [], []
    for p in chosen:
        if p.get("letter_status") == "posted":
            continue
        if not ensure_address(p):
            failed.append(f"{p['location_name']}: no postal address")
            continue
        try:
            job = _create_print(p, test=False)
        except Exception as exc:
            failed.append(f"{p['location_name']}: {exc}")
            continue
        p["letter_status"], p["letter_id"], p["letter_at"] = "posted", job.get("id", ""), int(time.time())
        store.put_prospect(p)
        done.append(f"{p['location_name']} {_cost(job)}".strip())
    msg = ("Posted (printed and in the post by the next working day):\n" + "\n".join(done)) if done else "Nothing posted."
    if failed:
        msg += "\n\nNot posted:\n" + "\n".join(failed)
    if missing:
        msg += "\n\nNot on the list: " + ", ".join(missing)
    if done:
        msg += "\n\nCall them 3 to 4 working days from now and open with: 'I sent you a letter about your report.'"
    return msg
