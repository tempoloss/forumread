import itertools
import json
import time
from base64 import b64encode
import urllib.error
import urllib.parse

import pytest

from forumread import discover, http, state
from forumread.cache import Cache
from forumread.http import FetchError
from forumread.model import count_comments
from forumread.sources import reddit, reddit_oauth

ARCTIC_422 = FetchError("arctic-shift.photon-reddit.com: HTTP 422", 422)


def listing(children):
    return {"kind": "Listing", "data": {"children": children}}


def t3(post_id, title="A post", **extra):
    return {"kind": "t3", "data": {
        "id": post_id, "title": title, "permalink": f"/r/headphones/comments/{post_id}/x/", "score": 42,
        "num_comments": 7, "created_utc": 1700000000, "selftext": "", "subreddit": "headphones", **extra}}


def t1(comment_id, body="text", parent="t3_abc", replies=""):
    return {"kind": "t1", "data": {
        "id": comment_id, "author": "u", "body": body, "score": 3, "created_utc": 1700000001,
        "parent_id": parent, "replies": replies}}


def more(ids, count=None, parent="t3_abc"):
    return {"kind": "more", "data": {"children": ids, "count": len(ids) if count is None else count, "parent_id": parent}}


class FakeReddit:
    """Stands in for reddit_oauth._send: a token endpoint, routed GETs, and Reddit's quota headers."""

    def __init__(self, monkeypatch):
        self.token_requests = []
        self.gets = []  # (url, headers)
        self.routes = {}  # url substring -> payload, or callable(url) -> payload
        self.token_status = 200
        self.unauthorized = 0  # answer this many GETs with 401
        self.remaining = 100
        self._tokens = itertools.count(1)
        monkeypatch.setattr(reddit_oauth, "_send", self.send)

    def send(self, method, url, headers, body=None):
        if method == "POST":
            self.token_requests.append((headers, body))
            if self.token_status != 200:
                return self.token_status, {}, b"{}"
            n = next(self._tokens)
            return 200, {"x-reddit-loid": f"loid{n}"}, json.dumps({"access_token": f"tok{n}", "expires_in": 86399}).encode()
        self.gets.append((url, headers))
        if self.unauthorized > 0:
            self.unauthorized -= 1
            return 401, {}, b"{}"
        self.remaining -= 1
        quota = {"x-ratelimit-remaining": str(float(self.remaining)), "x-ratelimit-reset": "300"}
        for needle, payload in self.routes.items():
            if needle in url:
                result = payload(url) if callable(payload) else payload
                if isinstance(result, int):
                    return result, quota, b"{}"
                return 200, quota, json.dumps(result).encode()
        raise AssertionError(f"unrouted oauth request: {url}")


@pytest.fixture
def live(monkeypatch):
    return FakeReddit(monkeypatch)


def search_payload(*posts):
    return listing(list(posts))


# --- token and quota -------------------------------------------------------------------------------------------


def test_token_is_fetched_once_and_reused_by_a_second_process(live):
    live.routes["/search"] = search_payload(t3("a1"))
    reddit_oauth.search("one", None, 3)
    state.reset()  # a new process: no memory, same database
    reddit_oauth.search("two", "headphones", 3)

    assert len(live.token_requests) == 1
    assert [h["Authorization"] for _, h in live.gets] == ["Bearer tok1", "Bearer tok1"]
    assert live.gets[0][1]["x-reddit-loid"] == "loid1"
    headers, body = live.token_requests[0]
    assert headers["Authorization"].startswith("Basic ") and json.loads(body) == {"scopes": ["*", "email", "pii"]}


def test_client_id_and_user_agent_come_from_the_environment(live, monkeypatch):
    live.routes["/search"] = search_payload(t3("a1"))
    monkeypatch.setenv("FORUMREAD_REDDIT_CLIENT_ID", "customid")
    monkeypatch.setenv("FORUMREAD_REDDIT_USER_AGENT", "custom-agent/1")
    reddit_oauth.search("q", None, 1)

    headers, _ = live.token_requests[0]
    assert headers["Authorization"] == "Basic " + b64encode(b"customid:").decode()
    assert headers["User-Agent"] == "custom-agent/1"
    assert live.gets[0][1]["User-Agent"] == "custom-agent/1"


