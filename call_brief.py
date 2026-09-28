"""Pre-call brief for Enrolment Calls: so Yeukai walks into every sales call already knowing the service.

When someone books the Enrolment Call, this finds their CQC location (from the report link they paste on the
booking form, or by searching their service name on cqc.org.uk), reads the published report, and writes:
  - what CQC found, and what is likely underneath it
  - how their stated worry connects to the report
  - how to open the call, the questions to ask, the offer that fits, objections, and the close
It arrives on Telegram as a message and a printable .md file. Telegram command: brief <CQC link or service name>.
"""
from __future__ import annotations

import re
import threading
from urllib.parse import quote, urlencode

import ai

CHECKOUTS = {
    "Compliance Vault": ("£297", "https://www.welllednetwork.com/paywall/447/checkout"),
    "Accelerator": ("£1,297 or 3 x £447", "https://www.welllednetwork.com/paywall/448/checkout"),
    "VIP Cohort": ("£2,997", "https://www.welllednetwork.com/paywall/449/checkout"),
    "Founding Place": ("£997 (first cohort only, starts Thursday 8 October 2026, 8pm)", "https://www.welllednetwork.com/paywall/450/checkout"),
}

OFFER = """The Requires Improvement to Good Accelerator (Yeukai's programme, method: The 90-Day Improvement Reset:
find the root cause, fix it in the right order, embed the change, prove it is working. Big idea: don't just fix
the findings, fix what keeps creating them.)
Tiers:
- Compliance Vault, £297: self-serve library of templates, trackers and guides. For confident managers who only need tools.
- Accelerator, £1,297 or 3 x £447: 4 module course with workbooks, weekly live implementation clinics (Thursdays 8pm UK,
  8 weeks), private community channel, the full Vault. The core offer for most Requires Improvement services.
- VIP Cohort, £2,997: everything in the Accelerator plus VIP extras and closer one to one support. For owners or NIs with
  more than one weak key question, several sites, or high stakes (contracts, occupancy, enforcement).
- Founding Place, £997 instead of £1,297: same access as the Accelerator, first cohort only (starts 8 October 2026).
  Only recommend it if today is before 8 October 2026; after that recommend the Accelerator.
Yeukai: 23 years in UK health and social care, 15 as a CQC Registered Manager, led two services to Good."""

BRIEF_SYSTEM = f"""You prepare Yeukai Kajidori for a 20 minute Enrolment Call with a care provider who booked it.
Goal of the call: understand their CQC report and pressure, show them he understands their situation better than
anyone they have spoken to, and if it is a genuine fit, enrol them in the right tier on the call.

{OFFER}

Rules:
- Use ONLY the report text and booking answers given. Never invent findings, numbers, names or dates. If the report
  could not be read, say so and base the brief on the ratings and their answers only.
- Root causes are likely causes, phrased as such.
- Direct response selling, but ethical: lead with the outcome they want and the fear driving them; never pressure,
  never promise a rating. If they are not a fit (for example already rated Good, or a hospital), say so and say what
  to offer instead (the free Well-Led Network, a Report Review, or nothing).
- British English, plain, calm, no emojis, no dashes as punctuation.
- Each bullet one sentence, under 30 words. The close lines are word for word what Yeukai can say.

Reply with JSON only:
{{
 "snapshot": "one sentence: who they are, their rating, when published, which key questions are weak",
 "found": ["3 or 4 key findings, each starting with the key question in brackets"],
 "underneath": ["2 or 3 likely root causes"],
 "their_worry": "one or two sentences connecting what they said worries them to the report",
 "open_with": "the first thing to say after hello, naming one specific finding so they know he has read it",
 "questions": ["5 questions in order that uncover the cost of staying where they are, their deadline, who else decides, and what they have already tried"],
 "stakes": ["2 or 3 things likely at stake for this service, only if supported by the report or their answers"],
 "fit": "fit | maybe | not a fit, with one sentence why",
 "recommend": "Compliance Vault | Accelerator | VIP Cohort | Founding Place",
 "why_this_tier": "one sentence",
 "objections": [{{"they_say": "...", "you_say": "..."}}],
 "close": ["3 short lines Yeukai says to close, ending with a clear question"],
 "if_not_now": "one sentence on the next step if they will not decide on the call"
}}"""


def _loc_id(text: str) -> str:
    m = re.search(r"(1-\d{5,})", text or "")
    return m.group(1) if m else ""


def find_location(answers: list, service_name: str) -> tuple[str, str]:
    """(location id, how it was found). Report link first, then a search of cqc.org.uk by service name."""
    import prospects

    for _lab, val in answers or []:
        lid = _loc_id(val)
        if lid:
            return lid, "from the report link they gave"
    name = service_name or next((v for lab, v in answers or [] if "service name" in lab.lower()), "")
    if not name:
        return "", ""
    try:
        params = [("query", name), ("display", "list"), ("filters[]", "archived:active")]
        html = prospects._site("/search/all?" + urlencode(params, quote_via=quote))
    except Exception:
        return "", ""
    m = re.search(r'href="\s*/location/(1-\d+)\s*"[^>]*>([^<]+)</a>', html)
    if not m:
        return "", ""
    return m.group(1), f"matched by searching '{name}' on cqc.org.uk (found '{' '.join(m.group(2).split())}'), please check it is theirs"


