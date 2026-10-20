"""On-disk cache of GET responses: one sqlite file in WAL mode, safe for concurrent threads and processes."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

THREAD_TTL = 6 * 3600.0
SEARCH_TTL = 3600.0
PRUNE_AGE = 24 * 3600.0  # older than the longest TTL: dead weight
BUSY_TIMEOUT = 30.0


def default_path() -> Path:
    override = os.environ.get("FORUMREAD_CACHE")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "forumread" / "cache.db"


class Cache:
    """URL -> response text with a per-lookup TTL. Failures of the cache itself degrade to a miss (noted once)."""

    def __init__(self, path: Path | str | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._clock = clock
        self._ready = False
        self._lock = threading.Lock()
        self._warned = False

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS responses (url TEXT PRIMARY KEY, fetched REAL NOT NULL, body TEXT NOT NULL)"
            )
            conn.execute("DELETE FROM responses WHERE fetched < ?", (self._clock() - PRUNE_AGE,))
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        with self._lock:  # sqlite connections are per call: they must not be shared between threads
            if not self._ready:
                self._init()
                self._ready = True
        return sqlite3.connect(self.path, timeout=BUSY_TIMEOUT)

    def _degrade(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            print(f"forumread: note: cache unavailable ({exc}); continuing without it", file=sys.stderr)

    def get(self, url: str, ttl: float) -> str | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT body FROM responses WHERE url = ? AND fetched >= ?", (url, self._clock() - ttl)
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._degrade(exc)
            return None
        return row[0] if row else None

    def put(self, url: str, body: str) -> None:
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO responses (url, fetched, body) VALUES (?, ?, ?)",
                        (url, self._clock(), body),
                    )
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._degrade(exc)
