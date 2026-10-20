"""Tiny HTTP layer: User-Agent, timeout, retry with backoff, gzip, per-host politeness, optional disk cache."""

from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from forumread.cache import Cache

USER_AGENT = "forumread/0.1 (+https://github.com/tempoloss/forumread)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_WAIT = 20.0


class FetchError(Exception):
    """A remote source could not be read; the message is safe to show the user."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HostGate:
    """Bounds concurrent requests to one host and spaces request starts at least `min_interval` seconds apart."""

    def __init__(
        self,
        concurrency: int,
        min_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._slots = threading.BoundedSemaphore(concurrency)
        self._interval = min_interval
        self._clock = clock
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._lock = threading.Lock()
        self._next_start = 0.0

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._slots:
            if self._interval:
                with self._lock:  # reserve the next start time; sleep outside the lock
                    now = self._clock()
                    start = max(now, self._next_start)
                    self._next_start = start + self._interval
                if start > now:
                    self._sleep(start - now)
            yield


# host -> (max concurrent requests, minimum seconds between request starts)
HOST_POLICIES: dict[str, tuple[int, float]] = {
    "arctic-shift.photon-reddit.com": (4, 0.0),
    "api.pullpush.io": (1, 1.0),
    "hn.algolia.com": (8, 0.0),
    "api.stackexchange.com": (4, 0.0),
    "html.duckduckgo.com": (1, 2.0),
    "oauth.reddit.com": (4, 0.0),
    "www.reddit.com": (2, 0.0),
}
DEFAULT_POLICY = (4, 0.0)
_gates: dict[str, HostGate] = {}
_gates_lock = threading.Lock()


def gate_for(host: str) -> HostGate:
    with _gates_lock:
        if host not in _gates:
            _gates[host] = HostGate(*HOST_POLICIES.get(host, DEFAULT_POLICY))
        return _gates[host]


_cache: Cache | None = None


def set_cache(cache: Cache | None) -> None:
    """Install (or, with None, disable) the process-wide response cache used by get_json(cache_ttl=...)."""
    global _cache
    _cache = cache


def cached(key: str, ttl: float) -> str | None:
    """A stored body for `key` (a URL, or a marker string) younger than `ttl`; always a miss without a cache."""
    return _cache.get(key, ttl) if _cache is not None else None


def remember(key: str, body: str) -> None:
    if _cache is not None:
        _cache.put(key, body)


def with_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return f"{url}{'&' if '?' in url else '?'}{query}"


def get_bytes(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
) -> bytes:
    url = with_params(url, params)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **(headers or {})}
    )
    host = urllib.parse.urlsplit(url).netloc
    gate = gate_for(host)
    last, last_status = "no attempt made", None
    for attempt in range(retries + 1):
        wait = 2.0**attempt
        try:
            with gate.slot(), urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
            return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        except urllib.error.HTTPError as exc:
            last, last_status = f"HTTP {exc.code}", exc.code
            if exc.code not in RETRY_STATUSES:
                raise FetchError(f"{host}: {last}", exc.code) from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last, last_status = str(getattr(exc, "reason", None) or exc), None
        if attempt < retries:
            time.sleep(min(wait, MAX_RETRY_WAIT))
    raise FetchError(f"{host}: {last} (after {retries + 1} attempts)", last_status)


def get_text(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> str:
    return get_bytes(url, params, **kwargs).decode("utf-8", errors="replace")


def _is_error_payload(payload: Any) -> bool:
    """APIs here report failures as 200 + {"error": ...}; an empty/null `data` may just be an indexing gap."""
    if not isinstance(payload, dict):
        return False
    if payload.get("error") or payload.get("error_message") or payload.get("errors"):
        return True
    return "data" in payload and not payload["data"]


def get_json(url: str, params: dict[str, Any] | None = None, *, cache_ttl: float | None = None, **kwargs: Any) -> Any:
    """GET and parse JSON. With `cache_ttl` and an installed cache, a fresh stored response is reused;
    only well-formed non-error payloads are stored."""
    full_url = with_params(url, params)
    store = _cache if cache_ttl else None
    if store is not None:
        stored = store.get(full_url, cache_ttl)
        if stored is not None:
            return json.loads(stored)
    body = get_text(full_url, **kwargs)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise FetchError(f"{urllib.parse.urlsplit(full_url).netloc}: response is not JSON") from exc
    if store is not None and not _is_error_payload(payload):
        store.put(full_url, body)
    return payload
