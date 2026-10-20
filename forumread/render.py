"""Filtering and markdown rendering of threads and search hits."""

from __future__ import annotations

import heapq
import itertools
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from forumread.model import Comment, SearchResult, Thread, assign_depths, count_comments

SOURCE_LABELS = {"reddit": "Reddit", "hn": "Hacker News", "se": "Stack Exchange"}
_DELETED_BODIES = {"", "[deleted]", "[removed]"}
_BOT_AUTHORS = {"automoderator"}  # an explicit set: names merely ending in "bot" are too often real people
_BOT_FOOTER = re.compile(r"\bI(?: am|'m) a bot\b", re.IGNORECASE)  # "I am a bot, and this action was performed automatically."
_BOT_WORDING = re.compile(r"\bautomatically\b|\bI(?: am|'m) a bot\b|this action was performed", re.IGNORECASE)


DEFAULT_MAX_BODY_CHARS = 4000
DEFAULT_MAX_COMMENT_CHARS = 2000
SNIPPET_CHARS = 160
_HEADER_MARKS = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)


@dataclass(frozen=True)
class Limits:
    max_comments: int | None = 300
    min_score: int | None = None
    depth: int | None = None  # nesting levels to keep; 1 = top-level comments only
    sort: str = "top"  # top | new
    include_bots: bool = False  # keep AutoModerator and self-declared bot comments
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS  # opening post / question body
    max_comment_chars: int = DEFAULT_MAX_COMMENT_CHARS  # each single comment / answer


@dataclass(frozen=True)
class View:
    thread: Thread  # filtered copy
    total: int  # comments before any limit, deleted leaves and bots already dropped
    limits: Limits
    bots_hidden: int = 0
    raw_total: int = 0  # every comment the source returned, before bots/deleted/limits

    @property
    def shown(self) -> int:
        return count_comments(self.thread.comments)

    def as_dict(self) -> dict:
        return {
            **asdict(self.thread),
            "comments_raw": self.raw_total,
            "comments_total": self.total,
            "comments_shown": self.shown,
            "bots_hidden": self.bots_hidden,
        }


def _is_deleted(comment: Comment) -> bool:
    return comment.body.strip().lower() in _DELETED_BODIES


def _is_bot(comment: Comment) -> bool:
    if comment.author.lower() in _BOT_AUTHORS:
        return True
    if comment.author.lower().endswith("bot") and _BOT_WORDING.search(comment.body):
        return True  # TransducerBot, RemindMeBot...; a person whose name ends in "bot" does not write like this
    lines = [line for line in comment.body.strip().splitlines() if line.strip()]
    return bool(lines) and len(lines[-1]) <= 400 and _BOT_FOOTER.search(lines[-1]) is not None


def _drop_bots(nodes: list[Comment]) -> tuple[list[Comment], int]:
    """Drop bot comments; replies to a bot move up to the bot's level. Returns (kept, number dropped)."""
    kept: list[Comment] = []
    dropped = 0
    for node in nodes:
        children, inner = _drop_bots(node.children)
        dropped += inner
        if _is_bot(node):
            dropped += 1
            kept.extend(children)
        else:
            kept.append(replace(node, children=children))
    return kept, dropped


def _drop_deleted(nodes: list[Comment]) -> list[Comment]:
    """Drop deleted/removed comments, keeping a placeholder only where replies would be orphaned."""
    kept = []
    for node in nodes:
        children = _drop_deleted(node.children)
        if children or not _is_deleted(node):
            kept.append(replace(node, children=children))
    return kept


def _filter(nodes: list[Comment], limits: Limits) -> list[Comment]:
    kept = []
    for node in nodes:
        if limits.depth is not None and node.depth >= limits.depth:
            continue
        if limits.min_score is not None and node.score is not None and node.score < limits.min_score:
            continue
        kept.append(replace(node, children=_filter(node.children, limits)))
    return kept


def _rank_for(sort: str) -> Callable[[Comment], int]:
    if sort == "new":
        return lambda c: -(c.created or 0)
    return lambda c: -(c.score or 0)  # stable sort keeps source order (HN ranking) when scores are absent


