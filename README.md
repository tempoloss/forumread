# forumread

[![CI](https://github.com/tempoloss/forumread/actions/workflows/ci.yml/badge.svg)](https://github.com/tempoloss/forumread/actions/workflows/ci.yml)

**Forum opinion for AI agents.** Search and read Reddit, Hacker News and Stack Exchange threads as compact, token-budgeted markdown. Stdlib-only Python CLI, plus an omp extension that exposes it as two agent tools.

## Why

- Agents can't read Reddit: pages are blocked, JavaScript-only or behind login walls, and a raw thread dumped into context burns thousands of tokens on noise.
- `forumread` returns ranked, trimmed, cited markdown: top comments by score, nested replies, bots and deleted comments hidden, every limit stated in the output so the agent knows what was cut.
- Batch calls: many queries or URLs in one call, fetched concurrently, printed in input order. A failed item is a one-line error; the rest is unaffected.
- Cached (sqlite, 6 h threads / 1 h searches) and polite to upstreams (per-host concurrency limits, shared rate-limit state).
- Honest provenance: every search starts with a `_via ..._` line naming the backend that actually served it (live Reddit, an archive, HN, ...), so stale data is never passed off as fresh.

## Example

```
$ forumread search "uv poetry" --source reddit --limit 2
# Search: uv poetry
_via reddit (live)_

1. **uv starting to overtake Poetry in package download**
   https://www.reddit.com/r/Python/comments/1jd7nhh/uv_starting_to_overtake_poetry_in_package_download/ · reddit · score 376 · 183 comments · 2025-03-17
   Downloads chart for Wagtail by installer: [uv overtakes Poetry](https://wagtail.org/blog/uv-overtakes-poetry/). It’s the first time I pull those kinds of stats…
2. **uv after 0.5.0 - might be worth replacing Poetry/pyenv/pipx**
   https://www.reddit.com/r/Python/comments/1gqh4te/uv_after_050_might_be_worth_replacing/ · reddit · score 399 · 127 comments · 2024-11-13
   uv is rapidly maturing as an open-source tool for Python project management, reaching a full-featured capabilities with recent versions 0.4.27 and 0.5.0, makin…
```

```
$ forumread thread https://www.reddit.com/r/Python/comments/1gqh4te/ --max-comments 3 --depth 2
# uv after 0.5.0 - might be worth replacing Poetry/pyenv/pipx
https://www.reddit.com/r/Python/comments/1gqh4te/uv_after_050_might_be_worth_replacing/
Reddit r/Python · by Martynoas · score 403 · 2024-11-13 · 126 comments (3 shown)

uv is rapidly maturing as an open-source tool for Python project management, ...

## Comments
_Showing 3 of 124 comments, sorted by top (limits: max_comments=3, depth=2)._
- **chub79** (182): Nice article. Thanks.
  I wasn't aware poetry's lead author had moved on from it. ...
- **latkde** (111): I'm basically waiting for Dependabot or Renovate to fully support UV lockfiles and then my main use case for Poetry will be gone.
- **runawayasfastasucan** (80): What frustrates me most with uv is how lackluster their documentation is. ...
```

(Output trimmed here for the README; the tool prints the full text up to the limits you set.)

## Install

Python 3.10+, no dependencies.

```
uv tool install git+https://github.com/tempoloss/forumread
# or: pipx install git+https://github.com/tempoloss/forumread
forumread --version
```

One-off, without installing:

```
uvx --from git+https://github.com/tempoloss/forumread forumread search "uv vs poetry" --source reddit
```

Optional: `STACKEXCHANGE_KEY=<free stackapps.com app key>` in the environment (see limits below).

## Use with agents

`SKILL.md` is the usage playbook (how to search wide, pick threads, read cheaply, report). Give it to the agent together with the CLI.

**omp.** Copy `omp/forumread.ts` to `~/.omp/agent/extensions/` and `SKILL.md` to `~/.omp/agent/skills/forumread/SKILL.md`, then restart omp (tools load at session start). The extension registers two read-only tools that call the CLI: `forum_search` (`queries[]`, `source`, `subreddit`, `site`, `limit`) and `forum_read` (`urls[]`, `max_comments`, `min_score`, `depth`, `sort`, `max_body_chars`, `max_comment_chars`, `include_bots`). The binary is `$FORUMREAD_BIN`, else `forumread` on PATH (else `~/.local/bin`). Timeout is 120 s; the extension passes `--deadline 100`, so a batch still running at 100 s prints the finished items plus `**Timed out:** <url>` for the rest.

**Claude Code, Codex, any agent with a shell.** Install the CLI, then add `SKILL.md` as a skill (Claude Code: `~/.claude/skills/forumread/SKILL.md`) or paste it into the agent's instructions. The agent calls `forumread search` / `forumread thread` from the shell.

**Programmatic use.** `--json` prints a JSON list: one object per thread (`comments_total`, `comments`, ...) or per query (`query`, `via`, `hits`); failures are `{"target", "error"}`.

## CLI reference

```
forumread thread <url-or-id> [<url-or-id> ...] [--max-comments 300] [--min-score S] [--depth D] [--sort top|new] [--include-bots] [--max-body-chars 4000] [--max-comment-chars 2000] [--json]
forumread search "<query>" ["<query>" ...] [-q "<query>" ...] [--source web|reddit|hn|se] [--subreddit X] [--site stackoverflow] [--limit N] [--json]
# both: [--workers 8] [--no-cache] [--deadline SECONDS]
```

`thread` accepts `reddit.com/r/<sub>/comments/<id>/...` (also old./np./m.), `redd.it/<id>`, `news.ycombinator.com/item?id=<n>`, `stackoverflow.com/questions/<id>` and other Stack Exchange sites (`superuser.com`, `serverfault.com`, `askubuntu.com`, `*.stackexchange.com`). A bare Reddit id works as is; a bare numeric id needs `--source hn` or `--source se [--site <site>]`.

Thread output: a header (title, url, author, score, date, comment count), the post body, then nested comments as `- **author** (score): text`, siblings sorted by score. Deleted/removed comments and bots (AutoModerator, "I am a bot" disclaimers, `*bot` authors that say they act automatically) are hidden; `--include-bots` restores them and the header says how many were hidden. The post is cut at 4000 chars and each comment at 2000, at a paragraph/line boundary, with a truncation marker. A note says how many comments were cut by `--max-comments` / `--min-score` / `--depth`.

`search` prints a numbered list (title, url, score, comment count, date) per query under a `_via ..._` line; for `web` the line also shows each source's status (`reddit (live): 5 hits; hn: 4 hits (loose match); stackoverflow: error ...`). Default `--source web`. A query without hits is a normal result (`_No results._`, exit 0).

Batch, cache, limits:

- Several URLs / queries run concurrently (`--workers`, default 8), printed separated by `---` in input order. The exit code is non-zero only if every item failed (1 = remote errors, 2 = bad arguments); errors go to stderr as `forumread: error: ...`.
- Cache: a sqlite file (`$FORUMREAD_CACHE`, else `%LOCALAPPDATA%\forumread\cache.db` / `~/.cache/forumread/cache.db`), 6 h for threads, 1 h for searches; error and empty responses are never stored; `--no-cache` skips it. WAL mode, safe for concurrent processes.
- Per-host concurrency limits: Arctic Shift 4, pullpush 1 (>=1 s spacing), HN 8, Stack Exchange 4, DuckDuckGo 1 (2 s spacing), oauth.reddit.com 4. 429/5xx are retried with backoff, except live Reddit (the archives are its retry).
- `--deadline S` bounds a whole call: unfinished items print as `**Timed out:** <url or query>` (JSON: an `error` entry) and the finished ones are shown as usual.
- Shared state (guest token, quota counters, cooldowns) lives in a `state` table of the same sqlite file and is independent of `--no-cache`.

## Sources and limits

| Source | Backend | Notes |
| --- | --- | --- |
| Reddit | live: anonymous guest token (`oauth.reddit.com`); fallback: [Arctic Shift](https://arctic-shift.photon-reddit.com) archive, then [pullpush](https://pullpush.io) | Live is primary: real scores and comment counts, Reddit's own relevance search (global or `--subreddit`). The quota is 100 requests per 10-minute window per token, i.e. per machine; it is shared by every process via the cache DB. A search costs 1 request, a thread 1–4. When the quota is used up or live access fails, calls fall back to the archives and the `_via` line says so (archive scores are snapshots; pullpush lags about a week). |
| Hacker News | Algolia API (bodies, tree) + official Firebase API (ranking) | Comments carry no points; top-level comments follow the site's ranking. Search requires every word first and only then loosens (`loose match` in the `_via` line). Crossposts are collapsed. |
| Stack Exchange | public API | Question + answers (accepted answer marked), no comment threads. Without a key the quota is 300 requests/day per IP, shared by every machine behind it; set `STACKEXCHANGE_KEY` to a free [stackapps.com](https://stackapps.com/apps/oauth/register) app key (not a secret) for 10,000/day. |
| web | DuckDuckGo HTML, one query per domain | Optional. If DuckDuckGo serves a bot challenge (it does from some networks), it is skipped for 15 minutes and `web` uses live Reddit + Hacker News + Stack Overflow API search instead; the `_via` line says so. |

Several machines behind one IP: each machine has its own cache, guest token and Reddit quota; Stack Exchange's daily quota, the archives' rate limits and DuckDuckGo's bot check are per IP and shared.

## How Reddit access works / caveats

Live Reddit access uses an anonymous token issued to the public client id of Reddit's Android app — the same approach as [redlib](https://github.com/redlib-org/redlib). forumread is not affiliated with or endorsed by Reddit, and this may conflict with Reddit's terms of service. It is meant for personal research at human-like volume: respect the rate limits and don't hammer it.

If Reddit changes or revokes the client id, set `FORUMREAD_REDDIT_CLIENT_ID` (and optionally `FORUMREAD_REDDIT_USER_AGENT`) to a working one. When live access fails for any reason, everything falls back to public archives; thanks to [Arctic Shift](https://arctic-shift.photon-reddit.com) and [pullpush](https://pullpush.io) for running them.

## Development

```
pip install -e ".[dev]"
ruff check .
python -m pytest -q
```

Tests are offline: fixtures live in `tests/fixtures/`, the HTTP layer is monkeypatched.

## License

MIT, see [LICENSE](LICENSE).
