"""Send the morning list and drafts to Yeukai on Telegram."""
from __future__ import annotations

import os

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _cfg():
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    return token, chat


def send_message(text: str) -> None:
    token, chat = _cfg()
    if not (token and chat):
        print("[telegram disabled]\n" + text)
        return
    # Telegram caps messages at 4096 characters; split on paragraph breaks.
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > 3900:
            chunks.append(buf)
            buf = ""
        buf += para + "\n\n"
    chunks.append(buf)
    for c in chunks:
        if c.strip():
            r = requests.post(
                API.format(token=token, method="sendMessage"),
                data={"chat_id": chat, "text": c, "disable_web_page_preview": "true"},
                timeout=30,
            )
            r.raise_for_status()


def send_document(filename: str, content: str, caption: str = "") -> None:
    token, chat = _cfg()
    if not (token and chat):
        print(f"[telegram disabled] would send {filename} ({len(content)} chars)")
        return
    r = requests.post(
        API.format(token=token, method="sendDocument"),
        data={"chat_id": chat, "caption": caption[:1000]},
        files={"document": (filename, content.encode("utf-8"), "text/markdown")},
        timeout=60,
    )
    r.raise_for_status()
