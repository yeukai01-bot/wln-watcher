"""Printed letters for report radar leads, printed and posted by Stannp (UK hybrid mail).

Each morning, once the radar has written its Report Reviews, the bot sends Yeukai the list of letters waiting
(each with a link to the exact PDF). Nothing is posted until he replies "post all" or "post L1 L3".
Each letter is the same design as the hand-built batch of 6 Nov 2026: a one page letter plus the free review
(letter_pdf.py). Large organisations are left out automatically; services with a serious notice on their CQC
page (suspension, special measures, cancellation, prosecution, a death) are held back and only go if Yeukai
names them ("post L2"), never with "post all".

The very first "post" runs in Stannp's test mode (no charge, nothing posted) so the set-up can be checked;
the next "post" is real. A monthly spending cap (LETTERS_CAP, default 150 pounds) stops posting once reached.

Env: STANNP_API_KEY (Stannp, Settings, API Settings), STANNP_API (default https://api-eu1.stannp.com/v1),
CQC_API_KEY (postal addresses), LETTERS_CLASS ("second" default, or "first"), LETTERS_CAP (pounds a month,
default 150), LETTERS_START (YYYY-MM-DD, only radar leads from this day on; default 2026-11-07),
LETTERS_DAYS (how many days back to list, default 7).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

API = os.getenv("STANNP_API", "https://api-eu1.stannp.com/v1").rstrip("/")
BATCH_KEY = "wln:letter_batch"
TESTED_KEY = "letters:stannp_tested"
SPEND_KEY = "wln:letter_spend"
SERIOUS = re.compile(r"suspend|special measures|cancel(?:led|lation)? (?:the|its|their)? ?registration|prosecut|"
                     r"urgent(?:ly)? (?:imposed|varied|action)|died|death", re.I)


def _class() -> str:
    return "first" if os.getenv("LETTERS_CLASS", "second").strip().lower().startswith("first") else "second"


def _cap() -> float:
    try:
        return float(os.getenv("LETTERS_CAP", "150"))
    except ValueError:
        return 150.0


def _start() -> str:
    return os.getenv("LETTERS_START", "2026-11-07")


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



# ---------------------------------------------------------------- monthly spend
def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def spent(store) -> float:
    if store.r:
        return float(store.r.hget(SPEND_KEY, _month()) or 0)
    return float(store._load().get("letter_spend", {}).get(_month(), 0))


def _add_spend(store, amount: float) -> None:
    if store.r:
        store.r.hincrbyfloat(SPEND_KEY, _month(), amount)
        return
    d = store._load()
    d.setdefault("letter_spend", {})
    d["letter_spend"][_month()] = round(d["letter_spend"].get(_month(), 0) + amount, 2)
    store._save(d)


# ---------------------------------------------------------------- serious notices on the CQC page
def check_serious(p: dict) -> str:
    """Returns the serious notice on the service's CQC page (or ''). Cached on the prospect."""
    if "serious_notice" in p:
        return p["serious_notice"]
    note = ""
    try:
        import prospects
        from bs4 import BeautifulSoup

        page = prospects._site(f"/location/{p['location_id']}")
        text = " ".join(BeautifulSoup(page, "html.parser").get_text(" ").split())
        for m in re.finditer(r"Important:(.{0,400}?)(?=Important:|Overview|$)", text):
            if SERIOUS.search(m.group(1)):
                note = m.group(1).strip()[:220]
                break
    except Exception as exc:
        print("serious notice check failed:", exc)
    p["serious_notice"] = note
    return note

# ---------------------------------------------------------------- batch handling
def preview_token(pid: str) -> str:
    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), f"letter:{pid}".encode(), hashlib.sha256).hexdigest()[:20]


