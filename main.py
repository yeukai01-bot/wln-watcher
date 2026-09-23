"""Morning run: watch primary sources, choose topics, draft the best ones, send to Yeukai.

Nothing is ever published by this job. Drafts go to Telegram; Yeukai approves by replying with numbers
(handled by web.py), and a scheduled Claude task publishes only what he approved.
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import ai, sources, telegram_io as telegram
from store import Store

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "48"))
MAX_TOPICS = int(os.getenv("MAX_TOPICS", "5"))
MAX_DRAFTS = int(os.getenv("MAX_DRAFTS", "1"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "12"))  # urgency x client_pull, out of 25
DRY_RUN = os.getenv("DRY_RUN") == "1"
UK = ZoneInfo("Europe/London")


def existing_titles() -> list[str]:
    p = Path(__file__).parent / "existing_articles.txt"
    titles = [l.strip() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]
    try:  # articles published through the approvals queue count as covered too
        titles += [a["headline"] for a in Store().approvals("published") if a.get("headline")]
    except Exception:
        pass
    return list(dict.fromkeys(titles))


def slugify(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:60]


def run() -> int:
    now = datetime.now(UK)
    seen = Store()
    items, errors = sources.collect(LOOKBACK_HOURS, os.getenv("QUESTIONS_CSV_URL") or None)
    if seen.is_empty():
        # First ever run: listing pages have no dates, so treat what is already there as old news.
        seen.add_many([i.key() for i in items if i.published is None])
    fresh = [i for i in items if not seen.has(i.key())]
    print(f"collected={len(items)} fresh={len(fresh)} errors={len(errors)}")

    header = f"Well-Led Network morning topics, {now:%A %d %B %Y}\n\n"
    if not fresh:
        telegram.send_message(header + "No new developments from the watched sources since the last run."
                              + (f"\n\nSources that failed: {len(errors)}\n" + "\n".join(errors) if errors else ""))
        return 0

    by_id = {f"i{n}": it for n, it in enumerate(fresh)}
    payload = [
        {"id": k, "source": v.source, "title": v.title, "summary": v.summary,
         "date": v.published.astimezone(UK).strftime("%d %b %Y") if v.published else ""}
        for k, v in by_id.items()
    ]
    picks = []
    for start in range(0, len(payload), 60):  # keep each triage call a sensible size
        picks += ai.triage(payload[start:start + 60], existing_titles())

    for p in picks:
        p["score"] = int(p.get("urgency", 0)) * int(p.get("client_pull", 0))
    set_aside = [p for p in picks if p.get("controversy") == "high"]
    covered = [p for p in picks if p.get("already_covered") and p.get("controversy") != "high"]
    ranked, heads = [], set()
    for p in sorted(picks, key=lambda p: p["score"], reverse=True):
        h = slugify(p.get("headline", ""))
        if p.get("controversy") == "high" or p.get("already_covered") or p["score"] < MIN_SCORE or h in heads:
            continue  # same story from two sources is shown once
        heads.add(h)
        ranked.append(p)
    ranked = ranked[:MAX_TOPICS]

    lines = [header.strip(), ""]
    if ranked:
        lines.append("Recommended topics (best first)")
        for n, p in enumerate(ranked, 1):
            it = by_id[p["id"]]
            lines += [
                f"\n{n}. {p['headline']}",
                f"Why now: {p.get('angle','')}",
                f"Leads to: {p.get('product','none')}  |  Score {p['score']}/25" + (f"  |  Key date: {p['key_date']}" if p.get("key_date") else ""),
                f"Free tool: {p.get('lead_magnet','')}",
                f"Source: {it.source}: {it.title}\n{it.url}",
            ]
    else:
        lines.append("Nothing new cleared the bar for an article today.")
    if covered:
        lines.append("\nAlready covered (consider updating the existing article):")
        lines += [f"- {p['already_covered']}  ({by_id[p['id']].title})" for p in covered]
    if set_aside:
        lines.append("\nSet aside for you (contested or political, not drafted):")
        lines += [f"- {by_id[p['id']].title}: {p.get('controversy_note','')}" for p in set_aside]
    if errors:
        lines.append(f"\nSources that failed this morning ({len(errors)}):")
        lines += [f"- {e}" for e in errors]
    lines.append("\nTo approve, reply to this chat with the numbers, for example: 1 3\n"
                 "Reply 'queue' to see what is waiting, or 'cancel 2' to withdraw one. "
                 "Nothing is published unless you reply with its number.")
    telegram.send_message("\n".join(lines))

    latest = {"date": now.strftime("%Y-%m-%d"), "topics": []}
    for n, p in enumerate(ranked, 1):
        it = by_id[p["id"]]
        latest["topics"].append({
            "number": n, "headline": p["headline"], "angle": p.get("angle", ""),
            "product": p.get("product", ""), "lead_magnet": p.get("lead_magnet", ""),
            "key_date": p.get("key_date", ""), "source": it.source, "source_title": it.title,
            "url": it.url, "draft": None,
        })
    if not DRY_RUN:
        seen.save_latest(latest)

    for p in ranked[:MAX_DRAFTS]:
        it = by_id[p["id"]]
        text = sources.page_text(it.url)
        if len(text) < 500:
            telegram.send_message(f"Could not read enough of the source to draft safely: {it.url}")
            continue
        if DRY_RUN:
            print("DRY_RUN: would draft", p["headline"])
            continue
        md = ai.draft(p, it.title, it.url, text)
        check = ai.fact_check(md, text)
        final = check.get("corrected_markdown") or md
        caption = (
            f"DRAFT for review: {p['headline']}\n"
            f"Fact check: {check.get('supported', '?')} claims supported, {check.get('unsupported', '?')} removed or marked [CHECK].\n"
            + (f"Flags: {'; '.join(f for f in check.get('controversy_flags', []) if f)}\n" if any(check.get('controversy_flags', [])) else "")
            + "Nothing is published until you say yes."
        )
        telegram.send_document(f"{now:%Y-%m-%d}-{slugify(p['headline'])}.md", final, caption)
        for t in latest["topics"]:
            if t["headline"] == p["headline"]:
                t["draft"] = final
        seen.save_latest(latest)

    if not DRY_RUN:
        seen.add_many([i.key() for i in fresh])
    return 0


def run_radar() -> None:
    """CQC report radar, run separately so a problem in one never stops the other."""
    import prospects

    try:
        prospects.run(Store(), telegram)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        try:
            telegram.send_message("The CQC report radar hit an error today:\n\n" + tb[-2000:])
        except Exception:
            pass


def main() -> None:
    rc = 0
    try:
        rc = run()
    except Exception:
        rc = 1
        tb = traceback.format_exc()
        print(tb)
        try:
            telegram.send_message("The Well-Led Network morning watcher hit an error and stopped:\n\n" + tb[-3000:])
        except Exception:
            pass
    run_radar()
    sys.exit(rc)


if __name__ == "__main__":
    main()
