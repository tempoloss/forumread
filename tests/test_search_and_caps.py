from forumread import __main__ as cli
from forumread import state
from forumread.http import FetchError
from forumread.model import Comment, SearchHit, SearchResult, Thread, assign_depths
from forumread.render import Limits, prepare, render_hits, render_thread
from forumread.sources import hn, reddit

ARCTIC_422 = FetchError("arctic-shift.photon-reddit.com: HTTP 422", 422)


def post(pid, title, score=1, selftext=""):
    return {"id": pid, "title": title, "score": score, "selftext": selftext, "num_comments": 1,
            "created_utc": 1700000000, "permalink": f"/r/x/comments/{pid}/t/"}


def make_thread(comments, body="Post body"):
    assign_depths(comments)
    return Thread("reddit", "t1", "https://www.reddit.com/r/s/comments/t1/x/", "Title", "op", 10, 1700000000, body, comments)


def test_arctic_422_falls_back_to_pullpush_then_cools_down(fake_http, fixtures, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(state, "_clock", lambda: now[0])
    fake_http["posts/search"] = ARCTIC_422
    fake_http["pullpush.io"] = fixtures("reddit_search.json")

    first = reddit.search("earbuds", "headphones", 2)

    assert len(first.hits) == 2
    assert first.via == "pullpush (arctic-shift rate-limited), lags ~1 week"
    arctic_calls = [c for c in fake_http.calls if "arctic-shift" in c[0]]
    assert len(arctic_calls) == reddit.SEARCH_ATTEMPTS
    _, params = next(c for c in fake_http.calls if "pullpush.io" in c[0])
    assert "subreddit" not in params and params["q"] == "earbuds"  # pullpush 429s any request carrying subreddit=
    assert params["size"] == 100 and params["sort_type"] == "score" and params["sort"] == "desc"

    now[0] += reddit.ARCTIC_COOLDOWN - 1  # still cooling down: Arctic Shift is not touched again
    second = reddit.search("earbuds", "headphones", 2)
    assert second.via == first.via
    assert len([c for c in fake_http.calls if "arctic-shift" in c[0]]) == len(arctic_calls)

    now[0] += 2  # cooldown over: Arctic Shift is tried again
    fake_http["posts/search"] = fixtures("reddit_search.json")
    assert reddit.search("earbuds", "headphones", 2).via == "arctic-shift (r/headphones, fresh)"


def test_both_backends_down_reports_both(fake_http):
    fake_http["posts/search"] = ARCTIC_422
    fake_http["pullpush.io"] = FetchError("api.pullpush.io: HTTP 500", 500)
    try:
        reddit.search("earbuds", "headphones", 2)
    except FetchError as exc:
        assert "arctic-shift" in str(exc) and "pullpush" in str(exc)
    else:
        raise AssertionError("expected FetchError")


def test_pullpush_rerank_needs_half_the_content_words_and_orders_by_overlap(fake_http):
    posts = [
        post("a1", "Infosys interview experience", score=900),
        post("b2", "Git rebase vs merge", score=10),
        post("c3", "Merge or rebase conflicts", score=10),
        post("c4", "Rebase and merge questions", score=500),
        post("d4", "Team workflow question", score=1, selftext="we use git, rebase and merge all day"),
        post("e5", "Merge strategies", score=700),  # one of three content words: below the half-rounded-up bar
    ]
    fake_http["pullpush.io"] = {"data": posts}

    result = reddit.search("git rebase vs merge", None, 10)

    assert [h.url.split("/")[-3] for h in result.hits] == ["b2", "c4", "c3", "d4"]
    _, params = fake_http.calls[0]
    assert params["size"] == 100 and params["sort_type"] == "score" and params["sort"] == "desc"

    assert len(reddit.search("git rebase vs merge", None, 2).hits) == 2


def test_pullpush_rerank_ignores_stopwords_and_prefers_no_results_to_junk(fake_http):
    fake_http["pullpush.io"] = {"data": [
        post("j1", "ALLEN Ribbon Headphone : A Subwoofer for Your Ears", selftext="I also own one IEM, but this is about ribbons"),
        post("j2", "Which is the best worth it?"),
    ]}
    assert reddit.search("best budget IEM", None, 5).hits == []  # a lone body mention of "iem" is not relevance
    fake_http["pullpush.io"] = {"data": [post("k1", "Cheap IEM roundup", selftext="budget picks")]}
    assert len(reddit.search("best budget IEM", None, 5).hits) == 1


def test_bot_named_author_with_automated_wording_is_a_bot():
    thread = make_thread([
        Comment("TransducerBot", 1, 0, "This submission has automatically been removed for rule 2."),
        Comment("RemindMeBot", 1, 0, "Reminder set. I am a bot, beep."),
        Comment("OtherBot", 1, 0, "Beep. this action was performed by a script"),
        Comment("RealPersonBot", 2, 0, "I just love my gear"),  # bot-like name, human wording: kept
        Comment("human", 3, 0, "This thread was automatically cross-posted, I think"),  # not bot-named: kept
    ])
    view = prepare(thread, Limits())
    assert sorted(c.author for c in view.thread.comments) == ["RealPersonBot", "human"]
    assert view.bots_hidden == 3


def test_web_fallback_labels_loose_hn_matches(fake_http, fixtures):
    from forumread import discover

    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    fake_http["pullpush.io"] = {"data": []}
    fake_http["api.stackexchange.com"] = {"items": []}
    fake_http["hn.algolia.com"] = lambda url, params: (
        {"hits": []} if "removeWordsIfNoResults" not in params else fixtures("hn_search.json")
    )
    result = discover.search("zzqxw nonexistent topic", 5)
    assert "hn: 2 hits (loose match)" in result.via


def test_post_body_truncated_at_paragraph_boundary_with_marker():
    first, second = "x" * 60, "y" * 60
    view = prepare(make_thread([], body=f"{first}\n\n{second}"), Limits(max_body_chars=100))
    out = render_thread(view)
    assert f"\n{first}\n_[post truncated: 60 of 122 chars; raise max_body_chars]_\n" in out
    assert "yyy" not in out

    untouched = render_thread(prepare(make_thread([], body=first), Limits(max_body_chars=100)))
    assert "truncated" not in untouched


def test_comment_truncated_with_marker():
    long = "word " * 10 + "\n" + "tail " * 40
    comment = Comment("a", 1, 0, long)
    out = render_thread(prepare(make_thread([comment]), Limits(max_comment_chars=60)))
    assert "_[comment truncated: " in out and "of 251 chars" in out and "tail" not in out.split("truncated")[0]


def test_truncation_never_leaves_a_code_fence_open():
    body = "intro\n```\n" + "code line\n" * 30 + "```"
    out = render_thread(prepare(make_thread([], body=body), Limits(max_body_chars=100)))
    assert out.count("```") % 2 == 0 and "_[post truncated:" in out


def test_hn_strict_first_then_loose_with_label(fake_http):
    def respond(url, params):
        return {"hits": []} if "removeWordsIfNoResults" not in params else {"hits": [
            {"objectID": "1", "title": "Unrelated story", "points": 3, "num_comments": 0, "created_at_i": 1}]}

    fake_http["/search"] = respond
    result = hn.search("zzqxw nonexistent topic", 5)

    assert result.via == "hn.algolia.com (loose match: not all words found)"
    assert [h.title for h in result.hits] == ["Unrelated story"]
    assert "removeWordsIfNoResults" not in fake_http.calls[0][1]
    assert fake_http.calls[1][1]["removeWordsIfNoResults"] == "allOptional"


def test_hn_strict_hit_makes_one_call_and_dedupes_crossposts(fake_http):
    fake_http["/search"] = {"hits": [
        {"objectID": "1", "title": "Show HN: Foo", "points": 5, "num_comments": 1, "created_at_i": 1},
        {"objectID": "2", "title": "Other thing", "points": 50, "num_comments": 1, "created_at_i": 2},
        {"objectID": "3", "title": "show hn:  foo!", "points": 90, "num_comments": 9, "created_at_i": 3},
    ]}
    result = hn.search("foo", 10)

    assert result.via == "hn.algolia.com" and len(fake_http.calls) == 1
    assert [(h.url[-1], h.score) for h in result.hits] == [("3", 90), ("2", 50)]  # best crosspost kept, at first rank


def test_zero_hits_exit_0_with_no_results_line(fake_http, capsys):
    fake_http["posts/search"] = {"data": []}
    code = cli.main(["search", "nothing", "--source", "reddit", "--subreddit", "headphones"])
    out = capsys.readouterr().out
    assert code == 0 and out.endswith("_No results._\n") and out.startswith("# Search: nothing\n_via arctic-shift")


def test_header_when_everything_was_filtered_as_bots():
    thread = make_thread([Comment("AutoModerator", 1, 0, "rules")])
    out = render_thread(prepare(thread, Limits()))
    assert "· 1 comment (all hidden: bots/deleted; use include_bots) · 1 bot comments hidden\n" in out
    assert "_No visible comments._" in out


def test_header_shows_visible_of_total():
    thread = make_thread([Comment(str(i), i, 0, "t") for i in range(25)])
    out = render_thread(prepare(thread, Limits(max_comments=6)))
    assert "· 25 comments (6 shown)\n" in out


def test_snippet_is_one_short_line_without_markdown_headers():
    hit = SearchHit("reddit", "T", "https://u", snippet="# Disclaimer\n\n" + "word " * 100)
    out = render_hits("q", SearchResult([hit], "via"))
    line = out.splitlines()[-1].strip()
    assert not line.startswith("#") and line.startswith("Disclaimer word") and len(line) <= 160
    assert line.endswith("\u2026")


def test_pullpush_fallback_keeps_only_the_requested_subreddit(fake_http):
    other = {**post("o1", "earbuds elsewhere", score=999), "subreddit": "audiophile"}
    mine = {**post("m1", "earbuds here", score=1), "subreddit": "Headphones"}
    fake_http["posts/search"] = ARCTIC_422
    fake_http["pullpush.io"] = {"data": [other, mine]}
    result = reddit.search("earbuds", "r/headphones", 5)
    assert [h.title for h in result.hits] == ["earbuds here"]
