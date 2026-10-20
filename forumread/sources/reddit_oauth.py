"""Live Reddit through a guest OAuth token (the public Android client id, the mechanism redlib uses).

The guest tokens (a pool of up to `FORUMREAD_REDDIT_TOKENS`, minted lazily), their rate-limit counters and the "guest
tokens are refused" cooldown live in the shared `state` table, so concurrent and consecutive forumread processes reuse
the same tokens and quotas (~100 requests per 10-minute window *per token*, reported in `x-ratelimit-*` response
headers). Every failure is a FetchError; callers fall back to the archives.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from typing import Any

from forumread import http, state
from forumread.cache import SEARCH_TTL, THREAD_TTL
from forumread.http import FetchError
from forumread.model import Comment, SearchHit, Thread
from forumread.sources.reddit_model import comment_from, hits_from, match_counts, query_terms, thread_from, to_int

DEFAULT_CLIENT_ID = "ohXpoqrZYub1kg"
TOKEN_URL = "https://www.reddit.com/auth/v2/oauth/access-token/loid"
API = "https://oauth.reddit.com"
DEFAULT_USER_AGENT = "Reddit/Android Version 2024.47.0/Build 2029755 (Score 1; Vibration 1)"
TIMEOUT = 20.0
TOKEN_MARGIN = 300.0  # reuse a token until this many seconds before it expires
TOKEN_DOWN_COOLDOWN = 600.0  # after a failed token fetch every process skips live Reddit for this long
MIN_REMAINING = 2  # at or below this many requests left in the window, wait for the reset
MAX_GATE_WAIT = 10.0  # longer than this the caller is better off with the archives
MORE_BATCH = 100  # /api/morechildren accepts at most 100 ids
MAX_MORE_CALLS = 3
DEFAULT_POOL = 10  # guest tokens per machine (each has its own quota); `FORUMREAD_REDDIT_TOKENS` overrides
FULL_QUOTA = 100.0  # what a token without a quota row is assumed to have left
WINDOW = 600.0  # quota windows are 10 minutes, aligned to the wall clock
MINT_SPACING = 2.0  # at most one new token per this many seconds, across processes
MINT_LOCK_TTL = TIMEOUT + 5.0  # a mint in flight blocks other processes for at most this long
MINT_ATTEMPTS = 3  # looks at the pool (each may wait out a sibling's mint) before falling back to the gate

TOKEN_PREFIX = "reddit-oauth:token"  # + ":<slot>"
RATE_PREFIX = "reddit-oauth:rate"  # + ":<slot>"
DOWN_KEY = "reddit-oauth:down"
MINT_KEY = "reddit-oauth:minting"
MORE_BLOCKED_KEY = "reddit-oauth:morechildren-blocked"
MORE_BLOCKED_TTL = 3600.0  # /api/* answers guest tokens 403 from some networks; then branches are fetched via /comments
# Reddit's search engine treats "vs" as a required term and ranks unrelated posts above the actual comparisons
# ("uv vs poetry" -> a metroidvania list; "uv poetry" -> r/Python threads comparing them).
COMPARISON_WORDS = frozenset({"vs", "vs.", "versus"})


def reddit_query(query: str) -> str:
    kept = [word for word in query.split() if word.lower() not in COMPARISON_WORDS]
    return " ".join(kept) or query


def client_id() -> str:
    """The public client id; `FORUMREAD_REDDIT_CLIENT_ID` overrides it (read per call)."""
    return os.environ.get("FORUMREAD_REDDIT_CLIENT_ID") or DEFAULT_CLIENT_ID


def user_agent() -> str:
    """The User-Agent sent to Reddit; `FORUMREAD_REDDIT_USER_AGENT` overrides it (read per call)."""
    return os.environ.get("FORUMREAD_REDDIT_USER_AGENT") or DEFAULT_USER_AGENT


_pool_lock = threading.Lock()


class RateLimited(FetchError):
    """The guest quota is used up (or Reddit answered 429) and the reset is too far away to wait for."""


def _send(method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    """One HTTP exchange, no retries. Returns (status, lower-cased response headers, body); only transport errors raise."""
    request = urllib.request.Request(url, data=body, method=method, headers={"Accept-Encoding": "gzip", **headers})
    host = urllib.parse.urlsplit(url).netloc
    try:
        with http.gate_for(host).slot(), urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status, head, data = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, head, data = exc.code, exc.headers, exc.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise FetchError(f"{host}: {getattr(exc, 'reason', None) or exc}") from exc
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return status, {key.lower(): value for key, value in (head or {}).items()}, data


def _fetch_token() -> dict[str, str]:
    basic = b64encode(f"{client_id()}:".encode()).decode()
    status, headers, body = _send(
        "POST",
        TOKEN_URL,
        {"Authorization": f"Basic {basic}", "User-Agent": user_agent(), "Content-Type": "application/json"},
        json.dumps({"scopes": ["*", "email", "pii"]}).encode(),
    )
    if status != 200:
        raise FetchError(f"www.reddit.com: token request HTTP {status}", status)
    try:
        payload = json.loads(body)
        return {
            "token": str(payload["access_token"]),
            "loid": headers.get("x-reddit-loid", ""),
            "expires_in": float(payload.get("expires_in") or 3600),
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise FetchError("www.reddit.com: token response not understood") from exc


def pool_size() -> int:
    """How many guest tokens one machine may hold: `FORUMREAD_REDDIT_TOKENS` (default 10, at least 1; read per call)."""
    try:
        return max(int(os.environ.get("FORUMREAD_REDDIT_TOKENS", "")), 1)
    except ValueError:
        return DEFAULT_POOL


def token_key(slot: int) -> str:
    return f"{TOKEN_PREFIX}:{slot}"


def rate_key(slot: int) -> str:
    return f"{RATE_PREFIX}:{slot}"


@dataclass
class _Slot:
    """A stored guest token and what is known about its quota (`remaining` is 100 when nothing is known)."""

    index: int
    credentials: dict[str, Any]
    remaining: float
    reset_at: float

    def usable(self) -> bool:
        return self.remaining > MIN_REMAINING


def _read_slots(shared: state.State, size: int) -> dict[int, _Slot]:
    """The unexpired tokens of the pool with their quota; a quota row past its reset counts as unknown (full)."""
    moment = state.now()
    slots: dict[int, _Slot] = {}
    for index in range(size):
        stored = shared.get_json(token_key(index))
        if not isinstance(stored, dict) or not stored.get("token"):
            continue
        rate = shared.get_json(rate_key(index))
        remaining, reset_at = FULL_QUOTA, 0.0
        if isinstance(rate, dict) and float(rate.get("reset_at", 0)) > moment:
            remaining, reset_at = float(rate.get("remaining", FULL_QUOTA)), float(rate["reset_at"])
        slots[index] = _Slot(index, stored, remaining, reset_at)
    return slots


def _best(slots: dict[int, _Slot]) -> _Slot | None:
    """The token with the most quota left (lowest index on a tie), or None when every one is used up."""
    usable = [slot for slot in slots.values() if slot.usable()]
    return max(usable, key=lambda slot: (slot.remaining, -slot.index), default=None)


def _mint(shared: state.State, size: int) -> _Slot | None:
    """Fetch one new token into a free slot; None when nothing was minted (another thread or process minted or is
    minting, we waited for the spacing): the caller looks at the pool again. Raises FetchError when Reddit refuses."""
    with _pool_lock:  # one mint per process at a time; the state row below does the same across processes
        slots = _read_slots(shared, size)
        usable = _best(slots)
        if usable is not None:
            return usable  # a sibling thread just minted
        free = next((index for index in range(size) if index not in slots), None)
        if free is None:
            return None
        moment = state.now()
        if not shared.add(MINT_KEY, json.dumps({"until": moment + MINT_LOCK_TTL}), MINT_LOCK_TTL):
            held = shared.get_json(MINT_KEY)
            wait = float(held.get("until", 0)) - moment if isinstance(held, dict) else 0.0
            time.sleep(min(max(wait, 0.05), MAX_GATE_WAIT))
            return None
        try:
            fresh = _fetch_token()
        except FetchError as exc:
            shared.set(DOWN_KEY, str(exc), TOKEN_DOWN_COOLDOWN)
            raise FetchError(f"reddit guest token unavailable ({exc})") from exc
        finally:
            shared.set_json(MINT_KEY, {"until": state.now() + MINT_SPACING}, MINT_SPACING)
        stored = {"token": fresh["token"], "loid": fresh["loid"]}
        shared.delete(rate_key(free))
        shared.set_json(token_key(free), stored, max(fresh["expires_in"] - TOKEN_MARGIN, 0.0))
        return _Slot(free, stored, FULL_QUOTA, 0.0)


def _gate(slots: dict[int, _Slot]) -> _Slot:
    """Every token is used up: wait for the nearest reset when it is close, else give up (the archives take over)."""
    if not slots:
        raise FetchError("reddit guest token unavailable (no token could be minted)")
    nearest = min(slots.values(), key=lambda slot: slot.reset_at)
    wait = nearest.reset_at - state.now()
    if wait > MAX_GATE_WAIT:
        raise RateLimited(f"reddit guest quota used up (resets in {wait:.0f}s)", 429)
    if wait > 0:
        time.sleep(wait)
    return nearest


def _acquire() -> _Slot:
    """A token to send the next request with: the stored one with the most quota left. A new one is minted only when
    every stored token is used up and the pool has room; otherwise wait for the nearest reset or raise RateLimited."""
    shared = state.shared()
    size = pool_size()
    for _ in range(MINT_ATTEMPTS):
        down = shared.get(DOWN_KEY)
        if down is not None:  # checked first: a refused token must not be reused during the cooldown
            raise FetchError(f"reddit guest token unavailable (cooling down: {down})")
        slots = _read_slots(shared, size)
        best = _best(slots)
        if best is not None:
            return best
        if len(slots) >= size:
            break
        minted = _mint(shared, size)
        if minted is not None:
            return minted
    return _gate(_read_slots(shared, size))


def _spare() -> _Slot | None:
    """A stored token that still has quota, for the retry after a 429; never mints, never waits."""
    return _best(_read_slots(state.shared(), pool_size()))


def _drop(slot: _Slot, refused: str) -> None:
    """Forget a token the server refused (unless another process already replaced it)."""
    shared = state.shared()
    stored = shared.get_json(token_key(slot.index))
    if isinstance(stored, dict) and stored.get("token") == refused:
        shared.delete(token_key(slot.index))
        shared.delete(rate_key(slot.index))


def _record_quota(slot: _Slot, headers: dict[str, str], status: int) -> None:
    """Store what the response says about this token's window; a 429 marks it used up until its reset."""
    try:
        remaining = float(headers["x-ratelimit-remaining"])
        reset = float(headers["x-ratelimit-reset"])
    except (KeyError, ValueError):
        if status != 429:
            return
        remaining, reset = 0.0, WINDOW - state.now() % WINDOW  # windows end on the clock: :00, :10, ...
    if status == 429:
        remaining = 0.0
    state.shared().set_json(
        rate_key(slot.index), {"remaining": remaining, "reset_at": state.now() + reset}, max(reset, 1.0)
    )


