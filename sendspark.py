"""Personal video for each radar prospect, using Sendspark Dynamic Videos.

Yeukai records ONE video in Sendspark using the variables {{first_name}} and {{company}}. Sendspark's AI
voice then speaks each manager's first name and service name, so every prospect gets their own copy.
This module adds each prospect to that dynamic video and collects the share link for the email.

Settings on Render (wln-secrets):
  SENDSPARK_API_KEY, SENDSPARK_API_SECRET   from Sendspark: Settings, API Credentials (Yeukai pastes these)
  SENDSPARK_WORKSPACE_ID, SENDSPARK_DYNAMIC_ID   the workspace and the dynamic video to use
If any is missing, the radar simply runs without videos.
"""
from __future__ import annotations

import os
import time
from urllib.parse import quote

import requests

API = "https://api-gw.sendspark.com/v1"
LAST_ERROR = [""]


def enabled() -> bool:
    return all(os.getenv(k) for k in ("SENDSPARK_API_KEY", "SENDSPARK_API_SECRET", "SENDSPARK_WORKSPACE_ID", "SENDSPARK_DYNAMIC_ID"))


def _headers() -> dict:
    return {"x-api-key": os.getenv("SENDSPARK_API_KEY", ""), "x-api-secret": os.getenv("SENDSPARK_API_SECRET", ""),
            "Accept": "application/json", "Content-Type": "application/json"}


def _base() -> str:
    return f"{API}/workspaces/{os.getenv('SENDSPARK_WORKSPACE_ID')}/dynamics/{os.getenv('SENDSPARK_DYNAMIC_ID')}"


def add(p: dict) -> bool:
    """Queue a personal video for this prospect. Returns True if Sendspark accepted it."""
    if not (enabled() and p.get("email")):
        return False
    first = (p.get("registered_manager") or "").split(" ")[0] or "there"
    body = {
        "processAndAuthorizeCharge": True,
        "prospect": {
            "contactName": first,
            "contactEmail": p["email"],
            "company": p["location_name"],
            "jobTitle": "Registered Manager",
            "backgroundUrl": (p.get("website") if (p.get("website") or "").startswith("http") else
                              ("https://" + p["website"]) if p.get("website") else p.get("cqc_page", "")),
        },
    }
    try:
        r = requests.post(f"{_base()}/prospect", json=body, headers=_headers(), timeout=40)
        if r.status_code >= 400:
            # Some API versions take the fields at the top level rather than under "prospect".
            flat = {"processAndAuthorizeCharge": True, **body["prospect"]}
            r = requests.post(f"{_base()}/prospect", json=flat, headers=_headers(), timeout=40)
        if r.status_code >= 400:
            LAST_ERROR[0] = f"HTTP {r.status_code}: {r.text[:200]}"
            return False
        return True
    except Exception as exc:
        LAST_ERROR[0] = f"{type(exc).__name__}: {exc}"
        return False


def _find_link(obj) -> str:
    """Pull the viewer link out of whatever shape the prospect record comes back in."""
    preferred = ("shareUrl", "shareLink", "videoLink", "videoUrl", "landingPageUrl", "url", "link")
    if isinstance(obj, dict):
        for k in preferred:
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http") and "sendspark" in v:
                return v
        for v in obj.values():
            got = _find_link(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_link(v)
            if got:
                return got
    return ""


def link(email: str) -> str:
    """The prospect's personal video link, or '' if it is not ready yet."""
    if not (enabled() and email):
        return ""
    try:
        r = requests.get(f"{_base()}/prospects/{quote(email)}", headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return ""
        return _find_link(r.json())
    except Exception:
        return ""


def wait_for_links(emails: list[str], max_wait: int = 600, every: int = 30) -> dict:
    """Sendspark takes a few minutes to render. Poll until every link is ready or max_wait seconds pass."""
    found: dict = {}
    deadline = time.time() + max_wait
    while True:
        for e in emails:
            if e not in found:
                got = link(e)
                if got:
                    found[e] = got
        if len(found) == len(emails) or time.time() > deadline:
            return found
        time.sleep(every)
