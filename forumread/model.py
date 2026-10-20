"""Common data model shared by all sources and the renderer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Comment:
    author: str
    score: int | None
    created: int | None  # unix seconds, UTC
    body: str
    depth: int = 0  # 0 = top level
    children: list[Comment] = field(default_factory=list)
    accepted: bool = False  # Stack Exchange accepted answer


@dataclass
class Thread:
    source: str  # reddit | hn | se
    id: str
    url: str
    title: str
    author: str
    score: int | None
    created: int | None
    body: str
    comments: list[Comment] = field(default_factory=list)
    unloaded: int = 0  # comments the source knows exist but did not deliver ("load more" stubs)


@dataclass
class SearchHit:
    source: str
    title: str
    url: str
    score: int | None = None
    num_comments: int | None = None
    created: int | None = None
    snippet: str = ""


def assign_depths(comments: list[Comment], depth: int = 0) -> None:
    for comment in comments:
        comment.depth = depth
        assign_depths(comment.children, depth + 1)


def count_comments(comments: list[Comment]) -> int:
    return sum(1 + count_comments(c.children) for c in comments)


@dataclass
class SearchResult:
    hits: list[SearchHit]
    via: str  # which backend(s) served the hits, with per-source status
    empty_note: str = ""  # shown instead of "No results" when zero hits is not the whole story
