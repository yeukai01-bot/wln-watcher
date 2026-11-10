"""Free CQC Report Review: read a service's newly published CQC report and write Yeukai's one page review.

The review is the offer. It goes in the first-contact email (short form) and on a printable page
(full form) at /r/<id>/<token> on the reply service. Yeukai approves every email before it is sent.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import re

from bs4 import BeautifulSoup

import ai

ENROLMENT_CALL = os.getenv("ENROLMENT_CALL_URL", "https://shor.by/enrolment-call")
# Shorby tracking link (from 5 Oct 2026) -> https://www.welllednetwork.com/blog/requires-improvement-to-good
RI_ARTICLE = os.getenv("RI_ARTICLE_URL", "https://shor.by/ri-to-good-guide")
# Shorby tracking link (from 5 Oct 2026) -> https://www.welllednetwork.com/blog/rated-inadequate-what-happens-next
INADEQUATE_ARTICLE = os.getenv("INADEQUATE_ARTICLE_URL", "https://shor.by/inadequate-guide")
KQ_SLUG = {"Safe": "safe", "Effective": "effective", "Caring": "caring", "Responsive": "responsive", "Well-led": "well-led"}
MAX_REPORT_CHARS = 14000


def report_base(location_html: str, lid: str) -> str:
    """Path of the latest assessment report, e.g. /location/1-123/reports/AP27354/overall."""
    m = re.search(rf'href="\s*(/location/{re.escape(lid)}/reports/[A-Za-z0-9]+/overall)\s*"', location_html or "")
    return m.group(1) if m else ""


def _main_text(page: str) -> str:
    soup = BeautifulSoup(page or "", "html.parser")
    node = soup.find("main") or soup.body or soup
    for tag in node.select("nav, script, style, footer, header form, .breadcrumb"):
        tag.decompose()
    text = node.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def report_text(site_get, lid: str, weak: list[str]) -> str:
    """Overall report page plus the pages for each key question rated below Good.
    site_get(path) returns HTML from www.cqc.org.uk. Returns '' if the report cannot be read."""
    try:
        base = report_base(site_get(f"/location/{lid}"), lid)
    except Exception:
        return ""
    if not base:
        return ""
    parts = []
    try:
        parts.append("OVERALL\n" + _main_text(site_get(base)))
    except Exception:
        return ""
    for item in weak:
        kq = item.split(":")[0].strip()
        slug = KQ_SLUG.get(kq)
        if not slug:
            continue
        try:
            parts.append(f"{kq.upper()}\n" + _main_text(site_get(f"{base}/{slug}")))
        except Exception:
            continue
    return "\n\n".join(parts)[:MAX_REPORT_CHARS]


REVIEW_SYSTEM = """You are Yeukai Kajidori: 23 years in UK health and social care, 15 as a CQC Registered Manager,
led two services to a Good rating. You write a short, free review of a care service's newly published CQC
report, to send to its registered manager as a genuine gift. The review must be useful even if they never
speak to you.

Method you use (The 90-Day Improvement Reset): find the root cause, fix it in the right order, embed the
change, prove it is working. A long list of findings usually comes from three or four underlying causes.

Rules:
- Use ONLY what is in the report text given. Never invent findings, numbers, names or events. If the report
  text is missing or thin, keep the findings general and say they come from the published ratings.
- Root causes are your professional reading from the outside, so phrase them as likely causes
  ("this often points to", "the pattern suggests"), never as certain.
- Respectful and kind. The manager is under pressure. Never criticise staff, the provider or CQC.
- British English, plain, calm. No hype, no emojis, no exclamation marks, no dashes or hyphens as punctuation.
- No promises about ratings or inspection outcomes.
- Keep every bullet to one sentence, under 30 words.
- No dates, deadlines, time limits or numbers unless that exact figure appears in the report text. Never say
  "28 days" or any other deadline unless the report says it.
- "fixes" must be things a manager can start on Monday: name the record, meeting, check or person involved.
  Never give directional advice like "improve governance"; say exactly what to do.

