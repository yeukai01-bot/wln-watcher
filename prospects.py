"""Report radar: find adult social care services whose new CQC report rates them Inadequate or
Requires improvement, and draft a short, respectful email offering practical help.

Nothing is sent automatically. Drafts go to Yeukai on Telegram with a one-tap link that opens each email,
already addressed and written, in his own mail app. He reads it and presses Send himself.

Data: the CQC public API (free key from the CQC Developer Portal, stored as CQC_API_KEY on Render).
"""
from __future__ import annotations

import os
import time
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urljoin

import requests
from bs4 import BeautifulSoup

import ai
import sources

API = "https://api.service.cqc.org.uk/public/v1"
TARGET_RATINGS = {"Inadequate", "Requires improvement"}
REPORT_MAX_AGE_DAYS = int(os.getenv("RADAR_REPORT_MAX_AGE_DAYS", "30"))
MAX_PROSPECTS = int(os.getenv("RADAR_MAX_PER_DAY", "10"))
PRIORITY_REGIONS = [r.strip().lower() for r in os.getenv("RADAR_PRIORITY_REGIONS", "South East,London").split(",") if r.strip()]
ONLY_PRIORITY = os.getenv("RADAR_ONLY_PRIORITY_REGIONS") == "1"
SOCIAL_CARE_TYPES = {"social care org"}
REPLIES_URL = os.getenv("REPLIES_URL", "https://wln-replies.onrender.com").rstrip("/")
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
WRONG_INBOXES = {"research", "myresearch", "referrals", "referral", "careers", "career", "jobs", "recruitment", "hr",
                 "marketing", "press", "media", "dpo", "privacy", "gdpr", "data", "accounts", "finance", "invoices",
                 "payroll", "training", "webmaster", "support", "sales"}
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "yahoo.com", "yahoo.co.uk",
             "live.co.uk", "icloud.com", "btinternet.com"}
BAD_EMAIL_BITS = ("example.", "sentry", "wixpress", ".png", ".jpg", ".gif", ".webp", "domain.com", "yourname", "noreply", "no-reply")

ARTICLES = {
    "Inadequate": ("Rated Inadequate: What Happens Next, and the Deadlines That Start When the Report Is Published",
                   "https://www.welllednetwork.com/blog/rated-inadequate-what-happens-next"),
    "Requires improvement": ("Your Audits Passed but the Inspector Found the Problem: Why Audits Miss What Matters",
                             "https://www.welllednetwork.com/blog/audits-missed-what-the-inspector-found"),
}


def _headers() -> dict:
    key = os.getenv("CQC_API_KEY", "")
    return {"Ocp-Apim-Subscription-Key": key, "Authorization": f"Bearer {key}", "User-Agent": sources.UA["User-Agent"]}


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{API}/{path.lstrip('/')}", headers=_headers(), params=params or {}, timeout=40)
    r.raise_for_status()
    return r.json()


