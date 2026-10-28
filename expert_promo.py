"""Well-Led Network Expert Sessions: everything after the recording, from one Telegram reply.

Flow
  Thursday      Telegram reminders at 7am, 3:30pm and 5pm list the approved guests waiting to record.
  You reply     recorded 123      (123 = the Clustdoc number shown in the list)
  Render then, without anything else from you:
    1. moves the guest to stage 4 (Session recorded) in Clustdoc
    2. reads their profile, downloads their photo, and has Claude write the posts and the write-up
    3. designs two branded graphics (session announcement + three takeaways)
    4. schedules the posts in AdaptlyPost: announcement next morning 8:30, takeaways 4 days later 12:30
       (your LinkedIn, the Well-Led Network LinkedIn page, Facebook, Instagram, Threads)
    5. puts the guest's promotion pack in their Clustdoc portal (both graphics, a ready-to-share post,
       the dates, a Marketplace reminder). Clustdoc's own sequence emails them about it, so no duplicate emails
    6. queues the write-up for the normal publishing run, and moves the guest to stage 6
    7. when the write-up goes live: stage 7, which starts the Clustdoc thank-you and referral sequence
  You get the graphics and a summary on Telegram, and can edit or remove any post in AdaptlyPost
  before it goes out.

Render settings (wln-secrets)
  ADAPTLY_API_KEY      required (AdaptlyPost > Manage > API & AI Agents; posts.schedule + media.upload)
  CLUSTDOC_API_TOKEN   already set; needs attachments:read and attachments:write added (photos + pack)
  ADAPTLY_LINKEDIN_ME / ADAPTLY_LINKEDIN_PAGE / ADAPTLY_FACEBOOK_PAGE / ADAPTLY_INSTAGRAM / ADAPTLY_THREADS
                       optional overrides of the connected account ids below
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import textwrap
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import expert_sessions as es

UK = ZoneInfo("Europe/London")
ADAPTLY = "https://post.adaptlypost.com/post/api/v1"
MARKETPLACE = "https://www.welllednetwork.com/channels/business-services-marketplace"
LOGO_URL = "https://ezycom.b-cdn.net/7/551/7cb67957e5cf/well-led-network-logo-2-mthontv1hmeh.png"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"

PROMO_DONE = "wln:expert_promo"        # hash app id -> summary
ASKED = "wln:expert_asked"             # date key, once per Thursday

ACCOUNTS = {
    "linkedin_me": os.getenv("ADAPTLY_LINKEDIN_ME", "cmvb2qlrr00jk01s8upgbbeio"),
    "linkedin_page": os.getenv("ADAPTLY_LINKEDIN_PAGE", "cmvpvi24j00bb01mvlmbkrmm1"),
    "facebook": os.getenv("ADAPTLY_FACEBOOK_PAGE", "cmvb2tcuu00jo01s83n2dc8lf"),
    "instagram": os.getenv("ADAPTLY_INSTAGRAM", "cmvb2udp800jp01s8lgjg96fk"),
    "threads": os.getenv("ADAPTLY_THREADS", "cmvbhnt6q000301p1kuydyd82"),
}

NAVY, GOLD, WHITE, SOFT = (14, 42, 74), (212, 168, 67), (255, 255, 255), (214, 224, 236)

COPY_SYSTEM = """You write promotion for the Well-Led Network Expert Sessions: recorded 45-minute conversations
between Yeukai Kajidori and a guest expert, for registered managers and care leaders in UK health and
social care. Everything you write must read as if Yeukai (or, for the guest_* fields, the guest) sat down
and wrote it himself. It must never sound like AI.

WHO YEUKAI IS (use only when it genuinely fits the topic, one detail at a time, never all at once):
23 years in UK health and social care, 15 as a CQC Registered Manager. Started in an Ofsted-regulated
children's home. Registered Manager at Home Angels (homecare, two branches, about 3,000 hours a week, Good
CQC ratings). Ran operations at Support Horizons in Berkshire (learning disability, autism and mental health
support; local authority Preferred Provider with zero deficiencies). Grew PLS, a mental health supported
living provider, from one site to five services in two years, and took a failing service from Embargo to
Green with the local authority. External Director at MT Supported Living in Swindon.

