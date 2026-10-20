"""Conversions from Reddit-shaped JSON (live API and archives use the same field names) to the common model."""

from __future__ import annotations

import re
from typing import Any

from forumread.model import Comment, SearchHit, Thread, assign_depths
from forumread.text import clean_markdown


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


STOPWORDS = frozenset(
    [
        "a", "an", "and", "any", "anyone", "are", "as", "at", "be", "best", "better", "by", "can", "do", "does",
        "for", "from", "get", "good", "how", "i", "in", "is", "it", "its", "my", "of", "on", "or", "review",
        "reviews", "should", "the", "this", "to", "versus", "vs", "what", "which", "with", "worth",
    ]
)
_WORD = re.compile(r"\w+")


def terms(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def query_terms(query: str) -> set[str]:
    words = terms(query)
    return (words - STOPWORDS) or words


def match_counts(post: dict[str, Any], wanted: set[str]) -> tuple[int, int] | None:
    """(title matches, selftext matches) when the post is about the query, else None: at least half (rounded up) of
    the query's content words appear in title+selftext, and the title names one of them or the selftext names all
    (a lone passing mention in a long post is not relevance)."""
    title = terms(clean_markdown(post.get("title")))
    body = terms(clean_markdown(post.get("selftext")))
    if len(wanted & (title | body)) < -(-len(wanted) // 2):
        return None
    if not wanted & title and not wanted <= body:
        return None
    return len(wanted & title), len(wanted & body)


def comment_from(data: dict[str, Any]) -> Comment:
    return Comment(
        author=data.get("author") or "[deleted]",
        score=to_int(data.get("score")),
        created=to_int(data.get("created_utc")),
        body=clean_markdown(data.get("body")),
    )


def permalink(post: dict[str, Any]) -> str:
    return f"https://www.reddit.com{post.get('permalink') or '/comments/' + post['id'] + '/'}"


def snippet(selftext: str | None) -> str:
    text = clean_markdown(selftext)
    return "" if text in ("[removed]", "[deleted]") else text[:200]


def hits_from(posts: list[dict[str, Any]], limit: int) -> list[SearchHit]:
    return [
        SearchHit(
            source="reddit",
            title=clean_markdown(post.get("title")),
            url=permalink(post),
            score=to_int(post.get("score")),
            num_comments=to_int(post.get("num_comments")),
            created=to_int(post.get("created_utc")),
            snippet=snippet(post.get("selftext")),
        )
        for post in posts[:limit]
    ]


def thread_from(post: dict[str, Any], comments: list[Comment], unloaded: int = 0) -> Thread:
    assign_depths(comments)
    return Thread(
        source="reddit",
        id=post["id"],
        url=permalink(post),
        title=clean_markdown(post.get("title")),
        author=post.get("author") or "[deleted]",
        score=to_int(post.get("score")),
        created=to_int(post.get("created_utc")),
        body=clean_markdown(post.get("selftext")),
        comments=comments,
        unloaded=unloaded,
    )
