// Registers forum_search and forum_read: thin wrappers over the `forumread` CLI
// (https://github.com/tempoloss/forumread, installed with `uv tool install git+https://github.com/tempoloss/forumread`).
// Binary: $FORUMREAD_BIN, else `forumread` on PATH (falling back to uv's default tool bin dir).

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const TIMEOUT_MS = 120_000;
const DEADLINE_S = 100; // CLI gives up on unfinished items before TIMEOUT_MS kills it, so finished ones are still shown

function resolveBinary(): string {
  const configured = process.env.FORUMREAD_BIN;
  if (configured) return configured;
  const onPath = (process.env.PATH ?? "")
    .split(path.delimiter)
    .filter(Boolean)
    .some((dir) =>
      ["forumread", "forumread.exe", "forumread.cmd"].some((name) => existsSync(path.join(dir, name))),
    );
  if (onPath) return "forumread";
  const uvBin = path.join(os.homedir(), ".local", "bin", process.platform === "win32" ? "forumread.exe" : "forumread");
  return existsSync(uvBin) ? uvBin : "forumread";
}

function runForumread(args: string[], signal: AbortSignal | undefined): Promise<{ stdout: string; stderr: string }> {
  const { promise, resolve, reject } = Promise.withResolvers<{ stdout: string; stderr: string }>();
  const bin = resolveBinary();
  const child = spawn(bin, args, {
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  const out: Buffer[] = [];
  const err: Buffer[] = [];
  let settled = false;
  const finish = (fn: () => void) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
    fn();
  };
  const onAbort = () => {
    child.kill();
    finish(() => reject(new Error("forumread was cancelled")));
  };
  const timer = setTimeout(() => {
    child.kill();
    finish(() => reject(new Error(`forumread timed out after ${TIMEOUT_MS / 1000}s`)));
  }, TIMEOUT_MS);
  if (signal?.aborted) onAbort();
  else signal?.addEventListener("abort", onAbort, { once: true });

  child.stdout.on("data", (chunk: Buffer) => out.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => err.push(chunk));
  child.on("error", (error: NodeJS.ErrnoException) =>
    finish(() =>
      reject(
        new Error(
          error.code === "ENOENT"
            ? `cannot run '${bin}': not found. Install with: uv tool install git+https://github.com/tempoloss/forumread, or set FORUMREAD_BIN`
            : `cannot run '${bin}': ${error.message}`,
        ),
      ),
    ),
  );
  child.on("close", (code) =>
    finish(() => {
      const stdout = Buffer.concat(out).toString("utf8");
      const stderr = Buffer.concat(err).toString("utf8").trim();
      if (code !== 0) reject(new Error(stderr || `forumread exited with code ${code}`));
      else resolve({ stdout, stderr });
    }),
  );
  return promise;
}

function textResult(stdout: string, stderr: string, details: Record<string, unknown>) {
  const text = stderr ? `${stdout}\n_forumread notes: ${stderr}_\n` : stdout;
  return { content: [{ type: "text" as const, text }], details };
}

export default function forumread(pi: ExtensionAPI) {
  const z = pi.zod;

  pi.registerTool({
    name: "forum_search",
    label: "Forum Search",
    description:
      "Find forum discussions (Reddit, Hacker News, Stack Exchange) about a topic. Use it when you need community opinion, " +
      "real user experience, comparisons, recommendations or troubleshooting threads rather than official docs or news. " +
      "Returns a numbered markdown list per query (title, URL, score, comment count, date), headed by which backend served it. " +
      "Pass ALL your queries in one call (3-6 phrasings, up to 10, run concurrently) and then open the best 4-8 threads (many comments, recent for fast-moving topics) with forum_read. " +
      "Write keyword queries, not questions: exact product/model names plus 1-2 topic words ('airpods pro 3 anc'); Reddit search is keyword matching, so sentences and filler pull unrelated posts. " +
      "source=web (default) searches Reddit + HN + Stack Overflow together; source=reddit searches Reddit live (global, or inside one subreddit) with real scores and comment counts " +
      "(guest OAuth, 100 requests per 10-minute window per machine; if that is unavailable the _via line says the archives Arctic Shift / pullpush served it, with stale scores and up to a week of lag); source=hn; source=se with a Stack Exchange site. " +
      "A query with no hits yields '_No results._' (not an error): rephrase it; a '_No strong match..._' note means the archive returned only weak matches. Results are cached for 1 hour. Queries still unfinished after 100 s print as '**Timed out:** <query>'.",
    parameters: z.object({
      queries: z.array(z.string()).min(1).max(10).describe("Search queries; batch every query you want in one call"),
      source: z
        .enum(["web", "reddit", "hn", "se"])
        .optional()
        .describe("web (default): reddit+hn+stackoverflow; reddit: live Reddit search; hn: Hacker News; se: Stack Exchange"),
      subreddit: z
        .string()
        .optional()
        .describe("source=reddit only: restrict to one subreddit (e.g. headphones); omit to search all of Reddit"),
      site: z.string().optional().describe("source=se only: Stack Exchange site, default stackoverflow (e.g. superuser, askubuntu)"),
      limit: z.number().int().min(1).max(100).optional().describe("Results per query, default 10"),
    }),
    loadMode: "essential",
    approval: "read",
    async execute(_toolCallId, params, signal) {
      const args = ["search", "--deadline", String(DEADLINE_S), ...params.queries.map((query) => `--query=${query}`)];
      if (params.source) args.push("--source", params.source);
      if (params.subreddit) args.push("--subreddit", params.subreddit);
      if (params.site) args.push("--site", params.site);
      if (params.limit) args.push("--limit", String(params.limit));
      const started = Date.now();
      const { stdout, stderr } = await runForumread(args, signal);
      return textResult(stdout, stderr, { queries: params.queries.length, ms: Date.now() - started });
    },
  });

  pi.registerTool({
    name: "forum_read",
    label: "Forum Read",
    description:
      "Read forum threads in full as compact markdown (post + nested comments sorted by score): Reddit (reddit.com/r/.../comments/<id>, redd.it), " +
      "Hacker News (news.ycombinator.com/item?id=), Stack Exchange questions (stackoverflow.com/questions/<id>, superuser, askubuntu, *.stackexchange.com). " +
      "Use it for real user experience, opinions, pros/cons and troubleshooting after forum_search, or when the user gives such a URL. " +
      "Pass ALL URLs in one call (up to 20, fetched concurrently); a thread that fails shows an error line instead of failing the batch. " +
      "AutoModerator/bot and deleted comments are already hidden (the header says when everything was hidden; include_bots restores bots). " +
      "Long post bodies (4000 chars) and comments (2000) are cut with a marker; raise max_body_chars / max_comment_chars if you need the rest. " +
      "Default for a research batch: max_comments 40, depth 2, max_body_chars 1500, max_comment_chars 800 (~3-6k tokens per thread); go deeper only on the 1-2 key threads. " +
      "Reddit is read live (guest OAuth, real scores, collapsed branches expanded) and falls back to archives; a note says how many comments were not loaded. " +
      "Threads still unfinished after 100 s print as '**Timed out:** <url>' while the finished ones are shown. Responses are cached for 6 hours.",
    parameters: z.object({
      urls: z.array(z.string()).min(1).max(20).describe("Thread URLs (reddit, redd.it, Hacker News, Stack Exchange)"),
      max_comments: z.number().int().min(1).optional().describe("Max comments/answers per thread, best first (default 300)"),
      min_score: z.number().int().optional().describe("Drop comments (and their replies) scoring below this"),
      depth: z.number().int().min(1).optional().describe("Max nesting depth; 1 = top-level comments only"),
      sort: z.enum(["top", "new"]).optional().describe("top (default) or new (most recent first)"),
      max_body_chars: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe("Cut the opening post / question body at this many chars (default 4000); a marker says when it was cut"),
      max_comment_chars: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe("Cut each single comment/answer at this many chars (default 2000); a marker says when it was cut"),
      include_bots: z.boolean().optional().describe("Keep AutoModerator/bot comments (hidden by default)"),
    }),
    loadMode: "essential",
    approval: "read",
    async execute(_toolCallId, params, signal) {
      const args = ["thread", "--deadline", String(DEADLINE_S)];
      if (params.max_comments) args.push("--max-comments", String(params.max_comments));
      if (params.min_score !== undefined) args.push(`--min-score=${params.min_score}`);
      if (params.depth) args.push("--depth", String(params.depth));
      if (params.sort) args.push("--sort", params.sort);
      if (params.max_body_chars) args.push("--max-body-chars", String(params.max_body_chars));
      if (params.max_comment_chars) args.push("--max-comment-chars", String(params.max_comment_chars));
      if (params.include_bots) args.push("--include-bots");
      args.push("--", ...params.urls);
      const started = Date.now();
      const { stdout, stderr } = await runForumread(args, signal);
      return textResult(stdout, stderr, { urls: params.urls.length, ms: Date.now() - started });
    },
  });
}
