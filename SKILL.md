---
name: forumread
description: Use when researching what people actually think about a product, tool, library or technique, or when troubleshooting — read Reddit, Hacker News and Stack Exchange discussions as compact markdown.
---

# forumread

Read forum opinion without a browser. Reddit is read live via an anonymous guest token with the public archives as fallback. Not installed (`forumread --version` fails)? Follow the Install section of `README.md` at https://github.com/tempoloss/forumread (`uv tool install git+https://github.com/tempoloss/forumread`).

## In omp: use the tools

`forum_search` and `forum_read` are top-level omp tools (extension `~/.omp/agent/extensions/forumread.ts`). Prefer them to the CLI.

- Use for: community opinion, real user experience, comparisons/recommendations, troubleshooting threads — not for official docs or news.
- Failed threads show an inline error line; the rest of the batch is unaffected. A query with no hits prints `_No results._` (not a failure): rephrase it. `include_bots: true` restores bot comments.

## Optimal use (one research question)

1. **Search once, wide.** One `forum_search` call with 3–6 queries. Write queries as keywords, not questions: product/model names exactly as people write them plus 1–2 topic words (`airpods pro 3 anc`, `uv poetry migration`). Reddit's search is keyword matching, not semantic: full sentences and filler (`is it worth it in 2026`) pull unrelated posts. "a vs b" is fine. Choose `source` by question:
   - opinions/products → `reddit` (global); add a second query with `subreddit` when you know the community (`headphones`, `LocalLLaMA`, `selfhosted`);
   - dev tools, startups, AI → also `hn`;
   - an error message or "how do I" → `se` (`site`: superuser, askubuntu, serverfault, ...), the error text as the query;
   - unsure → `web` (Reddit + HN + Stack Overflow together).
2. **Pick 4–8 threads** from the lists: many comments (20+) beat high score with few comments; prefer recent dates for fast-moving topics (models, software versions); skip weekly megathreads and link-only posts unless nothing else exists. Different subreddits/sources = different crowds: take some from each.
3. **Read them in one `forum_read` call** with small output: `max_comments: 40`, `depth: 2`, `max_body_chars: 1500`, `max_comment_chars: 800`. That is roughly 3–6k tokens per thread. Top comments by score carry most of the signal. Go deeper (`max_comments: 150`, higher char limits) only on the 1–2 threads that matter most.
4. **Second round only for gaps**: names, versions or complaints that came up in the threads you have not covered → one more `forum_search` with those exact terms → read the 2–4 best.
5. **Report**: what most commenters agree on, where they split, concrete names/versions, dates. Cite thread URLs with score and comment count, and say how many threads/comments the claim rests on. Forum opinion is anecdotes, not measurements: say so when it matters.

**Budget.** Live Reddit allows 100 requests per 10-minute window per token, pool of up to 10 tokens per machine (~1000), shared by every agent on it. A search costs 1 request (sometimes 2), a thread 1–4, and anything already fetched is free for hours (searches 1 h, threads 6 h). The flow above uses ~10–40. Don't loop re-searching near-identical phrasings; never read 20 threads at full size. If the `_via` line shows `arctic-shift` / `pullpush` instead of `reddit (live)`, the quota was used up or live Reddit failed: scores are stale and pullpush lags ~1 week.

## CLI (outside omp)

```
forumread search "<q1>" "<q2>" --source web --limit 10       # web | reddit --subreddit X | hn | se --site superuser
forumread thread <url1> <url2> ... --max-comments 60 --min-score 2 --depth 2 --sort top|new
```

Add `--include-bots` to keep AutoModerator/bot comments, `--no-cache` to bypass the cache, `--json` for a JSON list.

## Reading the results

- The `_via ..._` line under each search title says which backend served it and, for `web`, each source's hit count or error. If DuckDuckGo is blocked, results come from live reddit + HN + Stack Overflow APIs; a source showing `0 hits` or `error` contributed nothing.
- Summarize across threads: where people agree, where they split, concrete product/version names, dates. Cite the thread URL; weigh by score (current for live Reddit, snapshots from archives and HN).
- Hacker News comments have no scores; Stack Exchange shows answers (accepted one marked), not comment threads.
- Errors print `forumread: error: ...` (CLI) / become a tool error (omp). Retry once; for Reddit failures try another thread or `source: web`.
- Reddit is read **live** through a guest OAuth token (real scores/comment counts, Reddit's own relevance search, global or per `subreddit`). The quota is 100 requests per 10-minute window per token (resets on the clock: :00, :10, ...); a pool of up to 10 tokens per machine (~1000 requests per window, minted lazily) is shared by all agents on it; if live is unavailable (quota used up, token refused — Reddit could revoke the public client id) it falls back automatically to the Arctic Shift / pullpush archives and the `_via` line says so (archive scores are stale snapshots, pullpush lags ~1 week). `_No strong match in pullpush ..._` means the archive had only weak matches: use fewer words or `source: hn`/`se`.
- A thread header note `_N more comments exist but were not loaded_` means collapsed Reddit branches were not expanded (expansion is capped at 3 extra calls). A batch unfinished after 100 s prints `**Timed out:** <url>` for the missing items and the finished ones normally: re-request just those.