def build(b: dict) -> tuple[dict, str, dict]:
    """Returns (profile, how found, brief dict)."""
    import prospects
    import review

    answers = b.get("answers") or []
    lid, how = find_location(answers, "")
    prof: dict = {"location_name": next((v for lab, v in answers if "service name" in lab.lower()), "") or "their service"}
    text = ""
    if lid:
        detail = prospects.site_detail(lid)
        prof.update({"location_id": lid, "cqc_page": f"https://www.cqc.org.uk/location/{lid}",
                     "rating": detail.get("overall", "unknown"), "report_date": detail.get("published", "unknown"),
                     "weak_key_questions": detail.get("weak", [])})
        try:
            p, _why = prospects.build((lid, {"name": prof["location_name"], "rating": prof["rating"]}))
            if p:
                prof.update({k: p.get(k) for k in ("location_name", "provider_name", "service_type", "town", "sites",
                                                   "registered_manager", "phone", "website")})
        except Exception:
            pass
        text = review.report_text(prospects._site, lid, prof.get("weak_key_questions") or [])
    from datetime import datetime
    from zoneinfo import ZoneInfo

    user = (
        f"TODAY: {datetime.now(ZoneInfo('Europe/London')):%d %B %Y}\n\n"
        f"BOOKING\nName: {b.get('name')}\nCall time: {b.get('start_raw') or b.get('start')}\n"
        + "\n".join(f"{lab}: {val}" for lab, val in answers)
        + f"\n\nCQC LOCATION ({how or 'not found'})\n"
        + "\n".join(f"{k}: {v}" for k, v in prof.items() if v not in (None, "", []))
        + f"\n\nREPORT TEXT:\n{text or '(the report could not be read)'}"
    )
    brief = ai._json(ai._ask(BRIEF_SYSTEM, user, max_tokens=3000))
    return prof, how, brief


def render(b: dict, prof: dict, how: str, br: dict) -> str:
    rec = br.get("recommend", "Accelerator")
    price, link = CHECKOUTS.get(rec, CHECKOUTS["Accelerator"])
    bl = lambda xs: "\n".join(f"- {x}" for x in xs or [])
    lines = [
        f"CALL BRIEF: {b.get('name') or 'booking'}, {prof.get('location_name')}",
        f"Call: {b.get('when', '')}",
        "",
        br.get("snapshot", ""),
    ]
    if prof.get("cqc_page"):
        lines += [f"CQC page: {prof['cqc_page']} ({how})"]
    else:
        lines += ["CQC page: not found. Ask them for the link at the start of the call."]
    lines += [
        "", "WHAT CQC FOUND", bl(br.get("found")),
        "", "LIKELY UNDERNEATH", bl(br.get("underneath")),
        "", "THEIR WORRY", br.get("their_worry", ""),
        "", "OPEN WITH", br.get("open_with", ""),
        "", "ASK", "\n".join(f"{i}. {q}" for i, q in enumerate(br.get("questions") or [], 1)),
        "", "WHAT IS AT STAKE", bl(br.get("stakes")),
        "", f"FIT: {br.get('fit', '')}",
        f"OFFER: {rec}, {price}. {br.get('why_this_tier', '')}",
        f"Checkout: {link}",
        "", "OBJECTIONS",
        "\n".join(f"- They say: {o.get('they_say')}\n  You say: {o.get('you_say')}" for o in br.get("objections") or []),
        "", "CLOSE", "\n".join(br.get("close") or []),
        "", "IF NOT TODAY", br.get("if_not_now", ""),
    ]
    return "\n".join(lines).strip()


def short(br: dict) -> str:
    """Three lines for the 1 hour reminder."""
    rec = br.get("recommend", "Accelerator")
    price, link = CHECKOUTS.get(rec, CHECKOUTS["Accelerator"])
    return (f"Open with: {br.get('open_with', '')}\nOffer: {rec}, {price}\nCheckout: {link}")


def run(b: dict, store, telegram) -> None:
    """Build and send the brief; keep it on the booking so the reminder can repeat the key lines."""
    import bookings

    try:
        prof, how, br = build(b)
        b = {**b, "when": bookings.when(b)}
        text = render(b, prof, how, br)
        saved = bookings.get(store, b["id"]) or b
        saved["brief_short"] = short(br)
        bookings.save(store, saved)
        telegram.send_message(text)
        safe = re.sub(r"[^A-Za-z0-9]+", "-", prof.get("location_name") or "call").strip("-")[:40]
        telegram.send_document(f"call-brief-{safe}.md", text, "Printable call brief")
    except Exception as exc:
        telegram.send_message(f"Could not write the call brief for {b.get('name') or 'the new booking'} "
                              f"({type(exc).__name__}: {str(exc)[:150]}). Reply: brief <CQC link> to try again.")


def start(b: dict, store, telegram) -> None:
    threading.Thread(target=run, args=(b, store, telegram), daemon=True).start()


def on_request(arg: str, store, telegram) -> None:
    """Telegram 'brief <CQC link or service name>'."""
    b = {"id": "manual", "name": "on request", "answers": [("Your service name", arg)] if not _loc_id(arg) else [("CQC report link", arg)]}
    threading.Thread(target=run, args=(b, store, telegram), daemon=True).start()
