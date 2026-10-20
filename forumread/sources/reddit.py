"""Reddit: live through a guest OAuth token (reddit_oauth), with public archives as the fallback.

Search: guest OAuth, then Arctic Shift (fresh, needs a subreddit) or pullpush (global, lags about a week).
Threads: guest OAuth, then Arctic Shift, then pullpush. Scores from the archives are snapshots (0/1 for ~36 h).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from forumread import http, state
from forumread.cache import SEARCH_TTL, THREAD_TTL
from forumread.http import FetchError
from forumread.model import Comment, SearchResult, Thread, count_comments
from forumread.sources import reddit_oauth
from forumread.sources.reddit_model import comment_from, hits_from, match_counts, query_terms, thread_from, to_int

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
PULLPUSH = "https://api.pullpush.io/reddit/search"
PAGE_SIZE = 100
MAX_PAGES = 50
TREE_FALLBACK_PAGES = 5  # flat pagination used when the Arctic tree is cut short by "more" stubs
ATTEMPTS = 3  # tries per archive request: the one retry layer (http-level retries are off for these calls)
SEARCH_ATTEMPTS = 2  # Arctic Shift's text search often times out (HTTP 422); two quick tries, then the fallbacks
SEARCH_PAUSE = 0.5
RETRY_STATUSES = http.RETRY_STATUSES | {422}  # Arctic Shift reports its transient "Timeout. Maybe slow down" as 422
NEGATIVE_TTL = 300.0  # an Arctic request that kept failing with 422 is not repeated for this long
ARCTIC_COOLDOWN = 30.0  # seconds search skips Arctic Shift after it failed persistently
ARCTIC_DOWN_KEY = "arctic:search-down"
PULLPUSH_SIZE = 100  # posts fetched per pullpush search; the subreddit filter and rerank work on these
LIVE_VIA = "reddit (live)"
NO_STRONG_MATCH = (
    f"_No strong match in pullpush global top-{PULLPUSH_SIZE} (archive, lags ~1 week); try fewer words or source=hn/se._"
)


def _api(
    base: str,
    path: str,
    params: dict[str, Any],
    ttl: float = THREAD_TTL,
    attempts: int = ATTEMPTS,
    pause: float = 1.0,
) -> list[Any]:
    """GET `base/path` and return payload["data"]; archive errors come back as 200 + {"error": ...}.
    Retries transient failures here (`attempts` HTTP requests at most); a 422 that persisted is remembered for NEGATIVE_TTL."""
    host = base.split("/")[2]
    marker = f"422:{http.with_params(f'{base}/{path}', params)}"
    if base == ARCTIC and http.cached(marker, NEGATIVE_TTL) is not None:
        raise FetchError(f"{host}: HTTP 422 (failed moments ago)", 422)
    reason, status = "empty response", None
    for attempt in range(attempts):
        try:
            payload = http.get_json(f"{base}/{path}", params, cache_ttl=ttl, retries=0)
        except FetchError as exc:
            if exc.status is not None and exc.status not in RETRY_STATUSES:
                raise
            reason, status, payload = str(exc).partition(": ")[2], exc.status, None
        if isinstance(payload, dict) and payload.get("data") is not None and not payload.get("error"):
            return payload["data"]
        if isinstance(payload, dict) and payload.get("error"):
            reason = str(payload["error"])
        if attempt < attempts - 1:
            time.sleep(min(pause * (attempt + 1), 4.0))
    if base == ARCTIC and (status == 422 or "slow down" in reason.lower()):
        http.remember(marker, "1")
    raise FetchError(f"{host}: {reason}", status)


def _tree_nodes(children: Any) -> tuple[list[Comment], int]:
    """Convert one Arctic Shift tree level. Returns (comments, number of comments hidden behind "more" stubs)."""
    if isinstance(children, dict):  # reddit-style Listing wrapper
        children = children.get("data", {}).get("children", [])
    if not isinstance(children, list):  # "" when a comment has no replies
        return [], 0
    nodes: list[Comment] = []
    missing = 0
    for child in children:
        if not isinstance(child, dict):
            continue
        data = child.get("data") or {}
        if child.get("kind") == "more":
            missing += to_int(data.get("count")) or len(data.get("children") or [])
        elif child.get("kind") == "t1":
            comment = comment_from(data)
            comment.children, inner = _tree_nodes(data.get("replies"))
            missing += inner
            nodes.append(comment)
    return nodes, missing


def rebuild_tree(flat: list[dict[str, Any]]) -> list[Comment]:
    """Rebuild a comment tree from flat records using parent_id. Orphans become top level."""
    by_id = {record["id"]: comment_from(record) for record in flat}
    roots: list[Comment] = []
    for record in flat:
        parent_id = str(record.get("parent_id") or "")
        parent = by_id.get(parent_id.removeprefix("t1_")) if parent_id.startswith("t1_") else None
        (parent.children if parent is not None else roots).append(by_id[record["id"]])
    return roots


def _flat_comments(
    base: str, path: str, link_id: str, size_param: str, max_pages: int = MAX_PAGES
) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    after: int | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"link_id": link_id, size_param: PAGE_SIZE, "sort": "asc"}
        if after is not None:
            params["after"] = after
        page = _api(base, path, params)
        fresh = [c for c in page if c.get("id") not in seen]
        for record in fresh:
            seen[record["id"]] = record
        if not fresh or len(page) < PAGE_SIZE:
            break
        # `after` is exclusive; step back one second so same-second siblings are not lost (deduped above).
        after = max(to_int(c.get("created_utc")) or 0 for c in page) - 1
    return list(seen.values())


def _arctic_tree(thread_id: str, max_comments: int | None) -> tuple[list[Comment], int]:
    """The Arctic Shift tree. When "more" stubs cut it short of `max_comments`, flat pagination (a few pages) is tried
    instead of silently returning the truncated tree; if that is no better the tree is kept and the gap is reported."""
    comments, missing = _tree_nodes(_api(ARCTIC, "comments/tree", {"link_id": thread_id, "limit": 9999}))
    if comments and missing and (max_comments is None or count_comments(comments) < max_comments):
        try:
            flat = rebuild_tree(_flat_comments(ARCTIC, "comments/search", thread_id, "limit", TREE_FALLBACK_PAGES))
        except FetchError:
            return comments, missing
        if count_comments(flat) > count_comments(comments):
            return flat, 0
    return comments, missing


def _comment_strategies(
    thread_id: str, max_comments: int | None
) -> Iterator[tuple[str, Callable[[], tuple[list[Comment], int]]]]:
    yield "arctic-shift tree", lambda: _arctic_tree(thread_id, max_comments)
    yield "arctic-shift flat", lambda: (
        rebuild_tree(_flat_comments(ARCTIC, "comments/search", thread_id, "limit")),
        0,
    )
    yield "pullpush", lambda: (rebuild_tree(_flat_comments(PULLPUSH, "comment/", thread_id, "size")), 0)


def _fetch_post(thread_id: str, errors: list[str]) -> dict[str, Any]:
    for name, base, path, params in (
        ("arctic-shift", ARCTIC, "posts/ids", {"ids": thread_id}),
        ("pullpush", PULLPUSH, "submission/", {"ids": thread_id}),
    ):
        try:
            posts = _api(base, path, params)
        except FetchError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if posts:
            return posts[0]
        errors.append(f"{name}: not found")
    raise FetchError(f"reddit thread {thread_id} unavailable ({'; '.join(errors)})")


def fetch_thread(thread_id: str, max_comments: int | None = None) -> Thread:
    """`max_comments` only steers how hard the live and tree backends try to load collapsed comment branches."""
    try:
        return reddit_oauth.thread(thread_id, max_comments)
    except FetchError as exc:
        errors = [f"reddit live: {exc}"]
    post = _fetch_post(thread_id, errors)
    expects_comments = bool(to_int(post.get("num_comments")))
    comments: list[Comment] = []
    missing = 0
    answered = False
    for name, strategy in _comment_strategies(thread_id, max_comments):
        try:
            comments, missing = strategy()
        except FetchError as exc:
            errors.append(f"{name}: {exc}")
            continue
        answered = True
        if comments or not expects_comments:
            break
    if not answered:
        raise FetchError(f"reddit comments for {thread_id} unavailable ({'; '.join(errors)})")
    return thread_from(post, comments, missing)


_arctic_search_lock = threading.Lock()


def _rerank(posts: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    """Keep posts that are about the query (`match_counts`), ordered by title matches (weight 3) plus selftext matches
    (weight 1), then score. pullpush's own ordering is by recency or raw score, so weak matches are dropped rather
    than shown."""
    wanted = query_terms(query)
    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, post in enumerate(posts):
        counts = match_counts(post, wanted)
        if counts is None:
            continue
        weight = 3 * counts[0] + counts[1]
        ranked.append((-weight, -(to_int(post.get("score")) or 0), index, post))
    ranked.sort(key=lambda row: row[:3])
    return [row[3] for row in ranked[:limit]]


def _failure_reason(exc: FetchError) -> str:
    return "rate-limited" if exc.status == 422 or "slow down" in str(exc).lower() else "unavailable"


def _pullpush_search(query: str, subreddit: str | None, limit: int, via: str) -> SearchResult:
    """Global pullpush search. A `subreddit=` parameter is not sent: pullpush answers every request that has one with
    HTTP 429, so the subreddit is filtered client-side from the global top PULLPUSH_SIZE instead. Hits dropped by the
    filter/rerank are explained in `empty_note` rather than reported as a bare "No results"."""
    params: dict[str, Any] = {"q": query, "size": PULLPUSH_SIZE, "sort_type": "score", "sort": "desc"}
    posts = _api(PULLPUSH, "submission/", params, SEARCH_TTL)
    fetched = len(posts)
    if subreddit:
        posts = [p for p in posts if str(p.get("subreddit", "")).lower() == subreddit.lower()]
    ranked = _rerank(posts, query, limit)
    return SearchResult(hits_from(ranked, limit), via, NO_STRONG_MATCH if fetched and not ranked else "")


def search(query: str, subreddit: str | None, limit: int) -> SearchResult:
    """Live guest-OAuth search; if it is unavailable, Arctic Shift (fresh, subreddit only) or pullpush (global, lags ~1 week)."""
    subreddit = subreddit.removeprefix("r/") if subreddit else None
    try:
        return SearchResult(reddit_oauth.search(query, subreddit, limit), LIVE_VIA)
    except FetchError as exc:
        live = f"reddit live: {exc}"
    if not subreddit:
        try:
            return _pullpush_search(query, None, limit, "pullpush (global search, lags ~1 week)")
        except FetchError as exc:
            raise FetchError(f"{live}; pullpush: {exc}") from exc
    shared = state.shared()
    with _arctic_search_lock:  # one at a time: parallel queries are what trips the rate limit, and waiting ones then see the cooldown
        down = shared.get(ARCTIC_DOWN_KEY)
        if down is not None:
            reason, arctic_error = down, None
        else:
            params = {"subreddit": subreddit, "query": query, "limit": limit, "sort": "desc"}
            try:
                posts = _api(ARCTIC, "posts/search", params, SEARCH_TTL, SEARCH_ATTEMPTS, SEARCH_PAUSE)
            except FetchError as exc:
                reason, arctic_error = _failure_reason(exc), exc
                shared.set(ARCTIC_DOWN_KEY, reason, ARCTIC_COOLDOWN)
            else:
                return SearchResult(hits_from(posts, limit), f"arctic-shift (r/{subreddit}, fresh)")
    try:
        return _pullpush_search(query, subreddit, limit, f"pullpush (arctic-shift {reason}), lags ~1 week")
    except FetchError as exc:
        if arctic_error is None:
            raise FetchError(f"{live}; arctic-shift {reason} (cooling down); pullpush: {exc}") from exc
        raise FetchError(f"{live}; arctic-shift: {arctic_error}; pullpush: {exc}") from exc