STRUCTURE FOR EVERY POST AND THE WRITE-UP: the SLAY model.
- S, Story: open with a real, specific moment. Use what the guest wrote in their application, or tie the
  topic to one real setting from Yeukai's own career above (for example "When I was running PLS..." or
  "At Home Angels we..."). Name the real setting; real names of services, roles and regulators (CQC,
  Ofsted, the local authority, supported living, domiciliary care) make it credible and current.
- L, Lesson: the one thing that story taught, in a sentence or two.
- A, Application: exactly three practical, actionable tips a registered manager could use this week. Give
  the how away: specific steps, records, questions or routines, not vague advice. Number them 1, 2, 3.
- Y, Your ending: a clear call to action that suits the platform (rules below).

VOICE: British English. Calm, warm, plain-spoken, professional; how an experienced registered manager talks
to a peer. Short sentences and short paragraphs with a blank line between them. Make the guest the hero
and Yeukai the guide. No hype, no emojis, no shaming, no prices.

NEVER SOUND LIKE AI. Do not use: em dashes or en dashes (use commas or full stops), "delve", "navigate",
"landscape", "in today's", "game-changer", "unlock", "empower", "elevate", "journey", "seamless", "robust",
"tapestry", "testament", "it's not just X, it's Y", "let's dive in", "in conclusion", rhetorical questions
stacked in a row, or lists of three adjectives. No headings or bold inside social posts. Vary sentence
length. Write the way a person speaks.

FORMAT: inside the JSON values, never use double quotation marks; use single quotation marks if you need to
quote anything. Use \\n for line breaks.

FACTS: use only what the guest wrote in their application and the facts about Yeukai above. Never invent
statistics, quotes, results, names or events.

PLATFORM RULES AND CALLS TO ACTION:
- linkedin_me: Yeukai's personal LinkedIn. 1,000-1,250 characters. NO links at all. Name the guest in plain
  text. End with a call to action such as "The full session, and where to find [guest], is on my profile."
- linkedin_page: the Well-Led Network company page. 600-900 characters. NO links. End pointing readers to
  the link on the Well-Led Network page to watch the session.
- facebook: 700-1,100 characters. End with a clear call to action and this link exactly: MARKETPLACE_URL
- instagram: 500-900 characters, no links. End with "The full session is linked in my bio." then 3 to 5
  relevant hashtags on their own line.
- threads: under 480 characters, no links, one short story line, one lesson, one tip, then "More on my
  profile."
- takeaways_*: the same five fields again for a second post a few days later. Lead with a different short
  story, then the guest's three takeaways turned into three tips a manager can act on this week, then the
  same style of call to action.