def changed_location_ids(days: int = 2) -> list[str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    ids, page = [], 1
    while True:
        data = _get("changes/location", {
            "startTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTimestamp": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "page": page, "perPage": 1000,
        })
        ids += data.get("changes", [])
        if page >= int(data.get("totalPages", 1) or 1):
            break
        page += 1
    return list(dict.fromkeys(ids))


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def qualify(loc: dict, stats: dict | None = None) -> dict | None:
    """Return a prospect summary if this location has a fresh Inadequate / Requires improvement report."""
    stats = stats if stats is not None else {}

    def no(reason: str):
        stats[reason] = stats.get(reason, 0) + 1
        return None

    if (loc.get("registrationStatus") or "").lower() != "registered":
        return no("not registered")
    if (loc.get("type") or "").lower() not in SOCIAL_CARE_TYPES:
        return no("not adult social care")
    overall = ((loc.get("currentRatings") or {}).get("overall") or {})
    rating = overall.get("rating")
    if rating not in TARGET_RATINGS:
        return no("rated Good/Outstanding or not rated")
    published = _parse_date((loc.get("lastReport") or {}).get("publicationDate"))
    report_date = published or _parse_date(overall.get("reportDate"))
    if not report_date or report_date < datetime.now(timezone.utc) - timedelta(days=REPORT_MAX_AGE_DAYS):
        samples = stats.setdefault("_samples", [])
        if len(samples) < 3:
            samples.append(f"{loc.get('name')}: {rating}, rating report {overall.get('reportDate')}, "
                           f"last report published {(loc.get('lastReport') or {}).get('publicationDate')}, "
                           f"last inspection {(loc.get('lastInspection') or {}).get('date')}")
        return no("report older than %d days" % REPORT_MAX_AGE_DAYS)
    region = loc.get("region") or ""
    if ONLY_PRIORITY and region.lower() not in PRIORITY_REGIONS:
        return no("outside priority regions")
    kq = [
        f"{k.get('name')}: {k.get('rating')}"
        for k in (overall.get("keyQuestionRatings") or [])
        if k.get("rating") in TARGET_RATINGS
    ]
    services = ", ".join(s.get("name", "") for s in (loc.get("gacServiceTypes") or []) if s.get("name"))
    return {
        "location_id": loc.get("locationId"),
        "location_name": loc.get("name"),
        "provider_id": loc.get("providerId"),
        "rating": rating,
        "report_date": report_date.strftime("%d %B %Y"),
        "weak_key_questions": kq,
        "service_type": services,
        "region": region,
        "local_authority": loc.get("localAuthority") or "",
        "town": loc.get("postalAddressTownCity") or "",
        "phone": loc.get("mainPhoneNumber") or "",
        "website": loc.get("website") or "",
        "cqc_page": f"https://www.cqc.org.uk/location/{loc.get('locationId')}",
        "registered_manager": next(
            (f"{c.get('personGivenName', '')} {c.get('personFamilyName', '')}".strip()
             for r in (loc.get("regulatedActivities") or []) for c in (r.get("contacts") or [])
             if "registered manager" in " ".join(c.get("personRoles") or []).lower()), ""),
    }


SITE = "https://www.cqc.org.uk"
SEARCH_PLACES = [x.strip() for x in os.getenv("RADAR_SEARCH_PLACES", (
    "London,Reading,Oxford,Southampton,Brighton,Guildford,Maidstone,Milton Keynes,Luton,Chelmsford,Ipswich,Norwich,"
    "Cambridge,Peterborough,Northampton,Birmingham,Coventry,Leicester,Nottingham,Derby,Stoke-on-Trent,Lincoln,"
    "Bristol,Gloucester,Swindon,Bournemouth,Exeter,Plymouth,Truro,Manchester,Liverpool,Preston,Leeds,Sheffield,"
    "Hull,York,Middlesbrough,Newcastle upon Tyne,Carlisle,Shrewsbury,Worcester,Hereford")).split(",") if x.strip()]
SITE_RATING_RX = re.compile(r"Overall\s*:\s*(Inadequate|Requires improvement)")


def _site(path: str, params: list | None = None) -> str:
    r = requests.get(SITE + path, params=params, headers=sources.BROWSER_UA, timeout=40)
    r.raise_for_status()
    return r.text


def site_recent(period: str = "week") -> tuple[dict, dict]:
    """Services rated Inadequate or Requires improvement whose report the CQC website lists as published in the
    last week (or month). The public API does not yet carry the new-style assessment ratings, the website does."""
    found, stats = {}, {}
    for place in SEARCH_PLACES:
        for page in range(1, 11):
            params = [("query", ""), ("location-query", place), ("radius", "25"), ("display", "list"), ("sort", "distance"),
                      ("last-published", period), ("filters[]", "archived:active"), ("filters[]", "lastPublished:all"),
                      ("filters[]", "more_services:all"), ("filters[]", "overallRating:Requires improvement"),
                      ("filters[]", "overallRating:Inadequate"), ("filters[]", "services:all"), ("filters[]", "specialisms:all")]
            if page > 1:
                params += [("ajax", "0"), ("page", str(page))]
            try:
                html = _site("/search/all?" + urlencode(params, quote_via=quote))
            except Exception as exc:
                stats[f"website search failed ({type(exc).__name__})"] = stats.get(f"website search failed ({type(exc).__name__})", 0) + 1
                break
            # Split the page at each result title; each chunk holds that result's name, type and overall rating.
            parts = re.split(r'<h2[^>]*class="[^"]*service-header__title[^"]*"[^>]*>', html)
            links = parts[1:]
            if page == 1 and not links and not stats.get("_seen_empty"):
                stats["_seen_empty"] = 1
                soup = BeautifulSoup(html, "html.parser")
                title = (soup.title.get_text(strip=True) if soup.title else "no title")[:80]
                found_txt = (re.search(r"We found[^.<]{0,80}|Sorry but there were no results", soup.get_text(" ")) or [""])[0]
                stats[f"empty search page for {place}: '{title}' {found_txt}"] = 1
            for chunk in links:
                m = re.search(r'href="\s*/location/(1-\d+)\s*"[^>]*>([^<]+)</a>', chunk)
                if not m:
                    continue
                text = " ".join(re.sub(r"<[^>]+>", " ", chunk).split())
                r = SITE_RATING_RX.search(text)
                if r:
                    found.setdefault(m.group(1), {"name": " ".join(m.group(2).split()), "rating": r.group(1), "type": ""})
                else:
                    stats["search result without a readable rating"] = stats.get("search result without a readable rating", 0) + 1
            if len(links) < 10:
                break
    return found, stats


def site_detail(lid: str) -> dict:
    """Report publication date and the key questions rated below Good, from the service's CQC page."""
    try:
        html = _site(f"/location/{lid}")
    except Exception:
        return {}
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    out = {"weak": []}
    m = re.search(r"Report published\s*:\s*(\d{1,2} [A-Z][a-z]+ \d{4})", text)
    if m:
        out["published"] = m.group(1)
    seg = text[m.start():m.start() + 600] if m else text
    for kq, rating in re.findall(r"\b(Safe|Effective|Caring|Responsive|Well-led)\s*:?\s*(Inadequate|Requires improvement|Good|Outstanding)", seg):
        item = f"{kq}: {rating}"
        if rating in TARGET_RATINGS and item not in out["weak"]:
            out["weak"].append(item)
    return out


CLEAROUT = "https://api.clearout.io/v2"
CLEAROUT_LAST_ERROR = [""]


def _clearout(path: str, body: dict) -> dict:
    key = os.getenv("CLEAROUT_API_KEY", "")
    if not key:
        return {}
    r = requests.post(f"{CLEAROUT}/{path}", json=body, timeout=70,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    if r.status_code >= 400:
        CLEAROUT_LAST_ERROR[0] = f"{path} HTTP {r.status_code}: {r.text[:150]}"
        print("clearout error:", CLEAROUT_LAST_ERROR[0])
        return {}
    out = r.json() or {}
    print("clearout", path, "status", out.get("status"), "found" if (out.get("data") or {}).get("emails") else "")
    return out


def clearout_find(name: str, domain: str) -> str:
    """Clearout email finder (name + domain). Free when nothing is found; 4 credits per person found."""
    if not (name and domain and os.getenv("CLEAROUT_API_KEY")):
        return ""
    try:
        d = (_clearout("email_finder/instant", {"name": name, "domain": domain, "timeout": 60000, "queue": False}).get("data") or {})
        emails = [e.get("email_address", "") for e in (d.get("emails") or []) if e.get("email_address")]
        return emails[0].lower() if emails and int(d.get("confidence_score") or 0) >= 50 else ""
    except Exception:
        return ""
    finally:
        time.sleep(11)  # API limit is 6 finder requests a minute


def clearout_verify(email: str) -> str:
    """Returns 'safe', 'risky' (valid but unconfirmable or a shared inbox), 'invalid' or 'unchecked'."""
    if not (email and os.getenv("CLEAROUT_API_KEY")):
        return "unchecked"
    try:
        d = (_clearout("email_verify/instant", {"email": email, "timeout": 60000}).get("data") or {})
        status, safe = (d.get("status") or "").lower(), (d.get("safe_to_send") or "").lower()
        if status == "invalid":
            return "invalid"
        if safe == "yes":
            return "safe"
        if status in ("valid", "catch_all", "unknown"):
            return "risky"
        return "unchecked"
    except Exception:
        return "unchecked"
    finally:
        time.sleep(7)  # API limit is 10 verifications a minute


def _domain(website: str) -> str:
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", (website or "").strip().lower()).split("/")[0])


def find_email(website: str) -> str:
    """Look for a published contact email on the provider's own website (home and contact pages)."""
    if not website:
        return ""
    base = website if website.startswith("http") else "https://" + website
    found: list[str] = []
    for path in ("", "/contact", "/contact-us", "/contact-us/", "/contact/"):
        try:
            r = requests.get(urljoin(base.rstrip("/") + "/", path.lstrip("/")), headers=sources.BROWSER_UA, timeout=20)
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select('a[href^="mailto:"]'):
                found.append(a["href"][7:].split("?")[0])
            found += EMAIL_RX.findall(soup.get_text(" "))
        except Exception:
            continue
        if found:
            break
    clean = [e.strip().strip(".").lower() for e in found if not any(b in e.lower() for b in BAD_EMAIL_BITS)]
    # Only addresses that belong to this service: same domain as its website, or a free mailbox.
    site_domain = re.sub(r"^www\.", "", re.sub(r"^https?://", "", base).split("/")[0].lower())
    root = ".".join(site_domain.split(".")[-3:]) if site_domain.endswith(".uk") else ".".join(site_domain.split(".")[-2:])
    clean = [e for e in clean
             if e.split("@")[0] not in WRONG_INBOXES
             and (e.split("@")[1].endswith(root) or e.split("@")[1] in FREE_MAIL)]
    if not clean:
        return ""
    preferred = [e for e in clean if e.split("@")[0] in ("info", "enquiries", "enquiry", "admin", "office", "hello", "contact", "manager")]
    return (preferred or clean)[0]


EMAIL_SYSTEM = """You write one short, respectful first-contact email from Yeukai Kajidori to a UK adult social care
provider that has just received a CQC report rated Inadequate or Requires improvement.

About Yeukai: 23 years in UK health and social care, 15 as a CQC Registered Manager, led two services to Good,
runs The Well-Led Network (a free community for care leaders at welllednetwork.com).

Rules:
- British English, warm, calm, plain. No hype, no pressure, no emojis, no dashes used as punctuation.
- Under 170 words in the body. One clear, low-pressure offer: a free 30-minute call to talk through the report
  and the first 30 days, plus the free article and tool linked below.
- Only use facts given to you (service name, rating, report date, key questions rated below Good). Do not
  criticise the service, CQC or anyone else. Do not say or imply CQC endorses Yeukai. Do not promise any outcome.
- Acknowledge gently that a report like this is hard for the manager and team. Refer to the report by its date. Only say
  "this week" or "last week" if the number of days since publication given to you makes that literally true.
- Only use the facts about Yeukai listed above. Do not add any other claims (for example, do not say he has helped
  other managers through reports like theirs, and do not invent results).
- Address the registered manager by first name if one is given, otherwise "Dear [Service name] team".
- Include this final line exactly: "If you would rather not hear from me again, just reply 'remove' and I will not contact you."
- Sign off: Kind regards, Yeukai Kajidori, The Well-Led Network, welllednetwork.com, kajidoricollective@gmail.com

Reply in exactly this format:
SUBJECT: <subject line, under 70 characters, no rating in the subject>
BODY:
<email body>"""


def _days_since(date_text: str) -> str:
    try:
        d = datetime.strptime(date_text, "%d %B %Y").replace(tzinfo=timezone.utc)
        return str((datetime.now(timezone.utc) - d).days)
    except (TypeError, ValueError):
        return "unknown"


def draft_email(p: dict) -> tuple[str, str]:
    title, url = ARTICLES.get(p["rating"], ARTICLES["Inadequate"])
    user = (
        f"Service: {p['location_name']} ({p['service_type']}), {p['town']}\n"
        f"Registered manager: {p['registered_manager'] or 'not listed'}\n"
        f"Overall rating: {p['rating']}, report published {p['report_date']}\n"
        f"Today's date: {datetime.now(timezone.utc):%d %B %Y}. Days since the report was published: {_days_since(p['report_date'])}\n"
        f"Key questions rated below Good: {', '.join(p['weak_key_questions']) or 'not listed'}\n"
        f"Free article to link: {title} {url}\n"
        f"Free call link: https://tfft.io/CRIkyvF"
    )
    reply = ai._ask(EMAIL_SYSTEM, user, max_tokens=1200)
    reply = re.sub(r"\s*[\u2013\u2014]\s*", ", ", reply).replace(" - ", ", ")  # no dashes as punctuation
    subj = re.search(r"SUBJECT:\s*(.+)", reply)
    body = reply.split("BODY:", 1)[-1].strip()
    return (subj.group(1).strip() if subj else f"Support after your recent CQC report, {p['location_name']}"), body


def run(store, telegram) -> None:
    if not os.getenv("CQC_API_KEY"):
        telegram.send_message("Report radar is not running yet: add your free CQC API key to Render as CQC_API_KEY.")
        return
    first = not store.has("radar:initialised:site4")
    period = "month" if first else "week"
    candidates, stats = site_recent(period)
    found = []
    todo = []
    for lid, info in candidates.items():
        if store.has(f"prospect:{lid}"):
            stats["already sent to you"] = stats.get("already sent to you", 0) + 1
        else:
            todo.append((lid, info))

    def build(item):
        lid, info = item
        try:
            loc = _get(f"locations/{lid}")
        except Exception as exc:
            return None, f"could not read contact details ({type(exc).__name__})"
        if (loc.get("type") or "").lower() not in SOCIAL_CARE_TYPES:
            return None, "not adult social care"
        if (loc.get("registrationStatus") or "").lower() != "registered":
            return None, "not registered"
        region = loc.get("region") or ""
        if ONLY_PRIORITY and region.lower() not in PRIORITY_REGIONS:
            return None, "outside priority regions"
        detail = site_detail(lid)
        services = ", ".join(s.get("name", "") for s in (loc.get("gacServiceTypes") or []) if s.get("name")) or info.get("type", "")
        return {
            "location_id": lid, "location_name": loc.get("name") or info["name"], "provider_id": loc.get("providerId"),
            "rating": info["rating"], "report_date": detail.get("published") or "recently",
            "weak_key_questions": detail.get("weak", []), "service_type": services, "region": region,
            "local_authority": loc.get("localAuthority") or "", "town": loc.get("postalAddressTownCity") or "",
            "phone": loc.get("mainPhoneNumber") or "", "website": loc.get("website") or "",
            "cqc_page": f"https://www.cqc.org.uk/location/{lid}",
            "registered_manager": next(
                (f"{c.get('personGivenName', '')} {c.get('personFamilyName', '')}".strip()
                 for r in (loc.get("regulatedActivities") or []) for c in (r.get("contacts") or [])
                 if "registered manager" in " ".join(c.get("personRoles") or []).lower()), ""),
        }, None

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        for p, why in pool.map(build, todo):
            if why:
                stats[why] = stats.get(why, 0) + 1
            elif p:
                found.append(p)
    store.add_many(["radar:initialised:site4"])
    stats.pop("_seen_empty", None)
    breakdown = "\n".join(f"- {k}: {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1]))
    # Priority regions first, then Inadequate before Requires improvement.
    found.sort(key=lambda p: (p["region"].lower() not in PRIORITY_REGIONS, p["rating"] != "Inadequate"))
    found = found[:MAX_PROSPECTS]

    if not found:
        telegram.send_message(f"Report radar: no new Inadequate or Requires improvement adult social care reports "
                              f"published in the last {period} ({len(candidates)} poor reports of any kind found on the CQC website)."
                              + ("\n\nWhy they were ruled out:\n" + breakdown if breakdown else ""))
        return

    suppressed = set(store.suppressed())
    made = []
    co_stats = {"tried": 0, "found": 0, "no_name_or_site": 0}
    for n, p in enumerate(found, 1):
        try:
            prov = _get(f"providers/{p['provider_id']}")
        except Exception:
            prov = {}
        p["provider_name"] = prov.get("name", "")
        if "council" in p["provider_name"].lower() or len(prov.get("locationIds") or []) > 15:
            p["note"] = "Large organisation or council: lower priority, has its own quality team."
            p["large_org"] = True
        p["companies_house"] = prov.get("companiesHouseNumber", "")
        site = p["website"] or prov.get("website", "")
        p["email"] = find_email(site)
        p["email_source"] = "service website" if p["email"] else ""
        # No website on the CQC register is common for small homes: Clearout can resolve a company
        # name to its domain when its Email Finder "Relax" domain setting is on.
        lookup = _domain(site) or p["provider_name"] or ""
        if not p["email"] and p.get("registered_manager") and lookup:
            co_stats["tried"] += 1
            p["email"] = clearout_find(p["registered_manager"], lookup)
            p["email_source"] = "Clearout finder" if p["email"] else ""
            co_stats["found"] += bool(p["email"])
        elif not p["email"]:
            co_stats["no_name_or_site"] += 1
        p["email_check"] = clearout_verify(p["email"]) if p["email"] else ""
        if p["email_check"] == "invalid":
            p["note"] = (p.get("note", "") + f" Email {p['email']} failed the Clearout check and was dropped.").strip()
            p["email"], p["email_source"] = "", ""
        if p["email"] and p["email"] in suppressed:
            p["email"] = ""
            p["note"] = "Previously asked not to be contacted."
        # UK PECR: unsolicited marketing email is fine to companies (corporate subscribers) but not to
        # sole traders or partnerships without consent. No company number: call or write instead.
        p["can_email"] = bool(p["email"] and p["companies_house"])
        p["subject"], p["body"] = draft_email(p)
        p["status"] = "drafted"
        p["radar_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        p["code"] = f"P{n}"
        made.append(store.put_prospect(p))
        store.add_many([f"prospect:{p['location_id']}"])

    store.save_latest_prospects([m["id"] for m in made])
    day = made[0]["radar_date"]
    csv_link = ""
    if os.getenv("APPROVALS_KEY"):
        import hashlib
        import hmac

        tok = hmac.new(os.getenv("APPROVALS_KEY").encode(), f"radar:{day}".encode(), hashlib.sha256).hexdigest()[:24]
        csv_link = f"{REPLIES_URL}/radar/{day}/{tok}.csv"
    telegram.send_message(
        f"Report radar, {datetime.now().strftime('%d %B')}: {len(made)} services with a new Inadequate or Requires improvement report.\n\n"
        "Each draft follows below. Tap 'Open email' to open it in your mail app, already addressed and written; read it and press Send. "
        "Then reply e.g. 'sent P1 P3' so I keep track. "
        "Services without a company number or published email are marked 'call or write', because the law on "
        "unsolicited emails is stricter for sole traders and partnerships."
        + (f"\n\nEmail finder (Clearout): searched {co_stats['tried']}, found {co_stats['found']}; "
           f"{co_stats['no_name_or_site']} had no registered manager name to search with."
           + ("" if os.getenv("CLEAROUT_API_KEY") else " CLEAROUT_API_KEY is not set.") + (f" Last error: {CLEAROUT_LAST_ERROR[0]}" if CLEAROUT_LAST_ERROR[0] else ""))
        + (f"\n\nsam.ai import file for today (Leads, folder CQC Report Radar): {csv_link}" if csv_link else "")
        + ("\n\nPrinted letters: reply 'letters' to read today's letters, then 'post all' or 'post L1 L3' to have them printed and posted."
           if os.getenv("INTELLIPRINT_API_KEY") else "")
    )
    for p in made:
        check = {"safe": "checked, safe to send", "risky": "checked, valid but cannot be fully confirmed", "unchecked": "not checked"}.get(p.get("email_check", ""), "")
        how = f"Email to: {p['email']} ({p.get('email_source', '')}{', ' + check if check else ''})" if p["can_email"] else (
            "Call or write (no company number, email not allowed without consent)" if p["email"] else "Call or write (no email published)")
        telegram.send_message(
            f"{p['code']}. {p['location_name']} ({p['town']}, {p['region']})\n"
            f"{p['rating']}, report {p['report_date']}. {', '.join(p['weak_key_questions'])}\n"
            f"Provider: {p['provider_name']}  Phone: {p['phone'] or 'n/a'}\n"
            f"{how}\nCQC page: {p['cqc_page']}\n"
            + (f"Note: {p['note']}\n" if p.get("note") else "")
            + (f"Open email: {REPLIES_URL}/m/{p['id']}\n" if p["can_email"] else "")
            + "\n"
            f"Subject: {p['subject']}\n\n{p['body']}"
        )
