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

Reply with JSON only:
{
 "video_finding": "one short finding in plain words, under 12 words, e.g. 'medicines records were not always accurate'",
 "found": ["3 or 4 key findings, each starting with the key question in brackets, e.g. '(Safe) ...'"],
 "underneath": ["2 or 3 likely root causes connecting the findings"],
 "fix_first": ["3 actions for the next 30 days, in the order you would do them"],
 "evidence": ["2 or 3 things that would show the change is working"],
 "subject": "email subject under 60 characters, no rating in it, e.g. 'A free review of your CQC report, Oak House'"
}"""


def write_review(p: dict, text: str) -> dict:
    user = (
        f"Service: {p['location_name']} ({p.get('service_type', '')}), {p.get('town', '')}\n"
        f"Overall rating: {p['rating']}, report published {p.get('report_date', '')}\n"
        f"Key questions rated below Good: {', '.join(p.get('weak_key_questions') or []) or 'not listed'}\n\n"
        f"REPORT TEXT:\n{text or '(the full report could not be read; only the ratings above are known)'}"
    )
    data = ai._json(ai._ask(REVIEW_SYSTEM, user, max_tokens=2500))
    clean = lambda s: re.sub(r"\s*[–—]\s*", ", ", str(s)).replace(" - ", ", ").strip()
    out = {k: [clean(x) for x in (data.get(k) or [])][:4] for k in ("found", "underneath", "fix_first", "evidence")}
    out["video_finding"] = clean(data.get("video_finding", ""))
    out["subject"] = clean(data.get("subject", "")) or f"A free review of your CQC report, {p['location_name']}"
    out["from_report_text"] = bool(text)
    return out


def token(pid: str) -> str:
    return hmac.new(os.getenv("APPROVALS_KEY", "").encode(), f"review:{pid}".encode(), hashlib.sha256).hexdigest()[:16]


def review_url(base: str, pid: str) -> str:
    return f"{base.rstrip('/')}/r/{pid}/{token(pid)}"


def _nohyphen(s: str) -> str:
    return s.replace("Well-led", "Well led")


WHATSAPP_PS = ("P.S. Want quick answers to your CQC questions? Join the Well-Led Network, my free WhatsApp group "
               "for UK care leaders. Managers post their questions and I answer them there: https://shor.by/wln-whatsapp")


def email_body(p: dict, rv: dict, review_link: str, video_link: str = "") -> str:
    first = (p.get("registered_manager") or "").split(" ")[0]
    hello = f"Dear {first}," if first else f"Dear {p['location_name']} team,"
    b = lambda xs: "\n".join(f"• {_nohyphen(x)}" for x in xs)
    n = lambda xs: "\n".join(f"{i}. {_nohyphen(x)}" for i, x in enumerate(xs, 1))
    parts = [
        hello,
        f"I read the CQC report for {p['location_name']}, published {p.get('report_date', 'recently')}. I know how heavy "
        "a report like this feels for a manager and team, so I have written you a short, free review of what I think "
        "sits underneath it. It is yours either way.",
    ]
    generic = os.getenv("GENERIC_VIDEO_URL", "").strip()
    if video_link:
        parts.append(f"I also recorded a one minute video for you: {video_link}")
    elif generic:
        parts.append(f"I recorded a short video on how I read a report like yours: {generic}")
    parts += [
        "What the report found\n" + b(rv["found"]),
        "What I think sits underneath\n" + b(rv["underneath"]),
        "What I would fix first, in the next 30 days\n" + n(rv["fix_first"]),
        "How you will know it is working\n" + b(rv["evidence"]),
        f"The full review, ready to print or share with your team: {review_link}",
        f"If it would help to talk it through, I keep a few free 20 minute calls each week: {ENROLMENT_CALL}",
        "A little about me: I have spent 23 years in UK health and social care, 15 of them as a CQC Registered "
        "Manager, and I have led two services to a Good rating.",
        "If you would rather not hear from me again, just reply 'remove' and I will not contact you.",
        "Kind regards\nYeukai Kajidori\nThe Kajidori Collective\nhttps://www.welllednetwork.com/blog\nkajidoricollective@gmail.com",
        WHATSAPP_PS,
    ]
    return "\n\n".join(parts)


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