def _get(path: str, params: dict[str, Any], ttl: float) -> Any:
    """GET `path` on oauth.reddit.com and parse the JSON. One request, plus one retry: after a 401 on a fresh or
    other token, after a 429 on another token that still has quota."""
    url = http.with_params(API + path, params)
    stored = http.cached(url, ttl)
    if stored is not None:
        return json.loads(stored)
    slot = _acquire()
    for attempt in (0, 1):
        status, headers, body = _send(
            "GET",
            url,
            {
                "Authorization": f"Bearer {slot.credentials['token']}",
                "User-Agent": user_agent(),
                "x-reddit-loid": slot.credentials.get("loid", ""),
            },
        )
        _record_quota(slot, headers, status)
        if attempt == 0 and status == 401:
            _drop(slot, slot.credentials["token"])
            slot = _acquire()
            continue
        if attempt == 0 and status == 429:
            spare = _spare()
            if spare is not None:
                slot = spare
                continue
        break
    if status == 401:  # refused even with a fresh token: stop trying everywhere for a while
        _drop(slot, slot.credentials["token"])
        state.shared().set(DOWN_KEY, "token refused (HTTP 401)", TOKEN_DOWN_COOLDOWN)
    if status == 429:
        raise RateLimited("oauth.reddit.com: HTTP 429", 429)
    if status != 200:
        raise FetchError(f"oauth.reddit.com: HTTP {status}", status)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise FetchError("oauth.reddit.com: response is not JSON") from exc
    http.remember(url, body.decode("utf-8", errors="replace"))
    return payload