Return JSON only with these keys:
{"card_topic": "the session topic in at most 9 words",
 "takeaways": ["three takeaways, each at most 12 words"],
 "linkedin_me": "...", "linkedin_page": "...", "facebook": "...", "instagram": "...", "threads": "...",
 "takeaways_linkedin_me": "...", "takeaways_linkedin_page": "...", "takeaways_facebook": "...",
 "takeaways_instagram": "...", "takeaways_threads": "...",
 "alt_announce": "alt text for the announcement graphic", "alt_takeaways": "alt text for the takeaways graphic",
 "guest_linkedin": "the guest's own LinkedIn post about appearing, in their voice and the SLAY model, 900-1,200 characters, no links, three practical tips from their session, mentions Yeukai Kajidori and the Well-Led Network by name, ends inviting people to find them in the Well-Led Network Business Services Marketplace via their profile",
 "guest_facebook": "the guest's own Facebook post, SLAY model, 600-1,000 characters, three practical tips, ends with a call to action and this link exactly: MARKETPLACE_URL",
 "guest_instagram": "the guest's own Instagram caption, SLAY model, 500-800 characters, no links, three short tips, ends with 'Link in bio.' and 3 to 5 relevant hashtags",
 "writeup_title": "an SEO-friendly article title naming the topic and the guest, no colon-heavy clickbait",
 "writeup_markdown": "a 700-1,000 word standalone Well-Led Network article in markdown, written in Yeukai's voice using the SLAY model: open with a real story (the guest's, or one of Yeukai's real settings that fits), the lesson, then three practical tips under short H2 headings with specific steps a manager can take this week, then who the guest is, their give-back and how to reach them, and finish with a short paragraph inviting readers to find the guest in the Business Services Marketplace at MARKETPLACE_URL and to join the free Well-Led Network. No H1."}"""


# ---------------------------------------------------------------- small helpers
def _send_photo(png: bytes, caption: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", timeout=60,
                  data={"chat_id": chat, "caption": caption[:1000]}, files={"photo": ("card.png", png, "image/png")})


def _fields(items: list[dict]) -> dict:
    """Profile answers keyed by a short lower-case label."""
    out = {}
    for line in es._answers(items).split("\n"):
        if ":" in line and not line.startswith("["):
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _pick(fields: dict, *words: str) -> str:
    for k, v in fields.items():
        if all(w in k for w in words):
            return v
    return ""


def _download(url: str, log: list | None = None) -> bytes | None:
    """Clustdoc download links are usually pre-signed, so try without our key first, then with it."""
    if not url:
        return None
    for headers in ({}, {"Authorization": f"Bearer {es._token()}"}):
        try:
            r = requests.get(url, timeout=60, headers=headers, allow_redirects=True)
            if log is not None:
                log.append(f"download {'with' if headers else 'without'} key: {r.status_code}, {len(r.content)} bytes")
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception as exc:
            if log is not None:
                log.append(f"download error: {str(exc)[:100]}")
    return None


def _guest_file(items: list[dict], word: str, log: list | None = None) -> bytes | None:
    for it in items:
        if word in str(it.get("title", "")).lower() and it.get("type") in ("file", "fileshare", "custom", None):
            r = es._cd("GET", f"/application-items/{it['id']}/uploads")
            if log is not None:
                log.append(f"item {it['id']} uploads: {r.status_code} {r.text[:300]}")
            if r.status_code != 200:
                continue
            for up in r.json().get("data") or []:
                url = (up.get("download_url") or up.get("url") or up.get("file_url")
                       or (up.get("links") or {}).get("download") or "")
                data = _download(url, log)
                if data:
                    return data
    if log is not None and not any(word in str(i.get("title", "")).lower() for i in items):
        log.append(f"no step with '{word}' in its title; types seen: {sorted({str(i.get('type')) for i in items})}")
    return None


# ---------------------------------------------------------------- graphics
_font_cache: dict = {}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = "/tmp/montserrat.ttf"
    try:
        if not os.path.exists(path):
            r = requests.get(FONT_URL, timeout=30)
            r.raise_for_status()
            open(path, "wb").write(r.content)
        f = ImageFont.truetype(path, size)
        try:
            f.set_variation_by_name("Bold" if bold else "Regular")
        except Exception:
            pass
    except Exception:
        f = ImageFont.load_default(size=size)
    _font_cache[key] = f
    return f


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _canvas():
    from PIL import Image, ImageDraw

    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 14], fill=GOLD)
    d.rectangle([0, H - 110, W, H], fill=(10, 30, 54))
    d.text((W // 2, H - 55), "Find this expert in the Well-Led Business Services Marketplace", font=_font(28),
           fill=SOFT, anchor="mm")
    return img, d, W, H


_logo_cache: list = []


def _logo():
    from PIL import Image

    if not _logo_cache:
        try:
            logo = Image.open(io.BytesIO(requests.get(LOGO_URL, timeout=30).content)).convert("RGBA")
            logo.thumbnail((360, 120))
            pad = Image.new("RGBA", (logo.width + 40, logo.height + 24), (255, 255, 255, 255))
            pad.paste(logo, (20, 12), logo)
            _logo_cache.append(pad)
        except Exception:
            _logo_cache.append(None)
    return _logo_cache[0]


def _layout(blocks, W, H):
    """blocks: list of (height, draw_fn(y)). Centres the whole stack between the top bar and the footer."""
    top, bottom = 14, H - 110
    total = sum(h for h, _ in blocks)
    y = top + max(30, (bottom - top - total) // 2)
    for h, fn in blocks:
        fn(y)
        y += h


def _header_blocks(img, d, W, label: str):
    logo = _logo()
    blocks = []
    if logo is not None:
        blocks.append((logo.height + 40, lambda y: img.paste(logo, ((W - logo.width) // 2, y), logo)))
    else:
        blocks.append((80, lambda y: d.text((W // 2, y + 30), "The Well-Led Network", font=_font(48, True),
                                            fill=WHITE, anchor="mm")))
    blocks.append((80, lambda y: d.text((W // 2, y + 20), label.upper(), font=_font(34, True), fill=GOLD,
                                        anchor="mm")))
    return blocks


def _text_block(d, W, text, font, colour, width, max_lines, lh, after=0):
    lines = _wrap(d, text, font, width)[:max_lines]

    def draw(y):
        for i, line in enumerate(lines):
            d.text((W // 2, y + i * lh + lh // 2), line, font=font, fill=colour, anchor="mm")
    return (len(lines) * lh + after, draw)


def announce_card(name: str, role: str, topic: str, photo: bytes | None) -> bytes:
    from PIL import Image, ImageDraw, ImageOps

    img, d, W, H = _canvas()
    blocks = _header_blocks(img, d, W, "Expert Session")
    size = 440
    if photo:
        try:
            p = ImageOps.fit(Image.open(io.BytesIO(photo)).convert("RGB"), (size, size))
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
            ring = Image.new("RGB", (size + 18, size + 18), GOLD)
            rmask = Image.new("L", ring.size, 0)
            ImageDraw.Draw(rmask).ellipse([0, 0, ring.width, ring.height], fill=255)

            def draw_photo(y):
                img.paste(ring, ((W - ring.width) // 2, y), rmask)
                img.paste(p, ((W - size) // 2, y + 9), mask)
            blocks.append((size + 18 + 40, draw_photo))
        except Exception:
            pass
    blocks.append(_text_block(d, W, name, _font(68, True), WHITE, 960, 2, 80, 4))
    blocks.append(_text_block(d, W, role, _font(34), SOFT, 900, 2, 46, 26))
    blocks.append((44, lambda y: d.line([W // 2 - 90, y + 10, W // 2 + 90, y + 10], fill=GOLD, width=5)))
    blocks.append(_text_block(d, W, topic, _font(56, True), WHITE, 940, 3, 70))
    _layout(blocks, W, H)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def takeaways_card(name: str, topic: str, takeaways: list[str]) -> bytes:
    img, d, W, H = _canvas()
    blocks = _header_blocks(img, d, W, "Three takeaways")
    blocks.append(_text_block(d, W, topic, _font(54, True), WHITE, 960, 3, 68, 6))
    blocks.append(_text_block(d, W, f"with {name}", _font(36), SOFT, 900, 1, 48, 50))
    f = _font(44)
    for i, t in enumerate(takeaways[:3], 1):
        lines = _wrap(d, t, f, 760)[:3]
        h = max(96, len(lines) * 58) + 46

        def draw(y, i=i, lines=lines):
            d.ellipse([90, y, 172, y + 82], fill=GOLD)
            d.text((131, y + 41), str(i), font=_font(46, True), fill=NAVY, anchor="mm")
            for j, line in enumerate(lines):
                d.text((205, y + 12 + j * 58), line, font=f, fill=WHITE)
        blocks.append((h, draw))
    _layout(blocks, W, H)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- AdaptlyPost
def _ad(method: str, path: str, **kw):
    return requests.request(method, f"{ADAPTLY}{path}", timeout=60,
                            headers={"Authorization": f"Bearer {os.getenv('ADAPTLY_API_KEY', '').strip()}",
                                     "Content-Type": "application/json"}, **kw)


def _upload(png: bytes, name: str) -> str:
    r = _ad("POST", "/upload-urls", json={"files": [{"fileName": name, "mimeType": "image/png"}]})
    r.raise_for_status()
    u = r.json()["urls"][0]
    p = requests.put(u["uploadUrl"], data=png, headers={"Content-Type": "image/png"}, timeout=120)
    p.raise_for_status()
    return u["publicUrl"]


def _schedule(texts: dict, prefix: str, media: str, alt: str, when: datetime,
              check_only: bool = False) -> list[str]:
    at = when.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    base = {"contentType": "IMAGE", "mediaUrls": [media], "mediaAltTexts": [alt[:990]],
            "timezone": "Europe/London", "scheduledAt": at}
    results = []
    jobs = [
        ({**base, "platforms": ["LINKEDIN", "FACEBOOK", "INSTAGRAM", "THREADS"],
          "text": texts[prefix + "linkedin_me"],
          "platformTexts": [{"platform": "FACEBOOK", "text": texts[prefix + "facebook"]},
                            {"platform": "INSTAGRAM", "text": texts[prefix + "instagram"]},
                            {"platform": "THREADS", "text": texts[prefix + "threads"]}],
          "linkedinConnectionIds": [ACCOUNTS["linkedin_me"]], "pageIds": [ACCOUNTS["facebook"]],
          "instagramConnectionIds": [ACCOUNTS["instagram"]], "threadsConnectionIds": [ACCOUNTS["threads"]]},
         "your LinkedIn, Facebook, Instagram, Threads"),
        ({**base, "platforms": ["LINKEDIN"], "text": texts[prefix + "linkedin_page"],
          "linkedinConnectionIds": [ACCOUNTS["linkedin_page"]]}, "Well-Led Network LinkedIn page"),
    ]
    for body, label in jobs:
        if check_only:
            r = _ad("POST", "/social-posts/validate", json=body)
            if r.status_code in (200, 201):
                rep = r.json() if r.text else {}
                ok = rep.get("valid", True)
                probs = "; ".join(i.get("message", "") for i in (rep.get("issues") or [])[:3])
                results.append(f"{label}: {'would schedule fine' if ok else 'PROBLEM'} for "
                               f"{when.strftime('%a %d %b %H:%M')}" + (f" ({probs})" if probs else ""))
            else:
                results.append(f"{label}: check not available ({r.status_code}: {r.text[:120]}), nothing created")
            continue
        r = _ad("POST", "/social-posts", json=body)
        if r.status_code in (200, 201):
            results.append(f"{label}: scheduled {when.strftime('%a %d %b %H:%M')}")
        else:
            results.append(f"{label}: NOT scheduled ({r.status_code}: {r.text[:160]})")
    return results


# ---------------------------------------------------------------- promotion pack into the guest's portal
def _portal_pack(app_id: int, texts: dict, when1: datetime, when2: datetime, cards: list[tuple[str, bytes]]) -> str:
    """Puts the graphics and a ready-to-post caption in the guest's Clustdoc portal. Clustdoc's own
    'Your promotion pack' email (3 days after stage 7) tells them to log in and download it, so no email here."""
    d1, d2 = when1.strftime('%A %d %B'), when2.strftime('%A %d %B')
    text = (f"YOUR EXPERT SESSION PROMOTION PACK\n\n"
            f"Two graphics are attached below. Both are sized to work on LinkedIn, Facebook and Instagram.\n"
            f"- expert-session.png: post this first, on or after {d1}.\n"
            f"- three-takeaways.png: post this a few days later, on or after {d2}.\n\n"
            f"We share your session on our channels on those same dates, so posting on or after them means "
            f"your audience and ours see it together.\n\n"
            f"LINKEDIN (attach expert-session.png, tag Yeukai Kajidori and The Well-Led Network page):\n"
            f"{texts.get('guest_linkedin') or texts.get('guest_share_post', '')}\n\n"
            f"FACEBOOK (attach expert-session.png):\n{texts.get('guest_facebook', '')}\n\n"
            f"INSTAGRAM (attach expert-session.png, tag @yeukaik):\n{texts.get('guest_instagram', '')}\n\n"
            f"For the second post, use three-takeaways.png with a line in your own words about the takeaway that "
            f"matters most to you.\n\n"
            f"ONE MORE THING: make sure your services are listed in the Business Services Marketplace, as that is "
            f"where we send everyone who wants to work with you: {MARKETPLACE}")
    try:
        return es.add_portal_item(app_id, "fileshare", "Your promotion pack", text,
                                  [(n, b, "image/png") for n, b in cards])[1]
    except Exception as exc:
        return f"promotion pack NOT added to the portal ({str(exc)[:120]}); graphics are in Telegram"


def _send_writeup(texts: dict, name: str) -> None:
    """Test runs: every post and the full write-up to Telegram, in readable pieces."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    posts = [("Your LinkedIn", "linkedin_me"), ("Well-Led Network LinkedIn page", "linkedin_page"),
             ("Facebook", "facebook"), ("Instagram", "instagram"), ("Threads", "threads")]
    parts = ["POSTS - day after recording, with graphic 1:\n\n" +
             "\n\n".join(f"[{lab}]\n{texts.get(k, '')}" for lab, k in posts),
             "POSTS - four days later, with graphic 2:\n\n" +
             "\n\n".join(f"[{lab}]\n{texts.get('takeaways_' + k, '')}" for lab, k in posts),
             "GUEST'S OWN POSTS (in their promotion pack):\n\n[LinkedIn]\n" + texts.get("guest_linkedin", "") +
             "\n\n[Facebook]\n" + texts.get("guest_facebook", "") + "\n\n[Instagram]\n" + texts.get("guest_instagram", ""),
             "WRITE-UP for the Well-Led Network:\n\n# " + texts.get("writeup_title", "") + "\n\n" +
             texts.get("writeup_markdown", "")]
    for part in parts:
        while part:
            cut = part[:4000]
            if len(part) > 4000 and "\n" in cut:
                cut = cut[:cut.rfind("\n")]
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=60,
                          data={"chat_id": chat, "text": cut, "disable_web_page_preview": "true"})
            part = part[len(cut):].lstrip("\n")


