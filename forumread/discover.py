"""Thread discovery through DuckDuckGo's HTML endpoint (finds Reddit/HN/Stack Overflow URLs)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

from forumread import http, state
from forumread.http import FetchError
from forumread.model import SearchHit, SearchResult
from forumread.sources import hn, reddit, stackexchange
from forumread.text import html_to_text
from forumread.urls import UsageError, parse_target

ENDPOINT = "https://html.duckduckgo.com/html/"
DOMAINS = ("reddit.com", "news.ycombinator.com", "stackoverflow.com")
LABELS = ("reddit", "hn", "stackoverflow")
# DuckDuckGo answers a bot challenge (HTTP 202, no results) to non-browser User-Agents.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_RESULT = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'|<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)


def _target_url(href: str) -> str:
    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href
    redirect = parse_qs(urlsplit(href).query).get("uddg")
    return redirect[0] if redirect else href


def parse_results(markup: str) -> list[SearchHit]:
    hits: list[SearchHit | None] = []
    for match in _RESULT.finditer(markup):
        if match["href"]:
            hits.append(_hit(_target_url(match["href"]), match["title"]))
        elif hits and hits[-1] is not None and not hits[-1].snippet:
            hits[-1].snippet = html_to_text(match["snippet"])
    return [hit for hit in hits if hit is not None]


def _hit(url: str, title: str) -> SearchHit | None:
    try:
        target = parse_target(url)
    except UsageError:
        return None  # not a thread page (subreddit front page, user profile, ...)
    return SearchHit(source=target.source, title=html_to_text(title), url=url)


def _search_domain(query: str, domain: str) -> list[SearchHit]:
    markup = http.get_text(ENDPOINT, {"q": f"{query} site:{domain}"}, headers={"User-Agent": BROWSER_UA})
    if 'class="result__a"' not in markup and "No results" not in markup:
        raise FetchError("duckduckgo.com: no result list in response (bot challenge or layout change)")
    return parse_results(markup)


def _interleave(groups: list[list[SearchHit]], limit: int) -> list[SearchHit]:
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for rank in range(max((len(hits) for hits in groups), default=0)):  # round-robin by rank
        for hits in groups:
            if rank < len(hits) and hits[rank].url not in seen:
                seen.add(hits[rank].url)
                merged.append(hits[rank])
    return merged[:limit]


def _hits_status(hits: list[SearchHit]) -> str:
    return f"{len(hits)} hit{'' if len(hits) == 1 else 's'}"


DDG_BLOCKED_KEY = "ddg:blocked"
DDG_BLOCKED_TTL = 15 * 60.0  # DuckDuckGo's bot challenge is remembered across processes for this long


def _search_ddg(query: str) -> tuple[list[list[SearchHit]], list[str]]:
    """One DuckDuckGo query per domain. Fails if the first domain fails; later failures keep earlier results."""
    groups: list[list[SearchHit]] = []
    statuses: list[str] = []
    for domain, label in zip(DOMAINS, LABELS):
        try:
            hits = _search_domain(query, domain)
        except FetchError as exc:
            if not groups:
                raise
            statuses.append(f"{label}: error {exc}")
            break  # a challenge or rate limit applies to the next query too; keep what we have
        groups.append(hits)
        statuses.append(f"{label}: {_hits_status(hits)}")
    return groups, statuses


def _search_apis(query: str, limit: int) -> tuple[list[list[SearchHit]], list[str]]:
    """Fallback: the archive/forum search APIs, which are not subject to DuckDuckGo's bot challenge."""
    sources = (
        ("reddit (pullpush, lags ~1 week)", lambda: reddit.search(query, None, limit)),
        ("hn", lambda: hn.search(query, limit)),
        ("stackoverflow", lambda: SearchResult(stackexchange.search(query, stackexchange.DEFAULT_SITE, limit), "")),
    )
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = [pool.submit(run) for _, run in sources]
    groups: list[list[SearchHit]] = []
    statuses: list[str] = []
    failed = 0
    for (name, _), future in zip(sources, futures):
        try:
            result = future.result()
        except FetchError as exc:
            failed += 1
            statuses.append(f"{name}: error {exc}")
            continue
        groups.append(result.hits)
        loose = " (loose match)" if "loose match" in result.via else ""
        label = reddit.LIVE_VIA if result.via.startswith(reddit.LIVE_VIA) else name
        statuses.append(f"{label}: {_hits_status(result.hits)}{loose}")
    if failed == len(sources):
        raise FetchError(f"web search failed (duckduckgo unavailable; {'; '.join(statuses)})")
    return groups, statuses


def search(query: str, limit: int) -> SearchResult:
    shared = state.shared()
    blocked = shared.get(DDG_BLOCKED_KEY)
    if blocked is None:
        try:
            groups, statuses = _search_ddg(query)
            return SearchResult(_interleave(groups, limit), f"duckduckgo ({'; '.join(statuses)})")
        except FetchError as exc:
            blocked = str(exc)
            shared.set(DDG_BLOCKED_KEY, blocked, DDG_BLOCKED_TTL)
    groups, statuses = _search_apis(query, limit)
    via = f"API fallback, duckduckgo unavailable ({blocked}); " + "; ".join(statuses)
    return SearchResult(_interleave(groups, limit), via)
