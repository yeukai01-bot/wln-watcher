"""Shared memory for the watcher and the reply service.

Holds: which source items were already reported, the latest morning list, and the approvals queue.
Uses Render Key Value (Redis compatible) when REDIS_URL is set, otherwise a local JSON file for tests.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

SEEN = "wln:seen"
LATEST = "wln:latest_list"
APPROVALS = "wln:approvals"
TG_OFFSET = "wln:tg_offset"
_lock = threading.Lock()


class Store:
    def __init__(self) -> None:
        self.r = None
        url = os.getenv("REDIS_URL")
        if url:
            import redis

            self.r = redis.Redis.from_url(url, decode_responses=True)
        self.path = Path(os.getenv("STATE_FILE", "state.json"))

    # ---------- local file fallback ----------
    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"seen": [], "latest": None, "approvals": {}}

    def _save(self, d: dict) -> None:
        d["seen"] = d["seen"][-5000:]
        self.path.write_text(json.dumps(d))

    # ---------- seen items ----------
    def is_empty(self) -> bool:
        if self.r:
            return self.r.scard(SEEN) == 0
        return not self._load()["seen"]

    def has(self, key: str) -> bool:
        if self.r:
            return bool(self.r.sismember(SEEN, key))
        return key in self._load()["seen"]

    def add_many(self, keys: list[str]) -> None:
        if not keys:
            return
        if self.r:
            self.r.sadd(SEEN, *keys)
            return
        with _lock:
            d = self._load()
            d["seen"] += [k for k in keys if k not in d["seen"]]
            self._save(d)

    # ---------- latest morning list ----------
    def save_latest(self, lst: dict) -> None:
        if self.r:
            self.r.set(LATEST, json.dumps(lst))
            return
        with _lock:
            d = self._load()
            d["latest"] = lst
            self._save(d)

    def latest(self) -> dict | None:
        if self.r:
            v = self.r.get(LATEST)
            return json.loads(v) if v else None
        return self._load()["latest"]

    # ---------- approvals ----------
    def _all(self) -> dict:
        if self.r:
            return {k: json.loads(v) for k, v in self.r.hgetall(APPROVALS).items()}
        return self._load()["approvals"]

    def put_approval(self, a: dict) -> dict:
        a.setdefault("id", uuid.uuid4().hex[:10])
        a.setdefault("created", int(time.time()))
        if self.r:
            self.r.hset(APPROVALS, a["id"], json.dumps(a))
            return a
        with _lock:
            d = self._load()
            d["approvals"][a["id"]] = a
            self._save(d)
        return a

    def get_approval(self, aid: str) -> dict | None:
        return self._all().get(aid)

    def approvals(self, status: str | None = None) -> list[dict]:
        items = sorted(self._all().values(), key=lambda a: a["created"])
        return [a for a in items if status is None or a.get("status") == status]
