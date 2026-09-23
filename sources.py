"""Collect new items from primary sources that matter to UK adult social care leaders.

Every source is isolated: if one fails, the rest still run and the failure is reported.
Only official or primary sources are watched, so drafts start from facts, not commentary.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "WellLedNetworkWatcher/1.0 (+https://www.welllednetwork.com)"}
# Some official sites refuse unknown bots when reading a single article, so article reads use a normal browser identity.
BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 30


@dataclass
class Item:
    source: str
    title: str
    url: str
    published: datetime | None = None
    summary: str = ""
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        return self.url.split("#")[0].rstrip("/")


# GOV.UK publishes Atom feeds for any search. These cover CQC, DHSC, the Home Office
# (care worker visas), employment law and the adult social care keyword search.
GOVUK_FEEDS = {
    "GOV.UK: Care Quality Commission": "https://www.gov.uk/search/all.atom?organisations%5B%5D=care-quality-commission&order=updated-newest",
    "GOV.UK: Department of Health and Social Care": "https://www.gov.uk/search/all.atom?organisations%5B%5D=department-of-health-and-social-care&order=updated-newest",
    "GOV.UK: adult social care": "https://www.gov.uk/search/all.atom?keywords=%22adult+social+care%22&order=updated-newest",
    "GOV.UK: care worker visas and sponsorship": "https://www.gov.uk/search/all.atom?keywords=care+worker+sponsor&order=updated-newest",
    "GOV.UK: immigration rules changes": "https://www.gov.uk/search/all.atom?keywords=%22statement+of+changes+to+the+immigration+rules%22&order=updated-newest",
    "GOV.UK: Employment Rights Act": "https://www.gov.uk/search/all.atom?keywords=%22Employment+Rights+Act%22&order=updated-newest",
    "GOV.UK: Liberty Protection Safeguards and DoLS": "https://www.gov.uk/search/all.atom?keywords=deprivation+of+liberty&order=updated-newest",
    "GOV.UK: Oliver McGowan training": "https://www.gov.uk/search/all.atom?keywords=%22Oliver+McGowan%22&order=updated-newest",
}

# New statutory instruments. Filtered by keyword below so only care-relevant law comes through.
LEGISLATION_FEED = "https://www.legislation.gov.uk/new/data.feed"
LEGISLATION_KEYWORDS = re.compile(
    r"social care|care quality|health and social care|regulated activit|care act|mental capacity|"
    r"deprivation of liberty|mental health|employment rights|immigration|statutory sick|"
    r"national minimum wage|disclosure and barring|safeguarding",
    re.I,
)

# Pages without a feed: read the listing and pick up links that look like articles.
HTML_LISTINGS = {
    "CQC news": ("https://www.cqc.org.uk/news", r"^/news/[a-z0-9-]+$"),
    "Skills for Care news": ("https://www.skillsforcare.org.uk/news-and-events/News.aspx", r"(?i)^/news-and-events/news/[a-z0-9-]+$"),
    "Acas Employment Rights Act": ("https://www.acas.org.uk/employment-rights-act-2025", r"^/employment-rights-act-2025/[a-z0-9-/]+$"),
}


def _dt(entry) -> datetime | None:
    for attr in ("updated_parsed", "published_parsed"):
        t = entry.get(attr) if hasattr(entry, "get") else getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(name: str, url: str) -> list[Item]:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        items.append(
            Item(
                source=name,
                title=(e.get("title") or "").strip(),
                url=e.get("link") or "",
                published=_dt(e),
                summary=BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)[:600],
            )
        )
    return items


def fetch_legislation() -> list[Item]:
    items = fetch_feed("legislation.gov.uk: new legislation", LEGISLATION_FEED)
    return [i for i in items if LEGISLATION_KEYWORDS.search(i.title + " " + i.summary)]


def fetch_listing(name: str, url: str, pattern: str) -> list[Item]:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rx = re.compile(pattern)
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]
        path = re.sub(r"^https?://[^/]+", "", href)
        text = a.get_text(" ", strip=True)
        if not rx.search(path) or len(text) < 12 or path in seen:
            continue
        seen.add(path)
        items.append(Item(source=name, title=text[:200], url=urljoin(url, path), published=None))
    return items[:25]


def fetch_questions(csv_url: str) -> list[Item]:
    """Optional: questions Yeukai or Claude log from Facebook and LinkedIn groups.

    A Google Sheet published as CSV with columns: date, group, question, link.
    The watcher never logs into Facebook or LinkedIn itself.
    """
    r = requests.get(csv_url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    rows = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))
    items = []
    for row in rows:
        q = (row.get("question") or "").strip()
        if not q:
            continue
        when = None
        try:
            when = datetime.fromisoformat((row.get("date") or "").strip()).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        items.append(
            Item(
                source=f"Group question: {row.get('group', '').strip() or 'care group'}",
                title=q[:300],
                url=(row.get("link") or "").strip() or f"question:{hash(q)}",
                published=when,
            )
        )
    return items


def collect(lookback_hours: int, questions_csv: str | None = None) -> tuple[list[Item], list[str]]:
    """Return fresh items plus a list of source errors (for the morning report)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items: list[Item] = []
    errors: list[str] = []

    jobs = [(n, lambda n=n, u=u: fetch_feed(n, u)) for n, u in GOVUK_FEEDS.items()]
    jobs.append(("legislation.gov.uk", fetch_legislation))
    jobs += [(n, lambda n=n, u=u, p=p: fetch_listing(n, u, p)) for n, (u, p) in HTML_LISTINGS.items()]
    if questions_csv:
        jobs.append(("Group questions sheet", lambda: fetch_questions(questions_csv)))

    for name, job in jobs:
        try:
            got = job()
        except Exception as exc:  # one broken source must never stop the run
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        for it in got:
            # Listing pages have no dates; keep them and let the "already seen" check filter repeats.
            if it.published is None or it.published >= cutoff:
                items.append(it)

    # De-duplicate by URL, keeping the first (most specific) source.
    uniq: dict[str, Item] = {}
    for it in items:
        uniq.setdefault(it.key(), it)
    return list(uniq.values()), errors


def page_text(url: str, limit: int = 18000) -> str:
    """Plain text of a source page, for research. Returns '' on failure."""
    if not url.startswith("http"):
        return ""
    try:
        r = requests.get(url, headers=BROWSER_UA, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form", "aside"]):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    return text[:limit]