# ---------------------------------------------------------------- the main job
def _stage(app_id: int, stage: str) -> str:
    try:
        r = es._cd("PATCH", f"/applications/{app_id}", json={"stage": stage})
        return "" if r.status_code == 200 else f"stage '{stage[:2]}' not set ({r.status_code})"
    except Exception as exc:
        return f"stage not set ({exc})"


def _next_at(base: datetime, days: int, hh: int, mm: int) -> datetime:
    return (base + timedelta(days=days)).replace(hour=hh, minute=mm, second=0, microsecond=0)


def run(store, send, app_id: int, test: bool = False) -> str:
    """test=True: a full rehearsal that posts nothing, moves no stages and publishes nothing."""
    import ai

    notes = []
    r = es._cd("GET", f"/applications/{app_id}")
    if r.status_code != 200:
        return f"Couldn't open Clustdoc application {app_id} ({r.status_code})."
    app = r.json().get("data") or {}
    contact = app.get("contact") or {}
    items = es._items(app_id)
    if not test:
        notes.append(_stage(app_id, "4. Session recorded"))
    f = _fields(items)
    name = _pick(f, "name", "shown") or es._name(app)
    title, org = _pick(f, "job title"), _pick(f, "organisation name")
    role = title if (org and org.lower() in title.lower()) else ", ".join(x for x in (title, org) if x)
    answers = es._answers(items)

    reply = ai._ask(COPY_SYSTEM.replace("MARKETPLACE_URL", MARKETPLACE),
                    f"Guest: {name}\nRole: {role}\n\nTheir application:\n{answers}", max_tokens=8000)
    try:
        texts = ai._json(reply)
    except Exception:  # a stray quotation mark inside a post breaks the JSON; have Claude repair it once
        fixed = ai._ask("You repair JSON. Return only the corrected JSON object with exactly the same keys and "
                        "wording. Escape every double quotation mark inside a string value as \\\" and change "
                        "nothing else.", reply, max_tokens=8000)
        texts = ai._json(fixed)

    photo = _guest_file(items, "photo")
    if not photo:
        notes.append("no guest photo found (add attachments:read to the Clustdoc token), graphic made without it")
    card1 = announce_card(name, role, texts["card_topic"], photo)
    card2 = takeaways_card(name, texts["card_topic"], texts["takeaways"])
    _send_photo(card1, f"Expert Session graphic 1 of 2: {name}")
    _send_photo(card2, "Graphic 2 of 2: three takeaways")

    now = datetime.now(UK)
    when1 = _next_at(now, 1, 8, 30)
    when2 = _next_at(now, 5, 12, 30)
    if not os.getenv("ADAPTLY_API_KEY", "").strip():
        notes.append("posts NOT scheduled: ADAPTLY_API_KEY is not set in Render")
    elif test:
        try:
            m1 = _upload(card1, f"expert-{app_id}-session-test.png")
            notes.append("AdaptlyPost: connected, graphic uploaded fine")
            notes += _schedule(texts, "", m1, texts.get("alt_announce", ""), when1, check_only=True)
            notes += _schedule(texts, "takeaways_", m1, texts.get("alt_takeaways", ""), when2, check_only=True)
        except Exception as exc:
            notes.append(f"AdaptlyPost problem: {str(exc)[:200]}")
    else:
        try:
            m1 = _upload(card1, f"expert-{app_id}-session.png")
            m2 = _upload(card2, f"expert-{app_id}-takeaways.png")
            notes += _schedule(texts, "", m1, texts.get("alt_announce", ""), when1)
            notes += _schedule(texts, "takeaways_", m2, texts.get("alt_takeaways", ""), when2)
        except Exception as exc:
            notes.append(f"AdaptlyPost problem: {str(exc)[:200]}")

    notes.append(_portal_pack(app_id, texts, when1, when2,
                              [("expert-session.png", card1), ("three-takeaways.png", card2)]))

    today = now.strftime("%Y-%m-%d")
    if test:
        _send_writeup(texts, name)
        summary = ("TEST RUN for " + name + " (nothing posted, no stages moved, nothing published).\n"
                   + "\n".join("- " + n for n in notes if n)
                   + "\n\nAbove: both graphics, every post as it would go out, and the full write-up.")
        send(summary)
        return summary
    a = store.put_approval({
        "number": 900 + (app_id % 100), "headline": texts["writeup_title"], "date": today,
        "angle": "Expert Session write-up", "product": "", "lead_magnet": "", "key_date": "",
        "source": "Expert Session", "source_title": name, "url": MARKETPLACE,
        "draft": "# " + texts["writeup_title"] + "\n\n" + texts["writeup_markdown"],
        "status": "approved", "approved_at": int(time.time()), "expert_app_id": app_id,
    })
    notes.append(f"write-up queued for the next publishing run (topic {a['number']})")
    notes.append(_stage(app_id, "6. Promotion scheduled - LinkedIn and socials"))
    if store.r:
        store.r.hset(PROMO_DONE, str(app_id), today)
    summary = (f"Expert Session with {name} is in motion.\n" + "\n".join("- " + n for n in notes if n) +
               "\n\nYou can edit or remove any post in AdaptlyPost before it goes out. When the write-up is live,"
               " the guest moves to stage 7 and the Clustdoc thank-you and referral sequence starts.")
    send(summary)
    return summary