def test_client_id_and_user_agent_default_when_unset(live, monkeypatch):
    live.routes["/search"] = search_payload(t3("a1"))
    monkeypatch.delenv("FORUMREAD_REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("FORUMREAD_REDDIT_USER_AGENT", raising=False)
    reddit_oauth.search("q", None, 1)

    headers, _ = live.token_requests[0]
    assert headers["Authorization"] == "Basic " + b64encode(f"{reddit_oauth.DEFAULT_CLIENT_ID}:".encode()).decode()
    assert headers["User-Agent"] == reddit_oauth.DEFAULT_USER_AGENT


def test_token_expires_margin_before_the_server_says(live, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(state, "_clock", lambda: now[0])
    live.routes["/search"] = search_payload(t3("a1"))
    reddit_oauth.search("q", None, 1)
    now[0] += 86399 - 301  # still valid for us
    reddit_oauth.search("q", None, 1)
    assert len(live.token_requests) == 1
    now[0] += 2  # inside the 300 s safety margin: refetch
    reddit_oauth.search("q", None, 1)
    assert len(live.token_requests) == 2


def test_401_refetches_the_token_once_and_retries(live):
    live.routes["/search"] = search_payload(t3("a1"))
    reddit_oauth.search("warm", None, 1)
    live.unauthorized = 1
    hits = reddit_oauth.search("again", None, 1)

    assert len(hits) == 1
    assert len(live.token_requests) == 2
    assert [h["Authorization"] for _, h in live.gets][-2:] == ["Bearer tok1", "Bearer tok2"]


def test_persistent_401_gives_up_and_starts_a_cooldown(live):
    live.routes["/search"] = search_payload(t3("a1"))
    live.unauthorized = 99
    with pytest.raises(FetchError, match="HTTP 401"):
        reddit_oauth.search("q", None, 1)
    assert len(live.gets) == 2 and len(live.token_requests) == 2  # one refetch, one retry, no more

    state.reset()
    with pytest.raises(FetchError, match="cooling down"):
        reddit_oauth.search("q", None, 1)  # needs a new token (the stale one is refused) -> cooldown, no request
    assert len(live.token_requests) == 2 and len(live.gets) == 2


def test_failed_token_fetch_cools_down_for_every_process(live):
    live.token_status = 403
    with pytest.raises(FetchError, match="token"):
        reddit_oauth.search("q", None, 1)
    state.reset()
    with pytest.raises(FetchError, match="cooling down"):
        reddit_oauth.search("q", None, 1)
    assert len(live.token_requests) == 1


def test_quota_headers_are_stored_for_other_processes(live):
    live.routes["/search"] = search_payload(t3("a1"))
    reddit_oauth.search("q", None, 1)
    state.reset()
    rate = state.shared().get_json(reddit_oauth.RATE_KEY)
    assert rate["remaining"] == 99.0
    assert rate["reset_at"] == pytest.approx(state.now() + 300, abs=5)


def test_low_quota_waits_for_a_close_reset(live, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(state, "_clock", lambda: now[0])
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    live.routes["/search"] = search_payload(t3("a1"))
    state.shared().set_json(reddit_oauth.RATE_KEY, {"remaining": 2, "reset_at": 1004.0}, 4)

    assert len(reddit_oauth.search("q", None, 1)) == 1
    assert sleeps == [4.0] and len(live.gets) == 1


def test_low_quota_with_a_far_reset_raises_rate_limited_before_any_request(live, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(state, "_clock", lambda: now[0])
    state.shared().set_json(reddit_oauth.RATE_KEY, {"remaining": 1, "reset_at": 1030.0}, 30)

    with pytest.raises(reddit_oauth.RateLimited):
        reddit_oauth.search("q", None, 1)
    assert live.gets == []

    now[0] = 1031.0  # the window reset: requests flow again
    live.routes["/search"] = search_payload(t3("a1"))
    assert len(reddit_oauth.search("q", None, 1)) == 1


def test_http_429_blocks_the_gate_and_raises(live):
    live.routes["/search"] = 429
    with pytest.raises(reddit_oauth.RateLimited):
        reddit_oauth.search("q", None, 1)
    with pytest.raises(reddit_oauth.RateLimited):
        reddit_oauth.search("q", None, 1)
    assert len(live.gets) == 1  # the second call saw the stored 429 and never went out


def test_responses_are_cached_when_a_cache_is_installed(live, tmp_path):
    http.set_cache(Cache(tmp_path / "c.db"))
    live.routes["/search"] = search_payload(t3("a1"))
    reddit_oauth.search("q", None, 1)
    reddit_oauth.search("q", None, 1)
    assert len(live.gets) == 1


# --- search ----------------------------------------------------------------------------------------------------


def test_live_search_is_global_or_scoped_and_maps_hits(live):
    live.routes["/search"] = search_payload(t3("a1", "Hello"))
    (hit,) = reddit_oauth.search("best budget IEM", "headphones", 3)
    scoped = live.gets[0][0]
    assert scoped.startswith("https://oauth.reddit.com/r/headphones/search?q=best+budget+IEM&restrict_sr=on&sort=relevance")
    assert "limit=3" in scoped and "raw_json=1" in scoped
    assert (hit.title, hit.score, hit.num_comments, hit.url) == (
        "Hello", 42, 7, "https://www.reddit.com/r/headphones/comments/a1/x/")

    reddit_oauth.search("git rebase", None, 5)
    assert live.gets[1][0].startswith("https://oauth.reddit.com/search?q=git+rebase&sort=relevance&limit=5")


def test_comparison_query_tops_up_with_the_vs_less_query_keeping_only_on_topic_posts(live):
    def answer(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        if q == "uv vs poetry":
            return search_payload(t3("a1", "uv vs poetry, a year later"), t3("a2", "A list of metroidvanias"))
        assert q == "uv poetry"
        return search_payload(t3("b1", "uv vs Poetry, a year later"), t3("b2", "uv overtakes poetry in downloads"),
                              t3("b3", "Pizza night"))

    live.routes["/search"] = answer
    hits = reddit_oauth.search("uv vs poetry", None, 3)
    assert [hit.title for hit in hits] == [
        "uv vs poetry, a year later", "uv vs Poetry, a year later", "uv overtakes poetry in downloads"]


def test_comparison_query_with_enough_on_topic_hits_costs_one_request(live):
    live.routes["/search"] = search_payload(t3("a1", "chu 2 vs gate"), t3("a2", "gate vs chu 2 again"))
    assert len(reddit_oauth.search("chu 2 vs gate", None, 2)) == 2
    assert len(live.gets) == 1


def test_reddit_search_prefers_live_and_labels_it(live):
    live.routes["/search"] = search_payload(t3("a1"))
    result = reddit.search("q", "r/headphones", 3)
    assert result.via == "reddit (live)" and len(result.hits) == 1
    assert "/r/headphones/search" in live.gets[0][0]


def test_live_zero_hits_is_authoritative_no_archive_fallback(live):
    live.routes["/search"] = search_payload()  # no archive routes: a fallback would fail the test
    result = reddit.search("zzz", None, 3)
    assert result.hits == [] and result.via == "reddit (live)"


def test_oauth_failure_falls_back_to_arctic_with_its_label(live, fake_http, fixtures):
    live.token_status = 403
    fake_http["posts/search"] = fixtures("reddit_search.json")
    result = reddit.search("earbuds", "headphones", 3)
    assert result.via == "arctic-shift (r/headphones, fresh)" and len(result.hits) == 3

    state.reset()
    reddit.search("earbuds again", "headphones", 3)
    assert len(live.token_requests) == 1  # the cooldown is shared: no second token attempt


def test_rate_limited_live_search_falls_back_to_pullpush_globally(live, fake_http, fixtures):
    live.routes["/search"] = 429
    fake_http["pullpush.io"] = fixtures("reddit_search.json")
    result = reddit.search("earbuds", None, 3)
    assert result.via.startswith("pullpush") and len(result.hits) == 3


def test_all_backends_down_names_each(live, fake_http):
    live.token_status = 403
    fake_http["posts/search"] = ARCTIC_422
    fake_http["pullpush.io"] = FetchError("api.pullpush.io: HTTP 500", 500)
    with pytest.raises(FetchError, match=r"reddit live: .*arctic-shift: .*pullpush: ") as raised:
        reddit.search("earbuds", "headphones", 2)
    assert "403" in str(raised.value)


# --- thread ----------------------------------------------------------------------------------------------------


def thread_payload(children):
    return [listing([t3("abc", "Big thread", selftext="body")]), listing(children)]


def more_children(url):
    ids = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["children"][0].split(",")
    things = []
    for index, comment_id in enumerate(ids):
        parent = "t1_r1" if index % 2 == 0 else "t3_abc"
        things.append(t1(comment_id, f"body {comment_id}", parent=parent))
    return {"json": {"errors": [], "data": {"things": things}}}


def test_thread_expands_more_stubs_attaching_by_parent_and_stops_at_three_calls(live):
    ids = [f"m{i}" for i in range(450)]
    live.routes["/comments/abc"] = thread_payload([t1("r1"), t1("r2"), t1("r3"), more(ids), more([], count=5, parent="t1_r2")])
    live.routes["/api/morechildren"] = more_children

    thread = reddit_oauth.thread("abc", max_comments=10_000)

    calls = [url for url, _ in live.gets if "/api/morechildren" in url]
    assert len(calls) == 3
    first = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    assert first["link_id"] == ["t3_abc"] and first["api_type"] == ["json"] and len(first["children"][0].split(",")) == 100
    assert count_comments(thread.comments) == 3 + 300
    assert state.shared().get(reddit_oauth.MORE_BLOCKED_KEY) is None  # morechildren worked: no fallback needed
    r1 = thread.comments[0]
    assert len(r1.children) == 150 and all(c.depth == 1 for c in r1.children)  # even indexes of 300 attach to t1_r1
    assert len(thread.comments) == 3 + 150  # the rest are top level (parent t3_abc)
    assert thread.unloaded == 150 + 5  # 150 ids never requested + the "continue this thread" stub
    assert thread.title == "Big thread" and thread.body == "body"


def test_thread_stops_expanding_once_max_comments_are_loaded(live):
    live.routes["/comments/abc"] = thread_payload([t1("r1"), t1("r2"), t1("r3"), more([f"m{i}" for i in range(450)])])
    live.routes["/api/morechildren"] = more_children

    reddit_oauth.thread("abc", max_comments=150)
    assert len([u for u, _ in live.gets if "/api/morechildren" in u]) == 2  # 3 -> 103 -> 203 >= 150

    live.gets.clear()
    live.routes["/comments/abd"] = thread_payload([t1("r1"), t1("r2"), t1("r3"), more(["x1"])])
    thread = reddit_oauth.thread("abd", max_comments=3)
    assert [u for u, _ in live.gets if "/api/morechildren" in u] == []  # already enough
    assert thread.unloaded == 1


def test_morechildren_403_is_remembered_and_branches_load_through_comment_permalinks(live):
    def focused(comment):
        return [listing([t3("abc", "Big thread")]), listing([comment])]

    def route(url):
        if "/_/t1" in url:
            return focused(t1("t1", replies=listing([t1("t1a", parent="t1_t1", replies=listing([t1("t1b", parent="t1_t1a")]))])))
        if "/_/t2" in url:
            return focused(t1("t2"))
        if "/_/r2" in url:
            return focused(t1("r2", replies=listing([t1("r2a", parent="t1_r2")])))
        return thread_payload([
            t1("r1", replies=listing([more(["a1", "a2"], parent="t1_r1")])),
            t1("r2"),
            more(["t1", "t2"], count=40),  # top-level stub: each id is worth ~20 comments
            more([], count=5, parent="t1_r2"),  # "continue this thread"
        ])

    live.routes["/comments/abc"] = route
    live.routes["/comments/abd"] = route
    live.routes["/api/morechildren"] = 403

    thread = reddit_oauth.thread("abc", max_comments=10_000)

    urls = [url for url, _ in live.gets]
    assert sum("/api/morechildren" in u for u in urls) == 1
    assert [u.split("?")[0].rsplit("/", 1)[1] for u in urls if "/_/" in u] == ["t1", "t2", "r2"]  # best yield first, 3-call cap
    assert count_comments(thread.comments) == 2 + 3 + 1 + 1  # r1 r2, t1 t1a t1b, t2, r2a
    r1, r2, t1_, _ = thread.comments
    assert r2.children[0].depth == 1 and t1_.children[0].children[0].depth == 2 and not r1.children
    assert thread.unloaded == 2  # r1's stub (a1, a2) is left over
    assert state.shared().get(reddit_oauth.MORE_BLOCKED_KEY) is not None

    state.reset()  # another process: the 403 is already known, no wasted morechildren call
    live.gets.clear()
    reddit_oauth.thread("abd", max_comments=10_000)
    assert not any("/api/morechildren" in url for url, _ in live.gets)


def test_morechildren_failure_keeps_the_loaded_thread(live):
    live.routes["/comments/abc"] = thread_payload([t1("r1"), more(["m1", "m2"])])
    live.routes["/api/morechildren"] = 500
    thread = reddit_oauth.thread("abc", max_comments=100)
    assert count_comments(thread.comments) == 1 and thread.unloaded == 2


def test_fetch_thread_uses_live_then_archives(live, fake_http, fixtures):
    live.routes["/comments/15gd2g3"] = thread_payload([t1("r1")])
    assert reddit.fetch_thread("15gd2g3", 50).title == "Big thread"

    live.routes["/comments/15gd2g3"] = 404
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    fake_http["comments/tree"] = fixtures("reddit_tree.json")
    assert reddit.fetch_thread("15gd2g3", 50).title == "My cheap earbuds..."


# --- shared cooldowns ------------------------------------------------------------------------------------------


def test_state_values_expire_and_are_shared_between_instances(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(state, "_clock", lambda: now[0])
    first, second = state.State(tmp_path / "s.db"), state.State(tmp_path / "s.db")
    first.set("k", "v", 10)
    assert second.get("k") == "v"
    now[0] += 11
    assert second.get("k") is None and first.get("k") is None


def test_arctic_cooldown_is_visible_to_a_fresh_process(fake_http, fixtures):
    fake_http["posts/search"] = ARCTIC_422
    fake_http["pullpush.io"] = fixtures("reddit_search.json")
    reddit.search("earbuds", "headphones", 2)
    attempts = len([c for c in fake_http.calls if "arctic-shift" in c[0]])
    state.reset()
    reddit.search("earbuds", "headphones", 2)
    assert len([c for c in fake_http.calls if "arctic-shift" in c[0]]) == attempts


def test_ddg_block_is_visible_to_a_fresh_process(fake_http, fixtures):
    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    fake_http["hn.algolia.com"] = fixtures("hn_search.json")
    fake_http["pullpush.io"] = FetchError("api.pullpush.io: HTTP 429", 429)
    fake_http["api.stackexchange.com"] = FetchError("api.stackexchange.com: HTTP 502")
    discover.search("one", 3)
    state.reset()
    discover.search("two", 3)
    assert sum("duckduckgo" in url for url, _ in fake_http.calls) == 1


def test_web_search_uses_live_reddit_when_duckduckgo_is_blocked(live, fake_http, fixtures):
    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    fake_http["hn.algolia.com"] = fixtures("hn_search.json")
    fake_http["api.stackexchange.com"] = fixtures("se_search.json")
    live.routes["/search"] = search_payload(t3("a1"), t3("a2"))
    result = discover.search("git rebase", 6)
    assert "reddit (live): 2 hits" in result.via
    assert not any("pullpush" in url for url, _ in fake_http.calls)
    assert [h.source for h in result.hits][:3] == ["reddit", "hn", "se"]


# --- one retry layer ---------------------------------------------------------------------------------------------


@pytest.fixture
def urlopen_log(monkeypatch):
    """Real http layer, fake urlopen answering every request with `status`; returns the list of requested URLs."""
    urls: list[str] = []
    status = {"code": 422}

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, status["code"], "x", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake_urlopen.urls, fake_urlopen.status = urls, status
    return fake_urlopen


def test_http_attempts_per_logical_call_do_not_multiply(urlopen_log):
    urlopen_log.status["code"] = 503  # retryable at both layers: only one of them may retry
    with pytest.raises(FetchError, match="HTTP 503|pullpush"):
        reddit.search("earbuds", "headphones", 2)
    arctic = [u for u in urlopen_log.urls if "arctic-shift" in u]
    pullpush = [u for u in urlopen_log.urls if "pullpush" in u]
    assert len(arctic) == reddit.SEARCH_ATTEMPTS == 2
    assert len(pullpush) == reddit.ATTEMPTS == 3


def test_persistent_arctic_422_is_negatively_cached_for_five_minutes(fake_http, tmp_path):
    clock = [1000.0]
    http.set_cache(Cache(tmp_path / "neg.db", clock=lambda: clock[0]))
    fake_http["posts/search"] = ARCTIC_422
    params = {"subreddit": "headphones", "query": "q"}

    with pytest.raises(FetchError):
        reddit._api(reddit.ARCTIC, "posts/search", params, attempts=2)
    assert len(fake_http.calls) == 2

    clock[0] += reddit.NEGATIVE_TTL - 1
    with pytest.raises(FetchError, match="failed moments ago") as raised:
        reddit._api(reddit.ARCTIC, "posts/search", params, attempts=2)
    assert raised.value.status == 422 and len(fake_http.calls) == 2  # no request went out

    clock[0] += 2
    with pytest.raises(FetchError):
        reddit._api(reddit.ARCTIC, "posts/search", params, attempts=2)
    assert len(fake_http.calls) == 4  # the negative entry aged out

    with pytest.raises(FetchError):
        reddit._api(reddit.ARCTIC, "posts/search", {"subreddit": "other", "query": "q"}, attempts=2)
    assert len(fake_http.calls) == 6  # other URLs are unaffected
