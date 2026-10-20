"""Hacker News via the Algolia API."""

from __future__ import annotations

import re
from typing import Any

from forumread import http
from forumread.cache import SEARCH_TTL, THREAD_TTL
from forumread.http import FetchError
from forumread.model import Comment, SearchHit, SearchResult, Thread, assign_depths
from forumread.text import clean_markdown, html_to_text

API = "https://hn.algolia.com/api/v1"
RANKING_API = "https://hacker-news.firebaseio.com/v0/item"
ITEM_URL = "https://news.ycombinator.com/item?id="


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _comments(children: list[dict[str, Any]] | None) -> list[Comment]:
    nodes = []
    for child in children or []:
        if child.get("type") != "comment":
            continue
        nodes.append(
            Comment(
                author=child.get("author") or "[deleted]",
                score=_int(child.get("points")),
                created=_int(child.get("created_at_i")),
                body=html_to_text(child.get("text")),
                children=_comments(child.get("children")),
            )
        )
    return nodes


def _ranked(item_id: Any, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Algolia lists comments oldest-first and has no comment points; the official API's `kids` are in the order the
    site ranks them. Best effort: without the ranking the Algolia order stays."""
    try:
        official = http.get_json(f"{RANKING_API}/{item_id}.json", cache_ttl=THREAD_TTL)
    except FetchError:
        return children
    kids = official.get("kids") if isinstance(official, dict) else None
    if not isinstance(kids, list):
        return children
    rank = {kid: index for index, kid in enumerate(kids)}
    return sorted(children, key=lambda child: rank.get(child.get("id"), len(rank)))


def fetch_thread(item_id: str) -> Thread:
    item = http.get_json(f"{API}/items/{item_id}", cache_ttl=THREAD_TTL)
    if isinstance(item, dict) and item.get("type") == "comment" and item.get("story_id"):
        item = http.get_json(f"{API}/items/{item['story_id']}", cache_ttl=THREAD_TTL)  # a comment link: its whole story
    if not isinstance(item, dict) or not item.get("id"):
        raise FetchError(f"Hacker News item {item_id} not found")
    body = html_to_text(item.get("text"))
    if item.get("url"):
        body = f"Link: {item['url']}\n\n{body}".strip()
    comments = _comments(_ranked(item["id"], item.get("children") or []))
    assign_depths(comments)
    return Thread(
        source="hn",
        id=str(item["id"]),
        url=f"{ITEM_URL}{item['id']}",
        title=clean_markdown(item.get("title")) or f"HN item {item['id']}",
        author=item.get("author") or "[deleted]",
        score=_int(item.get("points")),
        created=_int(item.get("created_at_i")),
        body=body,
        comments=comments,
    )


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.lower()))


def _dedupe(hits: list[SearchHit]) -> list[SearchHit]:
    """Collapse Algolia crossposts (same title, different ids): keep the highest-scored one at the first one's rank."""
    best: dict[str, SearchHit] = {}
    for hit in hits:
        key = _title_key(hit.title) or hit.url
        if key not in best or (hit.score or 0) > (best[key].score or 0):
            best[key] = hit
    return list(best.values())


def _search(query: str, limit: int, loose: bool) -> list[SearchHit]:
    params: dict[str, Any] = {"query": query, "tags": "story", "hitsPerPage": min(limit * 2, 100)}  # spare for dedupe
    if loose:
        params["removeWordsIfNoResults"] = "allOptional"
    payload = http.get_json(f"{API}/search", params, cache_ttl=SEARCH_TTL)
    hits = [
        SearchHit(
            source="hn",
            title=clean_markdown(hit.get("title")),
            url=f"{ITEM_URL}{hit['objectID']}",
            score=_int(hit.get("points")),
            num_comments=_int(hit.get("num_comments")),
            created=_int(hit.get("created_at_i")),
        )
        for hit in payload.get("hits", [])
    ]
    return _dedupe(hits)[:limit]


def search(query: str, limit: int) -> SearchResult:
    """Algolia ANDs every word. Try that first; only when nothing matches, retry dropping words and say so."""
    hits = _search(query, limit, loose=False)
    if hits:
        return SearchResult(hits, "hn.algolia.com")
    loose = _search(query, limit, loose=True)
    return SearchResult(loose, "hn.algolia.com (loose match: not all words found)" if loose else "hn.algolia.com")