def on_writeup_published(store, a: dict, send) -> None:
    app_id = a.get("expert_app_id")
    if not app_id:
        return
    _stage(app_id, "5. Live on Well-Led Network + Marketplace listing")
    msg = _stage(app_id, "7. Write-up published")
    send(f"Expert Session write-up is live, so the guest is now at stage 7 and the Clustdoc thank-you and "
         f"referral sequence has started." + (f"\n{msg}" if msg else ""))


# ---------------------------------------------------------------- Telegram
def waiting_list(store) -> list[tuple[str, str]]:
    if not store.r:
        return []
    approved = [k for k, v in store.r.hgetall(es.DONE).items() if v.startswith("approved")]
    done = store.r.hgetall(PROMO_DONE)
    return [(k, store.r.hget(es.DONE, k)) for k in approved if k not in done]


def _waiting_lines(store) -> list[str]:
    lines = []
    for k, _ in waiting_list(store):
        r = es._cd("GET", f"/applications/{k}")
        nm = es._name(r.json().get("data") or {}) if r.status_code == 200 else "guest"
        lines.append(f"{k}: {nm}")
    return lines


AFTER = ("Straight after the recording:\n"
         "1. Reply here: recorded 123 (the number next to your guest).\n"
         "2. Download the video from Vectera and upload it to the Thursday Clinic on the Well-Led Network.\n"
         "Everything else (graphics, posts, guest pack, write-up) is done for you.")