def _select(roots: list[Comment], limit: int, rank: Callable[[Comment], int]) -> list[Comment]:
    """Keep the `limit` best comments, expanding best-first so a kept comment always has its parent."""
    counter = itertools.count()
    heap = [(rank(c), next(counter), c) for c in roots]
    heapq.heapify(heap)
    chosen: set[int] = set()
    while heap and len(chosen) < limit:
        _, _, node = heapq.heappop(heap)
        chosen.add(id(node))
        for child in node.children:
            heapq.heappush(heap, (rank(child), next(counter), child))

    def build(nodes: list[Comment]) -> list[Comment]:
        picked = [n for n in nodes if id(n) in chosen]
        return [replace(n, children=build(n.children)) for n in sorted(picked, key=rank)]

    return build(roots)


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Cut `text` to at most `cap` chars at a paragraph (else line) boundary in its second half; (result, was cut)."""
    if len(text) <= cap:
        return text, False
    head = text[:cap]
    cut = head.rfind("\n\n")
    if cut < cap // 2:
        cut = head.rfind("\n")
    kept = (head[:cut] if cut >= cap // 2 else head).rstrip()
    if kept.count("```") % 2:
        kept += "\n```"  # never leave a code fence open: it would swallow the marker and what follows
    return kept, True


def _cap_comments(nodes: list[Comment], cap: int) -> list[Comment]:
    capped = []
    for node in nodes:
        body, cut = _truncate(node.body, cap)
        if cut:
            body += f"\n_[comment truncated: {len(body)} of {len(node.body)} chars; raise max_comment_chars]_"
        capped.append(replace(node, body=body, children=_cap_comments(node.children, cap)))
    return capped


def prepare(thread: Thread, limits: Limits) -> View:
    comments, bots = thread.comments, 0
    raw_total = count_comments(comments)
    if not limits.include_bots:
        comments, bots = _drop_bots(comments)
        assign_depths(comments)  # replies hoisted out of a bot comment moved up a level
    comments = _drop_deleted(comments)
    total = count_comments(comments)
    rank = _rank_for(limits.sort)
    comments = _filter(comments, limits)
    if limits.max_comments is not None:
        comments = _select(comments, limits.max_comments, rank)
    else:
        comments = _select(comments, total, rank)
    comments = _cap_comments(comments, limits.max_comment_chars)
    body, cut = _truncate(thread.body, limits.max_body_chars)
    if cut:
        body += f"\n\n_[post truncated: {len(body)} of {len(thread.body)} chars; raise max_body_chars]_"
    return View(replace(thread, body=body, comments=comments), total, limits, bots, raw_total)


def _date(timestamp: int | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _compact(text: str) -> list[str]:
    """Lines of `text` with blank lines removed outside code fences."""
    lines, in_fence = [], False
    for line in text.strip().splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if line.strip() or in_fence:
            lines.append(line.rstrip())
    return lines


def _render_comments(nodes: list[Comment], level: int, out: list[str]) -> None:
    indent = "  " * level
    for node in nodes:
        meta = ", ".join(str(p) for p in (node.score, "accepted" if node.accepted else None) if p is not None)
        head = f"{indent}- **{node.author}**" + (f" ({meta})" if meta else "") + ":"
        body = ["[deleted]"] if _is_deleted(node) else _compact(node.body)
        out.append(f"{head} {body[0]}")
        out.extend(f"{indent}  {line}" if line else "" for line in body[1:])
        _render_comments(node.children, level + 1, out)


def _limit_note(limits: Limits) -> str:
    active = []
    if limits.max_comments is not None:
        active.append(f"max_comments={limits.max_comments}")
    if limits.min_score is not None:
        active.append(f"min_score={limits.min_score}")
    if limits.depth is not None:
        active.append(f"depth={limits.depth}")
    return ", ".join(active)


def _count_label(view: View) -> str:
    """`25 comments (6 shown)`; explains an empty list that bots/deleted comments, not the thread, caused."""
    noun = "answers" if view.thread.source == "se" else "comments"
    raw = view.raw_total
    if raw == 1:
        noun = noun.removesuffix("s")
    label = f"{raw} {noun}"
    if view.shown == raw:
        return label
    if view.shown == 0 and view.total == 0:
        hidden = "deleted" if view.limits.include_bots else "bots/deleted; use include_bots"
        return f"{label} (all hidden: {hidden})"
    return f"{label} ({view.shown} shown)"


def render_thread(view: View) -> str:
    thread = view.thread
    label = SOURCE_LABELS.get(thread.source, thread.source)
    subreddit = re.search(r"/r/([^/]+)/", thread.url) if thread.source == "reddit" else None
    meta = [f"{label} r/{subreddit[1]}" if subreddit else label, f"by {thread.author}"]
    if thread.score is not None:
        meta.append(f"score {thread.score}")
    if thread.created is not None:
        meta.append(_date(thread.created))
    meta.append(_count_label(view))
    if view.bots_hidden:
        meta.append(f"{view.bots_hidden} bot comments hidden")
    out = [f"# {thread.title}", thread.url, " · ".join(meta)]
    if thread.body.strip().lower() not in _DELETED_BODIES:
        out += ["", *_compact(thread.body)]
    out += ["", f"## {'Answers' if thread.source == 'se' else 'Comments'}"]
    if view.shown < view.total:
        out.append(
            f"_Showing {view.shown} of {view.total} {'answers' if thread.source == 'se' else 'comments'}, "
            f"sorted by {view.limits.sort} (limits: {_limit_note(view.limits)})._"
        )
    if view.total == 0:
        out.append("_No comments._" if view.raw_total == 0 else "_No visible comments._")
    if thread.unloaded:
        out.append(f"_{thread.unloaded} more comments exist but were not loaded (collapsed branches the source did not expand)._")
    _render_comments(thread.comments, 0, out)
    return "\n".join(out) + "\n"


def _snippet_line(text: str) -> str:
    """One short line: markdown header marks removed, whitespace collapsed, cut at SNIPPET_CHARS."""
    line = " ".join(_HEADER_MARKS.sub("", text).split())
    return line if len(line) <= SNIPPET_CHARS else line[: SNIPPET_CHARS - 1].rstrip() + "\u2026"


def render_hits(query: str, result: SearchResult) -> str:
    out = [f"# Search: {query}", f"_via {result.via}_", ""]
    hits = result.hits
    if not hits:
        out.append(result.empty_note or "_No results._")
    for number, hit in enumerate(hits, 1):
        out.append(f"{number}. **{hit.title}**")
        meta = [hit.source or "web"]
        if hit.score is not None:
            meta.append(f"score {hit.score}")
        if hit.num_comments is not None:
            meta.append(f"{hit.num_comments} {'answers' if hit.source == 'se' else 'comments'}")
        if hit.created is not None:
            meta.append(_date(hit.created))
        out.append(f"   {hit.url} · {' · '.join(meta)}")
        snippet = _snippet_line(hit.snippet)
        if snippet:
            out.append(f"   {snippet}")
    return "\n".join(out) + "\n"