Reply with JSON only:
{
 "video_finding": "one short finding in plain words, under 12 words, e.g. 'medicines records were not always accurate'",
 "found": ["3 or 4 key findings, each starting with the key question in brackets, e.g. '(Safe) ...'"],
 "underneath": ["2 or 3 likely root causes connecting the findings"],
 "fix_first": ["3 actions for the next 30 days, in the order you would do them"],
 "evidence": ["2 or 3 things that would show the change is working"],
 "working": "one or two sentences on what the report says is already working, taken only from the report; '' if the report text gives nothing",
 "fixes": [{"title": "the fix as a short instruction, under 10 words", "why": "one sentence tying it to the findings",
            "steps": ["3 concrete steps a manager can start this week, one sentence each"],
            "proof": "one sentence: what they will see when it is working"}],
 "questions": ["3 questions an inspector is likely to ask next time, one per fix, in the inspector's words"],
 "subject": "email subject under 60 characters, no rating in it, e.g. 'A free review of your CQC report, Oak House'",
 "stakes": "one sentence on what is at stake for them now, using only facts in the report (e.g. CQC has asked for an action plan; Safe rated Inadequate with N breaches if N is in the report). No deadlines unless the report gives one.",
 "hook": "one curiosity sentence in your own voice, framed as your view, that names a surprising angle on their report WITHOUT giving the answer, e.g. 'I don't think the medicines finding is really a medicines problem. It shows up in three places in your report.' It must rest on something the report itself says.",
 "context": {"people_supported": "number of people supported if stated, else ''", "breaches": "number of breaches if stated, else ''",
             "action_plan_requested": true or false, "praise": "one short verbatim phrase of praise from people or relatives in the report, under 12 words, or ''"},
 "trace": [{"item": "'found 1', 'found 2' ... for each found item, then 'hook', then 'stakes'", "quote": "a verbatim quote copied exactly from the report text, under 25 words, that supports that item"}]
}
"fixes" has exactly 3 items, in the same order as fix_first."""


def _norm(t: str) -> str:
    t = (t or "").lower()
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"')):
        t = t.replace(a, b)
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", t).split())


def _in_report(quote: str, text: str) -> bool:
    """True when the quote really appears in the report (allowing for spacing and punctuation)."""
    q = _norm(quote)
    return bool(q) and len(q) > 12 and q in _norm(text)


def _numbers_ok(sentence: str, text: str) -> bool:
    """Every number in the sentence must appear in the report, written as a digit or a word."""
    words = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight",
             "9": "nine", "10": "ten", "11": "eleven", "12": "twelve"}
    body = _norm(text)
    for n in re.findall(r"\b\d+\b", sentence or ""):
        if n not in body.split() and words.get(n, "#") not in body.split():
            return False
    return True


def check_review(rv: dict, text: str) -> dict:
    """Checks the review against the report before Yeukai sees it. Adds rv['trace'] (claim, quote, ok) and
    rv['unverified'] (claims we could not trace). Hook and stakes lines that cannot be traced are replaced with
    safe wording, so nothing untraceable goes out in the email or WhatsApp."""
    if not text:
        rv["trace"], rv["unverified"] = [], ["the full report could not be read, so nothing could be checked"]
        rv["hook"], rv["stakes"] = "", ""
        return rv
    items = {f"found {i}": x for i, x in enumerate(rv.get("found") or [], 1)}
    items.update({"hook": rv.get("hook", ""), "stakes": rv.get("stakes", "")})
    trace, ok_items = [], set()
    for t in rv.get("trace_raw") or []:
        item = str(t.get("item", "")).strip().lower()
        claim, quote = items.get(item, ""), str(t.get("quote", ""))
        ok = bool(claim) and _in_report(quote, text) and _numbers_ok(claim, text)
        trace.append({"item": item, "claim": claim, "quote": quote, "ok": ok})
        if ok:
            ok_items.add(item)
    unverified = [f"{k}: {v}" for k, v in items.items() if v and k.startswith("found") and k not in ok_items]
    for key in ("fix_first", "evidence", "underneath"):
        for x in rv.get(key) or []:
            if not _numbers_ok(x, text):
                unverified.append(f"number not in the report: {x}")
    for key in ("hook", "stakes"):
        if rv.get(key) and key not in ok_items:
            unverified.append(f"{key} dropped (could not trace it to the report): {rv[key]}")
            rv[key] = ""
    rv["trace"], rv["unverified"] = trace, unverified
    return rv


def write_review(p: dict, text: str) -> dict:
    user = (
        f"Service: {p['location_name']} ({p.get('service_type', '')}), {p.get('town', '')}\n"
        f"Overall rating: {p['rating']}, report published {p.get('report_date', '')}\n"
        f"Key questions rated below Good: {', '.join(p.get('weak_key_questions') or []) or 'not listed'}\n\n"
        f"REPORT TEXT:\n{text or '(the full report could not be read; only the ratings above are known)'}"
    )
    data = ai._json(ai._ask(REVIEW_SYSTEM, user, max_tokens=5000))
    clean = lambda s: re.sub(r"\s*[–—]\s*", ", ", str(s)).replace(" - ", ", ").strip()
    out = {k: [clean(x) for x in (data.get(k) or [])][:4] for k in ("found", "underneath", "fix_first", "evidence")}
    out["video_finding"] = clean(data.get("video_finding", ""))
    out["subject"] = clean(data.get("subject", "")) or f"A free review of your CQC report, {p['location_name']}"
    out["working"] = clean(data.get("working", ""))
    out["fixes"] = [{"title": clean(f.get("title", "")), "why": clean(f.get("why", "")),
                     "steps": [clean(s) for s in (f.get("steps") or [])][:3], "proof": clean(f.get("proof", ""))}
                    for f in (data.get("fixes") or []) if isinstance(f, dict)][:3]
    out["questions"] = [clean(q) for q in (data.get("questions") or [])][:3]
    out["stakes"] = clean(data.get("stakes", ""))
    out["hook"] = clean(data.get("hook", ""))
    out["context"] = data.get("context") if isinstance(data.get("context"), dict) else {}
    out["trace_raw"] = [t for t in (data.get("trace") or []) if isinstance(t, dict)]
    out["from_report_text"] = bool(text)
    return check_review(out, text)


def token(pid: str) -> str:
    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), f"review:{pid}".encode(), hashlib.sha256).hexdigest()[:16]


def review_url(base: str, pid: str) -> str:
    return f"{base.rstrip('/')}/r/{pid}/{token(pid)}"


def _nohyphen(s: str) -> str:
    return s.replace("Well-led", "Well led")


WHATSAPP_PS = ("P.S. Want quick answers to your CQC questions? Join the Well-Led Network, my free WhatsApp group "
               "for UK care leaders. Managers post their questions and I answer them there: https://shor.by/wln-whatsapp")


# Yeukai's proof lines, worded exactly as he approved them (10 Nov 2026). The bot picks one and never rewrites it.
PROOF_BASE = "I was a CQC Registered Manager for 15 years and led two services to a Good rating"
PROOF_INADEQUATE = PROOF_BASE + ", and took a failing service from local authority Embargo back to Green."
PROOF_SUPPORTED_LIVING = PROOF_BASE + ", and grew a mental health supported living provider from one service to five."
PROOF_HOME_CARE = PROOF_BASE + ", including a home care agency delivering about 3,000 hours of care a week."


def proof_line(p: dict) -> str:
    st = (p.get("service_type") or "").lower()
    if (p.get("rating") or "").lower() == "inadequate":
        return PROOF_INADEQUATE
    if "supported living" in st:
        return PROOF_SUPPORTED_LIVING
    if "home" in st or "domiciliary" in st:
        return PROOF_HOME_CARE
    return PROOF_BASE + "."


def when_published(p: dict) -> str:
    """'yesterday', 'on Friday' or 'on 3 November', from the report date."""
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(p.get("report_date", ""), "%d %B %Y").date()
    except ValueError:
        return "recently"
    days = (datetime.now(timezone.utc).date() - d).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "on " + d.strftime("%A")
    return "on " + d.strftime("%-d %B")


def is_mobile(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    return digits.startswith("07") or digits.startswith("447")


def _first_fix(rv: dict) -> str:
    """The first fix in full: title, the steps to start this week, and how they will know it is working."""
    fixes = rv.get("fixes") or []
    if fixes and fixes[0].get("steps"):
        f = fixes[0]
        lines = [_nohyphen(f["title"])] + [f"{i}. {_nohyphen(x)}" for i, x in enumerate(f["steps"], 1)]
        if f.get("proof"):
            lines.append("You will know it is working when " + _nohyphen(f["proof"][0].lower() + f["proof"][1:]))
        return "\n".join(lines)
    return "\n".join(f"{i}. {_nohyphen(x)}" for i, x in enumerate(rv.get("fix_first") or [], 1))


def email_body(p: dict, rv: dict, review_link: str, video_link: str = "") -> str:
    """Direct response first email (10 Nov 2026): their report and what is at stake, one proof line, a curiosity
    line traced to the report, then real help (the first fix in full), the review link, one easy action."""
    first = (p.get("registered_manager") or "").split(" ")[0]
    hello = f"Dear {first}," if first else f"Dear {p['location_name']} team,"
    opener = f"I read the CQC report for {p['location_name']}, published {p.get('report_date', 'recently')}."
    if rv.get("stakes"):
        opener += " " + _nohyphen(rv["stakes"])
    else:
        opener += " I know how heavy a report like this feels for a manager and team."
    parts = [hello, opener, proof_line(p)]
    if rv.get("hook"):
        parts.append(_nohyphen(rv["hook"]))
    parts.append("So I have written you a short, free review of what I think sits underneath it. It is yours either way, "
                 "and here is the first thing I would do, starting this week:\n\n" + _first_fix(rv))
    generic = os.getenv("GENERIC_VIDEO_URL", "").strip()
    if video_link:
        parts.append(f"I also recorded a one minute video for you: {video_link}")
    elif generic:
        parts.append(f"I recorded a short video on how I read a report like yours: {generic}")
    parts += [
        f"The full review, with the other two fixes, ready to print or share with your team: {review_link}",
        "If one of the three hits home, reply \"call\" and I will give you 20 minutes this week, no charge. "
        f"Or pick a time that suits you: {ENROLMENT_CALL}",
        "If you would rather not hear from me again, just reply 'remove' and I will not contact you.",
        "Kind regards\nYeukai Kajidori\nFounder, Well-Led Network\nCreator of the Well-Led and Safe Supported Living Framework\nhttps://www.welllednetwork.com/blog\nWork with me: https://shor.by/wln-work-with-me\nThe Kajidori Collective Ltd",
        WHATSAPP_PS,
    ]
    return "\n\n".join(parts)


def whatsapp_text(p: dict, rv: dict, review_link: str) -> str:
    """Short WhatsApp first message for a published mobile, same structure as the email (approved 10 Nov 2026)."""
    first = (p.get("registered_manager") or "").split(" ")[0]
    hello = f"Hello {first}," if first else "Hello,"
    opener = f"{hello} {p['location_name']}'s CQC report came out {when_published(p)}."
    if rv.get("stakes"):
        opener += " " + _nohyphen(rv["stakes"])
    parts = [opener, "I'm Yeukai Kajidori. " + proof_line(p)]
    if rv.get("hook"):
        parts.append(_nohyphen(rv["hook"]))
    parts += [f"I've written you a free review that sets it out, with the three things I'd fix first: {review_link}",
              "Have a read. If it's useful, reply \"call\" and I'll give you 20 minutes this week, no charge.",
              "If you'd rather I didn't contact you, just say so."]
    return "\n\n".join(parts)


def trace_note(rv: dict) -> str:
    """For Yeukai only (Telegram): where each claim comes from in the report, and anything that could not be traced."""
    if not rv.get("from_report_text"):
        return "Check: NOT VERIFIED. The full report could not be read, so read it yourself before sending."
    lines = ["Trace (report quotes behind each line):"]
    for t in rv.get("trace") or []:
        if t.get("ok"):
            lines.append(f"• {t['item']}: \"{t['quote']}\"")
    if rv.get("unverified"):
        lines.append("Could not trace (check or remove before sending):")
        lines += [f"• {u}" for u in rv["unverified"]]
    else:
        lines.append("Every finding, the hook and the stakes line were found in the report.")
    return "\n".join(lines)


def page_html(p: dict) -> str:
    """Printable one page review, branded, for the link in the email."""
    rv = p.get("review") or {}
    e = html.escape
    li = lambda xs: "".join(f"<li>{e(_nohyphen(x))}</li>" for x in xs)
    art = INADEQUATE_ARTICLE if p.get("rating") == "Inadequate" else RI_ARTICLE
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>CQC Report Review: {e(p['location_name'])}</title>
<style>
@page{{size:A4;margin:14mm}}
body{{margin:0;background:#EEF3F2;font:15px/1.55 Georgia,'Times New Roman',serif;color:#14262B}}
.sheet{{max-width:760px;margin:24px auto;background:#fff;padding:36px 44px;box-shadow:0 1px 6px #0002}}
.top{{display:flex;justify-content:space-between;gap:16px;border-bottom:2px solid #14262B;padding-bottom:12px;margin-bottom:16px;font-family:Arial,sans-serif}}
.brand{{font-weight:700;color:#0A4C44;letter-spacing:.04em;font-size:13px;text-transform:uppercase}}
.meta{{font-size:12px;color:#5E6F72;text-align:right}}
h1{{font-size:24px;line-height:1.2;margin:0 0 4px}}
.sub{{font-family:Arial,sans-serif;font-size:13px;color:#46595D;margin:0 0 14px}}
h2{{font-family:Arial,sans-serif;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#0E6F63;margin:18px 0 6px}}
ul,ol{{margin:0;padding-left:20px}} li{{margin:0 0 5px}}
.call{{margin-top:20px;background:#0A4C44;color:#fff;border-radius:8px;padding:14px 18px;font-family:Arial,sans-serif;font-size:14px}}
.call a{{color:#fff;font-weight:700}}
.fine{{font-family:Arial,sans-serif;font-size:11px;color:#6E7F82;margin-top:16px}}
@media print{{body{{background:#fff}}.sheet{{box-shadow:none;margin:0;padding:0}}}}
@media (max-width:600px){{.sheet{{padding:22px 18px;margin:0}}.top{{flex-direction:column}}.meta{{text-align:left}}}}
</style></head><body><div class="sheet">
<div class="top"><div class="brand">The Well-Led Network · Free CQC Report Review</div>
<div class="meta">Prepared by Yeukai Kajidori<br>Former CQC Registered Manager, 23 years in care</div></div>
<h1>{e(p['location_name'])}</h1>
<p class="sub">{e(p.get('service_type', ''))}{', ' + e(p.get('town', '')) if p.get('town') else ''} · Overall rating {e(p.get('rating', ''))}, report published {e(p.get('report_date', ''))}</p>
<h2>What the report found</h2><ul>{li(rv.get('found', []))}</ul>
<h2>What I think sits underneath</h2><ul>{li(rv.get('underneath', []))}</ul>
<h2>What I would fix first, in the next 30 days</h2><ol>{li(rv.get('fix_first', []))}</ol>
<h2>How you will know it is working</h2><ul>{li(rv.get('evidence', []))}</ul>
<div class="call">Want to talk it through? Book a free 20 minute call: <a href="{e(ENROLMENT_CALL)}">{e(ENROLMENT_CALL.replace('https://', ''))}</a><br>
Free guide: <a href="{e(art)}">{e(art.replace('https://', '').replace('www.', ''))}</a></div>
<p class="fine">This review is based only on the report CQC has published, read from the outside; the likely causes are a professional view, not a diagnosis.
The Kajidori Collective and The Well-Led Network are independent and not affiliated with the Care Quality Commission. Nothing here guarantees any inspection outcome.</p>
</div></body></html>"""