def thursday_prompt(store, send) -> None:
    """Thursday reminders: 7am (with the morning brief), 3:30pm (before the 4pm recording) and 5pm (after it).
    Only sent when an approved guest is waiting to record."""
    now = datetime.now(UK)
    if now.weekday() != 3 or not store.r or not es._token():
        return
    if now.hour == 7:
        slot = "morning"
    elif now.hour == 15 and now.minute >= 30:
        slot = "before"
    elif now.hour == 17:
        slot = "after"
    else:
        return
    if not store.r.set(f"{ASKED}:{now:%Y%m%d}:{slot}", "1", nx=True, ex=86400):
        return
    lines = _waiting_lines(store)
    if not lines:
        return
    guests = "\n".join(lines)
    if slot == "morning":
        send("Expert Session today, recording at 4pm in Vectera.\nApproved guests waiting to record:\n"
             f"{guests}\n\n{AFTER}")
    elif slot == "before":
        send(f"Expert Session recording starts in 30 minutes (4pm, Vectera).\n{guests}\n\n{AFTER}")
    else:
        send(f"Has today's Expert Session been recorded?\n{guests}\n\n{AFTER}")


def handle(text: str, store, send) -> str | None:
    t = re.match(r"^recorded\s+test\s+(\d+)$", text.strip().lower())
    if t:
        app_id = int(t.group(1))
        threading.Thread(target=lambda: _safe(store, send, app_id, True), daemon=True).start()
        return (f"Test run for {app_id}: writing the posts, graphics and write-up now. Nothing will be posted "
                "or published; you'll get everything here in a few minutes.")
    m = re.match(r"^recorded\s+(\d+)$", text.strip().lower())
    if not m:
        return None
    app_id = int(m.group(1))
    if store.r and store.r.hexists(PROMO_DONE, str(app_id)):
        return f"{app_id} has already been promoted. Nothing done."
    if not es._token():
        return "CLUSTDOC_API_TOKEN isn't set in Render."
    threading.Thread(target=lambda: _safe(store, send, app_id), daemon=True).start()
    return f"On it. Writing the posts and designing the graphics for {app_id} now; you'll get them here in a few minutes."


def _safe(store, send, app_id: int, test: bool = False) -> None:
    try:
        run(store, send, app_id, test)
    except Exception as exc:
        send(f"Expert Session promotion for {app_id} hit a problem: {type(exc).__name__}: {str(exc)[:300]}")