def _children(listing: Any) -> list[dict[str, Any]]:
    """The `children` of a Listing (`""` stands for "no replies")."""
    if isinstance(listing, dict):
        children = (listing.get("data") or {}).get("children")
        if isinstance(children, list):
            return [child for child in children if isinstance(child, dict)]
    return []


def _search_posts(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _get(path, params, SEARCH_TTL)
    if not isinstance(payload, dict):
        raise FetchError("oauth.reddit.com: unexpected search response")
    posts = [child.get("data") or {} for child in _children(payload) if child.get("kind") == "t3"]
    return [post for post in posts if post.get("id")]


def search(query: str, subreddit: str | None, limit: int, sort: str = "relevance") -> list[SearchHit]:
    """Reddit's own relevance search: global, or inside one subreddit.

    A comparison query ("a vs b") is sent as typed first, which finds posts titled "a vs b"; when that leaves fewer
    than `limit` on-topic posts, the query without "vs" is sent too and its on-topic posts fill the rest.
    """
    path = f"/r/{urllib.parse.quote(subreddit, safe='')}/search" if subreddit else "/search"
    scope: dict[str, Any] = {"restrict_sr": "on"} if subreddit else {}
    tail = {"sort": sort, "limit": limit, "raw_json": 1}
    posts = _search_posts(path, {"q": query} | scope | tail)
    stripped = reddit_query(query)
    if stripped == query:
        return hits_from(posts, limit)
    wanted = query_terms(stripped)
    on_topic = [post for post in posts if match_counts(post, wanted)]
    if len(on_topic) < limit:
        seen = {post["id"] for post in on_topic}
        for post in _search_posts(path, {"q": stripped} | scope | tail):
            if post["id"] not in seen and match_counts(post, wanted):
                seen.add(post["id"])
                on_topic.append(post)
    return hits_from(on_topic, limit)


@dataclass
class _Stub:
    """A collapsed "load more" branch: `ids` are comments /api/morechildren can fetch; with no ids it is a
    "continue this thread" stub. `parent` is the fullname (t1_x / t3_x) the hidden comments reply to."""

    parent: str
    ids: list[str]
    count: int

    @property
    def hidden(self) -> int:
        return len(self.ids) or self.count


Row = tuple[str, Comment, str]  # (comment id, comment, parent fullname)


def _parse(children: list[dict[str, Any]], rows: list[Row], stubs: list[_Stub]) -> list[Comment]:
    """Comment tree of a listing; every comment is also appended to `rows`, every "more" stub to `stubs`."""
    nodes: list[Comment] = []
    for child in children:
        data = child.get("data") or {}
        if child.get("kind") == "t1" and data.get("id"):
            node = comment_from(data)
            rows.append((str(data["id"]), node, str(data.get("parent_id") or "")))
            node.children = _parse(_children(data.get("replies")), rows, stubs)
            nodes.append(node)
        elif child.get("kind") == "more":
            stubs.append(_Stub(str(data.get("parent_id") or ""), [str(i) for i in data.get("children") or []], to_int(data.get("count")) or 0))
    return nodes


def _attach(rows: list[Row], roots: list[Comment], by_id: dict[str, Comment]) -> None:
    """Hang flat comments under their parents (already loaded or in `rows`); unknown parents become top level."""
    for comment_id, node, _ in rows:
        by_id[comment_id] = node
    for _, node, parent_id in rows:
        parent = by_id.get(parent_id.removeprefix("t1_")) if parent_id.startswith("t1_") else None
        (roots if parent is None or parent is node else parent.children).append(node)


def _load_batch(thread_id: str, roots: list[Comment], by_id: dict[str, Comment], stubs: list[_Stub]) -> None:
    """One /api/morechildren call for the first MORE_BATCH stub ids."""
    batch: list[str] = []
    for stub in stubs:
        batch += stub.ids[: MORE_BATCH - len(batch)]
    params = {"link_id": f"t3_{thread_id}", "children": ",".join(batch), "api_type": "json", "raw_json": 1}
    result = _get("/api/morechildren", params, THREAD_TTL)["json"]
    if result.get("errors"):
        raise FetchError("oauth.reddit.com: morechildren refused the request")
    things = result["data"]["things"]
    taken = set(batch)
    kept = []
    for stub in stubs:
        had_ids = bool(stub.ids)
        stub.ids = [i for i in stub.ids if i not in taken]
        if stub.ids or not had_ids:
            kept.append(stub)
    stubs[:] = kept
    rows: list[Row] = []
    for thing in things:
        data = thing.get("data") or {}
        if thing.get("kind") == "t1" and data.get("id"):
            rows.append((str(data["id"]), comment_from(data), str(data.get("parent_id") or "")))
        elif thing.get("kind") == "more":
            stubs.append(_Stub(str(data.get("parent_id") or ""), [str(i) for i in data.get("children") or []], to_int(data.get("count")) or 0))
    _attach(rows, roots, by_id)


def _priority(stub: _Stub) -> float:
    """Comments one subtree call is expected to bring: all of a t1 stub's branch, one id's share of a top-level stub."""
    return float(stub.count) if stub.parent.startswith("t1_") else stub.count / max(len(stub.ids), 1)


def _graft(fetched: list[Comment], rows: list[Row], roots: list[Comment], by_id: dict[str, Comment]) -> None:
    """Add the comments of a focused fetch that are not loaded yet, under the loaded comment they reply to."""
    ids = {id(node): comment_id for comment_id, node, _ in rows}

    def register(node: Comment) -> None:
        by_id[ids[id(node)]] = node
        for child in node.children:
            register(child)

    def merge(node: Comment) -> None:
        known = by_id[ids[id(node)]]
        for child in node.children:
            if ids[id(child)] in by_id:
                merge(child)
            else:
                known.children.append(child)
                register(child)

    for node in fetched:
        if ids[id(node)] in by_id:
            merge(node)
        else:
            roots.append(node)
            register(node)


def _load_subtree(thread_id: str, roots: list[Comment], by_id: dict[str, Comment], stubs: list[_Stub]) -> None:
    """One /comments/<thread>/_/<comment> call: the whole branch under the best stub's parent (or, for a top-level
    stub, under its first id). Works where /api/morechildren is refused (Reddit's WAF answers it 403 to guest tokens)."""
    stub = max((s for s in stubs if s.parent.startswith("t1_") or s.ids), key=_priority, default=None)
    if stub is None:
        raise FetchError("oauth.reddit.com: no collapsed branch can be loaded")
    target = stub.parent.removeprefix("t1_") if stub.parent.startswith("t1_") else stub.ids[0]
    payload = _get(f"/comments/{thread_id}/_/{target}", {"limit": 500, "depth": 10, "raw_json": 1}, THREAD_TTL)
    rows: list[Row] = []
    fresh: list[_Stub] = []
    fetched = _parse(_children(payload[1]), rows, fresh)
    if stub.parent.startswith("t1_"):
        stubs.remove(stub)
    else:
        stub.count -= stub.count // len(stub.ids)  # the share of the id just fetched
        stub.ids.pop(0)
        if not stub.ids:
            stubs.remove(stub)
    _graft(fetched, rows, roots, by_id)
    stubs.extend(s for s in fresh if s not in stubs)


def _expand(thread_id: str, roots: list[Comment], by_id: dict[str, Comment], stubs: list[_Stub], max_comments: int | None) -> None:
    """Load collapsed branches: at most MAX_MORE_CALLS calls, only while fewer than `max_comments` comments are loaded.
    /api/morechildren (100 ids per call) when Reddit allows it; a 403 is remembered and branch fetches are used instead."""
    shared = state.shared()
    for _ in range(MAX_MORE_CALLS):
        if not stubs or (max_comments is not None and len(by_id) >= max_comments):
            return
        try:
            if any(s.ids for s in stubs) and shared.get(MORE_BLOCKED_KEY) is None:
                try:
                    _load_batch(thread_id, roots, by_id, stubs)
                    continue
                except FetchError as exc:
                    if exc.status != 403:
                        raise
                    shared.set(MORE_BLOCKED_KEY, "HTTP 403", MORE_BLOCKED_TTL)
            _load_subtree(thread_id, roots, by_id, stubs)
        except (FetchError, KeyError, IndexError, TypeError, AttributeError):
            return  # the thread itself loaded: keep what we have, the stubs count as not loaded


def thread(thread_id: str, max_comments: int | None = None) -> Thread:
    payload = _get(f"/comments/{thread_id}", {"limit": 500, "depth": 10, "raw_json": 1}, THREAD_TTL)
    if not isinstance(payload, list) or len(payload) < 2:
        raise FetchError(f"oauth.reddit.com: unexpected response for thread {thread_id}")
    posts = [child.get("data") or {} for child in _children(payload[0]) if child.get("kind") == "t3"]
    if not posts or not posts[0].get("id"):
        raise FetchError(f"oauth.reddit.com: thread {thread_id} has no post")
    rows: list[Row] = []
    stubs: list[_Stub] = []
    roots = _parse(_children(payload[1]), rows, stubs)
    by_id = {comment_id: node for comment_id, node, _ in rows}
    _expand(thread_id, roots, by_id, stubs, max_comments)
    return thread_from(posts[0], roots, sum(stub.hidden for stub in stubs))
