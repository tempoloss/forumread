"""Small key/value store with expiry, shared by every forumread process through the cache sqlite file.

Holds what must outlive one process: the Reddit guest tokens, their rate-limit counters and "backend X is down" cooldowns.
Independent of `--no-cache`. A broken database degrades to a per-process dict (the values then just are not shared).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from forumread.cache import BUSY_TIMEOUT, default_path

_clock = time.time


def now() -> float:
    return _clock()


class State:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._ready = False
        self._lock = threading.Lock()
        self._warned = False
        self._memory: dict[str, tuple[str, float]] = {}

    def _connect(self) -> sqlite3.Connection:
        with self._lock:  # sqlite connections are per call; first use creates the table
            if not self._ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS state "
                        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
                    )
                    conn.execute("DELETE FROM state WHERE expires_at < ?", (now(),))
                    conn.commit()
                finally:
                    conn.close()
                self._ready = True
        return sqlite3.connect(self.path, timeout=BUSY_TIMEOUT)

    def _degrade(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            print(f"forumread: note: shared state unavailable ({exc}); continuing per process", file=sys.stderr)

    def get(self, key: str) -> str | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM state WHERE key = ? AND expires_at > ?", (key, now())).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._degrade(exc)
            held = self._memory.get(key)
            return held[0] if held and held[1] > now() else None
        return row[0] if row else None

    def set(self, key: str, value: str, ttl: float) -> None:
        expires_at = now() + ttl
        self._memory[key] = (value, expires_at)
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO state (key, value, expires_at) VALUES (?, ?, ?)", (key, value, expires_at)
                    )
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._degrade(exc)

    def delete(self, key: str) -> None:
        self._memory.pop(key, None)
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.execute("DELETE FROM state WHERE key = ?", (key,))
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._degrade(exc)

    def add(self, key: str, value: str, ttl: float) -> bool:
        """Store `value` only if `key` is absent or expired; True if this call stored it (a cross-process try-lock)."""
        moment = now()
        expires_at = moment + ttl
        try:
            conn = self._connect()
            try:
                with conn:
                    cursor = conn.execute(
                        "INSERT INTO state (key, value, expires_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                        "SET value = excluded.value, expires_at = excluded.expires_at WHERE state.expires_at <= ?",
                        (key, value, expires_at, moment),
                    )
                    added = cursor.rowcount == 1
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._degrade(exc)
            held = self._memory.get(key)
            added = not (held and held[1] > moment)
        if added:
            self._memory[key] = (value, expires_at)
        return added

    def get_json(self, key: str) -> Any:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def set_json(self, key: str, value: Any, ttl: float) -> None:
        self.set(key, json.dumps(value), ttl)


_state: State | None = None
_state_lock = threading.Lock()


def shared() -> State:
    """The process-wide State on the default cache file (`$FORUMREAD_CACHE` honoured at first use)."""
    global _state
    with _state_lock:
        if _state is None:
            _state = State()
        return _state


def reset() -> None:
    """Forget the process-wide State (a fresh process would start like this); the stored rows stay."""
    global _state
    with _state_lock:
        _state = None
