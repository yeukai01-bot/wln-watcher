"""The three Claude steps: triage the news, draft an article in Yeukai's voice, fact-check it."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anthropic

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
BRIEF = (Path(__file__).parent / "voice_brief.md").read_text()

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def _ask(system: str, user: str, max_tokens: int = 8000) -> str:
    msg = client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _json(text: str):
    """Pull the first JSON object or array out of a model reply."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    start = min([i for i in (raw.find("["), raw.find("{")) if i != -1], default=0)
    return json.loads(raw[start:])


PRODUCTS = """Yeukai's paid offers (the article should lead naturally, never pushily, towards the one that fits):
- CQC Registration Application Review (one-to-one pre-submission review of a new provider's full document set)
- CQC Registration Application Risk Check (self-service pre-submission checks)
- 30-Day Well-Led Governance Reset (governance workbook and community, for registered services)
- Inspection Ready Registered Manager (course: inspection readiness under CQC's changing framework)
- The First 5 Care Packages Blueprint (course: newly registered providers winning their first council and private work)
- From Compliance to Excellence (one-day training for mental health and supported living teams)
- Free CQC Readiness Strategy Call (for warm readers with a live problem)
Membership of the Well-Led Network is free."""

TRIAGE_SYSTEM = f"""You are the research editor for The Well-Led Network, a free community run by Yeukai Kajidori
(23 years in UK health and social care, 15 as a CQC Registered Manager). Readers are Registered Managers,
Nominated Individuals, owners and people registering a new adult social care service in England.

Your job each morning: look at new items from primary sources and pick the ones worth an article that
(1) a stressed manager needs to know about soon, and (2) is likely to bring readers who later become clients.
Being first on a genuine change matters.

{PRODUCTS}

Rules:
- Adult social care in England only. Ignore NHS-only, hospital, GP, children's services and Wales/Scotland/NI items
  unless they directly change what an adult social care provider in England must do.
- Controversy: Yeukai wants facts, not debate. Mark controversy "high" for anything party-political, a dispute
  between the sector and government, criticism of CQC or any organisation, industrial action, or a contested
  legal question. "high" items are set aside for Yeukai, never drafted.
- Never invent facts. Base the headline and angle only on what the item says.
- Headline style that performs: name the moment and the stakes in the first words, e.g.
  "A CQC Warning Notice Has Landed: The Two Clocks That Start Running",
  "The Reasonable Adjustment Digital Flag: What Care Providers Need to Do Now". Never a vague story title.
- British English. No dashes as punctuation in the headline."""

TRIAGE_USER = """Existing Well-Led Network articles (do not propose duplicates; a genuinely new development on
the same subject is fine and should say what is new):
{existing}

New items (id | source | date | title | summary):
{items}

Return ONLY a JSON array, one object per item that is relevant (omit irrelevant ones), each with:
"id", "urgency" (1 to 5, how soon a provider must act), "client_pull" (1 to 5, how strongly it leads to one of
the paid offers), "product" (the best matching offer or "none"), "controversy" ("none", "low" or "high"),
"controversy_note", "already_covered" (title of the existing article, or ""), "headline", "angle" (one sentence:
what the reader will be able to do after reading), "lead_magnet" (one practical free tool: checklist, tracker or
template, and what it does), "key_date" (any deadline or commencement date stated in the item, else "")."""


def triage(items: list[dict], existing_titles: list[str]) -> list[dict]:
    lines = "\n".join(
        f"{i['id']} | {i['source']} | {i.get('date','')} | {i['title']} | {i.get('summary','')[:300]}" for i in items
    )
    reply = _ask(
        TRIAGE_SYSTEM,
        TRIAGE_USER.format(existing="\n".join(f"- {t}" for t in existing_titles), items=lines),
        max_tokens=8000,
    )
    return _json(reply)


DRAFT_SYSTEM = f"""You write articles for The Well-Led Network in Yeukai Kajidori's voice. Follow this brief exactly:

{BRIEF}

Extra rules for this automated draft:
- Use ONLY facts found in the SOURCE TEXT provided. If a point needs a fact that is not in the sources,
  leave it out, or write it and mark it [CHECK] so Yeukai can verify it.
- Facts, not debate. If any aspect is contested or political, state only the neutral facts and list the
  issue under "NOTES FOR YEUKAI" at the very end instead of discussing it in the article.
- Do not invent anecdotes, statistics, cases, quotes or dates. Personal lines must be general and true of a
  long-serving Registered Manager.
- Use the placeholders LEAD_MAGNET_LINK, ASK_LINK and CALL_LINK exactly as the brief describes.
- After the article, add a section headed "LEAD MAGNET SPEC" describing the free tool's contents in detail
  (sections, columns, rows) so it can be built, then "NOTES FOR YEUKAI"."""


def draft(pick: dict, source_title: str, source_url: str, source_text: str, extra_sources: str = "") -> str:
    user = f"""Write the article.

Proposed headline: {pick.get('headline')}
Angle: {pick.get('angle')}
Best-fit offer to lead towards (gently, in "On paying someone"): {pick.get('product')}
Lead magnet idea: {pick.get('lead_magnet')}

SOURCE: {source_title}
URL: {source_url}
SOURCE TEXT:
{source_text}

{extra_sources}"""
    return _ask(DRAFT_SYSTEM, user, max_tokens=12000)


CHECK_SYSTEM = """You are a strict fact-checker and editor for a UK adult social care publication.
You compare a draft against its source text. You are sceptical and precise."""

CHECK_USER = """SOURCE TEXT:
{source}

DRAFT:
{draft}

Do all of the following:
1. List every factual claim in the draft (dates, deadlines, numbers, legal references, what an organisation
   says or requires). For each, say whether the source text supports it, with a short quote from the source.
2. Rewrite the draft so every unsupported claim is either removed or marked [CHECK]. Do not add new facts.
3. Remove any dash used as punctuation (em dash, en dash, spaced hyphen); keep hyphens inside words.
4. Flag anything opinionated, political, critical of an organisation, or likely to be controversial.
5. Keep the voice, structure, placeholders, LEAD MAGNET SPEC and NOTES FOR YEUKAI sections.

Return ONLY JSON: {{"supported": n, "unsupported": n, "claims": [{{"claim": "", "supported": true, "evidence": ""}}],
"controversy_flags": [""], "ready_for_review": true, "corrected_markdown": ""}}"""


def fact_check(draft_md: str, source_text: str) -> dict:
    reply = _ask(CHECK_SYSTEM, CHECK_USER.format(source=source_text, draft=draft_md), max_tokens=16000)
    return _json(reply)


def draft_and_check(topic: dict) -> tuple[str | None, dict | None, str]:
    """Research the source page, draft, fact-check. Returns (markdown, check, problem)."""
    import sources

    text = sources.page_text(topic["url"])
    if len(text) < 500:
        return None, None, f"Could not read enough of the source to draft safely: {topic['url']}"
    md = draft(topic, topic.get("source_title", ""), topic["url"], text)
    check = fact_check(md, text)
    return check.get("corrected_markdown") or md, check, ""
