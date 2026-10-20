"""Stack Exchange network via the public API (gzip-compressed responses)."""

from __future__ import annotations

import html
import os
from typing import Any

from forumread import http
from forumread.cache import SEARCH_TTL, THREAD_TTL
from forumread.http import FetchError
from forumread.model import Comment, SearchHit, Thread
from forumread.text import html_to_text

API = "https://api.stackexchange.com/2.3"
DEFAULT_SITE = "stackoverflow"


def _items(path: str, params: dict[str, Any], ttl: float = THREAD_TTL) -> list[dict[str, Any]]:
    # Without an app key the API allows 300 requests a day per IP, shared by every machine behind that IP; a free
    # stackapps.com key (not a secret) raises it to 10,000.
    key = os.environ.get("STACKEXCHANGE_KEY")
    payload = http.get_json(f"{API}/{path}", params | {"key": key} if key else params, cache_ttl=ttl)
    if payload.get("error_message"):
        raise FetchError(f"api.stackexchange.com: {payload['error_message']}")
    return payload.get("items", [])


def _author(item: dict[str, Any]) -> str:
    return html.unescape((item.get("owner") or {}).get("display_name") or "[deleted]")


def fetch_thread(question_id: str, site: str) -> Thread:
    questions = _items(f"questions/{question_id}", {"site": site, "filter": "withbody"})
    if not questions:
        raise FetchError(f"Stack Exchange question {question_id} not found on {site}")
    question = questions[0]
    answers = _items(
        f"questions/{question_id}/answers",
        {"site": site, "filter": "withbody", "sort": "votes", "order": "desc", "pagesize": 100},
    )
    return Thread(
        source="se",
        id=str(question_id),
        url=question["link"],
        title=html.unescape(question.get("title", "")),
        author=_author(question),
        score=question.get("score"),
        created=question.get("creation_date"),
        body=html_to_text(question.get("body")),
        comments=[
            Comment(
                author=_author(answer),
                score=answer.get("score"),
                created=answer.get("creation_date"),
                body=html_to_text(answer.get("body")),
                accepted=bool(answer.get("is_accepted")),
            )
            for answer in answers
        ],
    )


def search(query: str, site: str, limit: int) -> list[SearchHit]:
    items = _items(
        "search/advanced",
        {"q": query, "site": site, "pagesize": limit, "order": "desc", "sort": "relevance"},
        SEARCH_TTL,
    )
    return [
        SearchHit(
            source="se",
            title=html.unescape(item.get("title", "")),
            url=item["link"],
            score=item.get("score"),
            num_comments=item.get("answer_count"),
            created=item.get("creation_date"),
        )
        for item in items[:limit]
    ]
