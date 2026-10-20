"""Command line interface: `forumread thread ...` and `forumread search ...` (both take several targets/queries)."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from forumread import __version__, discover, http, render
from forumread.cache import Cache
from forumread.http import FetchError
from forumread.model import SearchResult, Thread
from forumread.sources import hn, reddit, stackexchange
from forumread.urls import SOURCES, UsageError, parse_target

MAX_SEARCH_LIMIT = 100
DEFAULT_WORKERS = 8
SEPARATOR = "\n---\n\n"


@dataclass(frozen=True)
class Failure:
    label: str  # the URL / query that failed
    message: str
    usage: bool = False  # bad input rather than a remote failure
    timed_out: bool = False  # still unfinished when the --deadline passed


def _run_all(items: list[str], work: Callable[[str], Any], workers: int, deadline: float | None = None) -> list[Any]:
    """Run `work` over `items` concurrently; results keep input order, errors become Failure values.
    With a `deadline` (seconds, for the whole batch) unfinished items become timed-out Failures; their threads are
    daemons, so they neither delay the output nor keep the process alive."""

    def guarded(item: str) -> Any:
        try:
            return work(item)
        except UsageError as exc:
            return Failure(item, str(exc), usage=True)
        except FetchError as exc:
            return Failure(item, str(exc))
        except Exception as exc:  # one malformed upstream payload must not sink the rest of the batch
            return Failure(item, f"unexpected {type(exc).__name__}: {exc}")

    if len(items) == 1 and deadline is None:
        return [guarded(items[0])]
    results: list[Any] = [None] * len(items)
    done = [threading.Event() for _ in items]
    todo: queue.SimpleQueue[int] = queue.SimpleQueue()
    for index in range(len(items)):
        todo.put(index)
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                index = todo.get_nowait()
            except queue.Empty:
                return
            results[index] = guarded(items[index])
            done[index].set()

    for _ in range(min(workers, len(items))):
        threading.Thread(target=worker, daemon=True).start()
    end = None if deadline is None else time.monotonic() + deadline
    for index, finished in enumerate(done):
        if not finished.wait(None if end is None else max(0.0, end - time.monotonic())):
            stop.set()
            break
    return [
        results[i] if done[i].is_set() else Failure(items[i], f"timed out after {deadline:g}s", timed_out=True)
        for i in range(len(items))
    ]


def _report(
    results: list[Any],
    render_one: Callable[[Any], str],
    json_one: Callable[[Any], dict],
    failed_heading: str,
    as_json: bool,
) -> tuple[str, int]:
    """Combine per-item results into (stdout, exit code). Exit is nonzero only when every item failed."""
    failures = [r for r in results if isinstance(r, Failure)]
    if failures and len(failures) == len(results):
        for failure in failures:
            prefix = f"{failure.label}: " if len(results) > 1 else ""
            print(f"forumread: error: {prefix}{failure.message}", file=sys.stderr)
        return "", 2 if all(f.usage for f in failures) else 1
    if as_json:
        rows = [{"target": r.label, "error": r.message} if isinstance(r, Failure) else json_one(r) for r in results]
        return json.dumps(rows, ensure_ascii=False) + "\n", 0

    def block(result: Any) -> str:
        if isinstance(result, Failure):
            if result.timed_out:
                return f"**Timed out:** {result.label}\n"
            return f"**Error {failed_heading} {result.label}:** {result.message}\n"
        return render_one(result)

    return SEPARATOR.join(block(r) for r in results), 0


def _fetch(target_source: str, thread_id: str, site: str | None, max_comments: int) -> Thread:
    if target_source == "reddit":
        return reddit.fetch_thread(thread_id, max_comments)
    if target_source == "hn":
        return hn.fetch_thread(thread_id)
    return stackexchange.fetch_thread(thread_id, site or stackexchange.DEFAULT_SITE)


def _cmd_thread(args: argparse.Namespace) -> tuple[str, int]:
    limits = render.Limits(
        max_comments=args.max_comments,
        min_score=args.min_score,
        depth=args.depth,
        sort=args.sort,
        include_bots=args.include_bots,
        max_body_chars=args.max_body_chars,
        max_comment_chars=args.max_comment_chars,
    )

    def read(raw: str) -> render.View:
        target = parse_target(raw, args.source, args.site)
        return render.prepare(_fetch(target.source, target.id, target.site, args.max_comments), limits)

    results = _run_all(list(dict.fromkeys(args.targets)), read, args.workers, args.deadline)
    return _report(results, render.render_thread, lambda view: view.as_dict(), "reading", args.json)


def _search_one(query: str, args: argparse.Namespace) -> SearchResult:
    """Zero hits is a normal result (rendered as `_No results._`), not an error."""
    if not query.strip():
        raise UsageError("empty search query")
    if args.source == "reddit":
        return reddit.search(query, args.subreddit, args.limit)
    if args.source == "hn":
        return hn.search(query, args.limit)
    if args.source == "se":
        site = args.site or stackexchange.DEFAULT_SITE
        return SearchResult(stackexchange.search(query, site, args.limit), f"api.stackexchange.com ({site})")
    return discover.search(query, args.limit)


@dataclass(frozen=True)
class _Searched:
    label: str
    result: SearchResult


def _cmd_search(args: argparse.Namespace) -> tuple[str, int]:
    queries = list(dict.fromkeys([*args.queries, *args.query]))
    if not queries:
        raise UsageError("no search query given")
    if args.subreddit and args.source != "reddit":
        raise UsageError("--subreddit only applies to --source reddit")

    def run(query: str) -> _Searched:
        return _Searched(query, _search_one(query, args))

    results = _run_all(queries, run, args.workers, args.deadline)
    return _report(
        results,
        lambda r: render.render_hits(r.label, r.result),
        lambda r: {
            "query": r.label,
            "via": r.result.via,
            "hits": [asdict(hit) for hit in r.result.hits],
            **({"note": r.result.empty_note} if r.result.empty_note else {}),
        },
        "searching",
        args.json,
    )


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _limit(text: str) -> int:
    value = _positive(text)
    if value > MAX_SEARCH_LIMIT:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_SEARCH_LIMIT}")
    return value


def _seconds(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workers", type=_positive, default=DEFAULT_WORKERS, metavar="N", help="concurrent fetches (default 8)")
    parser.add_argument("--no-cache", action="store_true", help="skip the on-disk response cache (read and write)")
    parser.add_argument("--json", action="store_true", help="print a JSON list of objects instead of markdown")
    parser.add_argument(
        "--deadline", type=_seconds, metavar="S",
        help="overall time budget in seconds: items still unfinished then print as '**Timed out:** <target>', "
        "finished ones as usual (default: no deadline)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forumread", description="Read forum threads (Reddit, Hacker News, Stack Exchange) as compact markdown."
    )
    parser.add_argument("--version", action="version", version=f"forumread {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    thread = commands.add_parser("thread", help="print threads with their comments as markdown (several at once)")
    thread.add_argument(
        "targets", nargs="+", metavar="target", help="thread URL (reddit, redd.it, news.ycombinator.com, Stack Exchange) or bare id"
    )
    thread.add_argument("--source", choices=SOURCES, help="only needed for a bare numeric id")
    thread.add_argument("--site", help="Stack Exchange site for a bare id (default: stackoverflow)")
    thread.add_argument("--max-comments", type=_positive, default=300, metavar="N", help="default 300")
    thread.add_argument("--min-score", type=int, metavar="S", help="drop comments (and their replies) scoring below S")
    thread.add_argument("--depth", type=_positive, metavar="D", help="max nesting levels (1 = top-level only)")
    thread.add_argument("--sort", choices=("top", "new"), default="top")
    thread.add_argument("--include-bots", action="store_true", help="keep AutoModerator and self-declared bot comments")
    thread.add_argument(
        "--max-body-chars", type=_positive, default=render.DEFAULT_MAX_BODY_CHARS, metavar="N",
        help=f"cut the opening post / question body at N chars (default {render.DEFAULT_MAX_BODY_CHARS})",
    )
    thread.add_argument(
        "--max-comment-chars", type=_positive, default=render.DEFAULT_MAX_COMMENT_CHARS, metavar="N",
        help=f"cut each comment / answer at N chars (default {render.DEFAULT_MAX_COMMENT_CHARS})",
    )
    _add_common(thread)
    thread.set_defaults(run=_cmd_thread)

    search = commands.add_parser("search", help="list threads matching queries (several at once)")
    search.add_argument("queries", nargs="*", metavar="query", help="search query (repeat for several)")
    search.add_argument("-q", "--query", action="append", default=[], metavar="Q", help="additional query (repeatable)")
    search.add_argument("--source", choices=("reddit", "hn", "se", "web"), default="web", help="default: web")
    search.add_argument("--subreddit", help="restrict --source reddit to one subreddit (recommended: fresh results)")
    search.add_argument("--site", help="Stack Exchange site for --source se (default: stackoverflow)")
    search.add_argument("--limit", type=_limit, default=10, metavar="N", help="default 10")
    _add_common(search)
    search.set_defaults(run=_cmd_search)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    http.set_cache(None if args.no_cache else Cache())
    try:
        output, code = args.run(args)
    except UsageError as exc:
        print(f"forumread: error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