# ---------------------------------------------------------------- batch handling
def waiting(store) -> list[dict]:
    days = int(os.getenv("LETTERS_DAYS", "7"))
    since = max((datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d"), _start())
    suppressed = set(store.suppressed())
    out = []
    for p in store.prospects():
        if p.get("radar_date", "") < since or p.get("letter_status") in ("posted", "skipped"):
            continue
        if p.get("email") and p["email"] in suppressed:
            continue
        if not p.get("review"):
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
    if not os.getenv("STANNP_API_KEY"):
        return "Letters are not switched on yet: add your Stannp API key to Render as STANNP_API_KEY."
    items = waiting(store)
    if not items:
        return "No letters waiting. New radar leads appear here each morning."
    lines, ids, no_addr, left_out = [], [], [], []
    for p in items:
        if p.get("large_org"):
            p["letter_status"] = "skipped"
            p["letter_note"] = "large organisation"
            store.put_prospect(p)
            left_out.append(p.get("location_name", ""))
            continue
        if not ensure_address(p):
            no_addr.append(p.get("location_name", ""))
            continue
        serious = check_serious(p)
        store.put_prospect(p)
        ids.append(p["id"])
        n = len(ids)
        flag = f"\n   HELD: {serious}" if serious else ""
        lines.append(f"L{n}. {p['location_name']}, {p['postcode']} ({p.get('rating', '')}){flag}\n   To: {address_for(p)['name']}\n"
                     f"   Read it: {base_url}/letter/{p['id']}/{preview_token(p['id'])}")
    _save_batch(store, ids)
    left = _cap() - spent(store)
    msg = (f"Letters ready to post ({len(ids)}): your letter plus the free report review, {_class()} class, printed and posted by Stannp. "
           f"This month: £{spent(store):.2f} of your £{_cap():.0f} cap used, £{max(left, 0):.2f} left.\n\n" + "\n".join(lines)
           + "\n\nReply 'post all' to send them (held ones stay back), 'post L1 L3' for particular ones, or 'skip L2' to drop one for good.")
    if left_out:
        msg += "\n\nLeft out (large organisation): " + ", ".join(left_out)
    if no_addr:
        msg += "\n\nNo postal address on the CQC register for: " + ", ".join(no_addr)
    return msg


def _pick(store, which: str) -> tuple[list[dict], list[str]]:
    """Letters chosen by 'all' leave out held ones (serious notice); naming a letter sends it anyway."""
    ids = _batch(store)
    if not ids:
        return [], ["Reply 'letters' first to see the list."]
    if which.strip() in ("all", "everything", "them all"):
        nums = list(range(1, len(ids) + 1))
    else:
        nums = [int(x) for x in re.findall(r"l?(\d+)", which)]
    chosen, missing = [], []
    every = which.strip() in ("all", "everything", "them all")
    for n in dict.fromkeys(nums):
        p = store.get_prospect(ids[n - 1]) if 0 < n <= len(ids) else None
        if p and every and p.get("serious_notice"):
            missing.append(f"L{n} held ({p['location_name']}): reply 'post L{n}' if you want it sent")
            continue
        (chosen.append(p) if p else missing.append(f"L{n}"))
    return chosen, missing


def skip(store, which: str) -> str:
    chosen, missing = _pick(store, which)
    for p in chosen:
        p["letter_status"] = "skipped"
        store.put_prospect(p)
    msg = ("No letter will be sent to: " + ", ".join(p["location_name"] for p in chosen)) if chosen else "Nothing skipped."
    return msg + (f"\nNot on the list: {', '.join(missing)}" if missing else "")



# ---------------------------------------------------------------- Stannp
def _recipient(p: dict) -> dict:
    manager = _title(p.get("registered_manager") or "")
    first, _, last = manager.partition(" ")
    lines = list(p.get("address_lines") or [])
    town = lines[-1] if len(lines) > 1 else ""
    street = lines[:-1] if len(lines) > 1 else lines
    r = {"recipient[firstname]": first or "The Registered", "recipient[lastname]": last or "Manager",
         "recipient[job_title]": "Registered Manager", "recipient[company]": p.get("location_name", ""),
         "recipient[address1]": street[0] if street else "", "recipient[address2]": street[1] if len(street) > 1 else "",
         "recipient[address3]": ", ".join(street[2:]), "recipient[city]": town,
         "recipient[postcode]": p.get("postcode", ""), "recipient[country]": "GB"}
    return r


def _create_letter(p: dict, test: bool) -> dict:
    import letter_pdf

    pdf, pages = letter_pdf.build_pdf(p)
    data = {"test": "true" if test else "false", "duplex": "true", "clearzone": "true",
            "tags": f"wln-radar,{p.get('radar_date', '')}", **_recipient(p)}
    if _class() == "first":
        data["addons"] = "FIRST_CLASS"
    r = requests.post(f"{API}/letters/create", auth=(os.getenv("STANNP_API_KEY", ""), ""), data=data,
                      files={"file": (f"letter-{p['id']}.pdf", pdf, "application/pdf")}, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not body.get("success"):
        raise RuntimeError(f"Stannp said {r.status_code}: {(body.get('error') or r.text)[:300]}")
    out = body.get("data") or {}
    out["pages"] = pages
    return out


def _cost(job: dict) -> float:
    try:
        return float(job.get("cost") or 0)
    except (TypeError, ValueError):
        return 0.0


def post(store, which: str) -> str:
    if not os.getenv("STANNP_API_KEY"):
        return "Letters are not switched on yet: add your Stannp API key to Render as STANNP_API_KEY."
    chosen, missing = _pick(store, which)
    if not chosen:
        return "Nothing posted. " + " ".join(missing)
    test = not store.has(TESTED_KEY)
    if test:
        p = chosen[0]
        if not ensure_address(p):
            return f"Test letter not made: no postal address for {p['location_name']}."
        try:
            job = _create_letter(p, test=True)
        except Exception as exc:
            return f"Test letter failed, nothing was posted or charged.\n{exc}"
        store.add_many([TESTED_KEY])
        return (f"Test run passed for {p['location_name']} (Stannp test mode: no charge, nothing posted"
                + (f", it would cost £{_cost(job):.2f}" if _cost(job) else "") + ").\n"
                + (f"Stannp's proof of the printed letter: {job.get('pdf')}\n" if job.get("pdf") else "")
                + f"\nReply 'post {which}' again to post for real.")
    done, failed, capped = [], [], []
    total = 0.0
    for p in chosen:
        if p.get("letter_status") == "posted":
            continue
        guess = float(os.getenv("LETTERS_GUESS", "2.30"))
        if spent(store) + guess > _cap():
            capped.append(p["location_name"])
            continue
        if not ensure_address(p):
            failed.append(f"{p['location_name']}: no postal address")
            continue
        try:
            job = _create_letter(p, test=False)
        except Exception as exc:
            failed.append(f"{p['location_name']}: {exc}")
            continue
        cost = _cost(job) or guess
        _add_spend(store, cost)
        total += cost
        p["letter_status"], p["letter_id"], p["letter_at"] = "posted", str(job.get("id", "")), int(time.time())
        p["letter_cost"], p["letter_class"] = cost, _class()
        store.put_prospect(p)
        done.append(f"{p['location_name']} (£{cost:.2f})")
    msg = (f"Posted, {_class()} class (printed by Stannp and in the post by the next working day):\n" + "\n".join(done)
           + f"\nToday £{total:.2f}. This month £{spent(store):.2f} of your £{_cap():.0f} cap.") if done else "Nothing posted."
    if capped:
        msg += (f"\n\nMonthly cap of £{_cap():.0f} reached, so these were not posted: " + ", ".join(capped)
                + ". Raise LETTERS_CAP on Render, or they can go next month.")
    if failed:
        msg += "\n\nNot posted:\n" + "\n".join(failed)
    if missing:
        msg += "\n\nNot posted: " + "; ".join(missing)
    return msg
