import pytest

from forumread.http import FetchError
from forumread.model import count_comments
from forumread.sources import reddit

ARCTIC_ERR = {"data": None, "error": "Timeout. Maybe slow down a bit"}


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.children)


def test_tree_with_more_and_deleted_nodes(fake_http, fixtures):
    tree = fixtures("reddit_tree.json")
    first = tree["data"][0]["data"]
    first["replies"]["data"]["children"].append({"kind": "more", "data": {"children": ["abc"], "count": 3}})
    first["replies"]["data"]["children"].append(
        {"kind": "t1", "data": {"id": "zz1", "author": "[deleted]", "body": "[deleted]", "score": 1,
                                "created_utc": 1, "parent_id": "t1_x", "replies": ""}}
    )
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    fake_http["comments/tree"] = tree
    fake_http["comments/search"] = ARCTIC_ERR  # the "more" stub triggers a flat attempt; it fails, so the tree is kept

    thread = reddit.fetch_thread("15gd2g3")

    assert thread.title == "My cheap earbuds..."
    assert thread.url == "https://www.reddit.com/r/headphones/comments/15gd2g3/my_cheap_earbuds/"
    assert len(thread.comments) == 3
    assert count_comments(thread.comments) == 16  # 15 real + the deleted node (render drops it); "more" stub skipped
    assert thread.unloaded == 3  # what the stub hides is reported, not silently dropped
    top = thread.comments[0]
    assert (top.author, top.score, top.depth) == ("htoisanaung", 17, 0)
    assert top.children[0].children[0].children[0].depth == 3


def _truncated_tree(fixtures):
    tree = fixtures("reddit_tree.json")
    tree["data"][0]["data"]["replies"]["data"]["children"].append({"kind": "more", "data": {"children": ["abc"], "count": 3}})
    return tree


def test_truncated_tree_falls_through_to_flat_pagination(fake_http, fixtures):
    records = [
        {"id": f"c{i}", "parent_id": "t3_x", "body": f"b{i}", "author": "u", "created_utc": 100 + i, "score": 1}
        for i in range(30)
    ]
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    fake_http["comments/tree"] = _truncated_tree(fixtures)
    fake_http["comments/search"] = {"data": records}

    thread = reddit.fetch_thread("15gd2g3", max_comments=300)

    assert count_comments(thread.comments) == 30 and thread.unloaded == 0


def test_truncated_tree_that_already_covers_max_comments_is_not_paginated(fake_http, fixtures):
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    fake_http["comments/tree"] = _truncated_tree(fixtures)  # no comments/search route: asking for it fails the test

    thread = reddit.fetch_thread("15gd2g3", max_comments=10)

    assert count_comments(thread.comments) == 15 and thread.unloaded == 3


def test_flat_rebuilt_into_tree_when_tree_fails(fake_http, fixtures):
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    fake_http["comments/tree"] = ARCTIC_ERR
    fake_http["comments/search"] = fixtures("reddit_flat.json")

    thread = reddit.fetch_thread("15gd2g3")

    assert count_comments(thread.comments) == 15
    assert {c.author for c in thread.comments} == {"htoisanaung", "Silent-Piccolo", "OutlandishnessOk8261"}
    htoisanaung = next(c for c in thread.comments if c.author == "htoisanaung")
    assert htoisanaung.children and all(c.depth == 1 for c in htoisanaung.children)
    assert max(c.depth for c in walk(thread.comments)) == 3


def test_rebuild_tree_orphans_become_top_level():
    flat = [
        {"id": "a", "parent_id": "t3_x", "body": "root", "author": "u"},
        {"id": "b", "parent_id": "t1_a", "body": "child", "author": "u"},
        {"id": "c", "parent_id": "t1_missing", "body": "orphan", "author": "u"},
    ]
    roots = reddit.rebuild_tree(flat)
    assert [r.body for r in roots] == ["root", "orphan"]
    assert roots[0].children[0].body == "child"


def test_flat_pagination_uses_cursor_and_dedupes(fake_http, fixtures, monkeypatch):
    monkeypatch.setattr(reddit, "PAGE_SIZE", 2)
    records = [
        {"id": f"c{i}", "parent_id": "t3_x", "body": f"b{i}", "author": "u", "created_utc": 100 + i}
        for i in range(3)
    ]
    cursors = []

    def page(url, params):
        cursors.append(params.get("after"))
        after = params.get("after")
        return {"data": [r for r in records if after is None or r["created_utc"] > after][:2]}

    fake_http["comments/search"] = page
    got = reddit._flat_comments(reddit.ARCTIC, "comments/search", "x", "limit")
    assert [r["id"] for r in got] == ["c0", "c1", "c2"]
    assert cursors[0] is None and cursors[1] == 100


def test_pullpush_fallback_when_arctic_down(fake_http, fixtures):
    flat = fixtures("reddit_flat.json")
    post = fixtures("reddit_post.json")
    fake_http["arctic-shift"] = ARCTIC_ERR
    fake_http["pullpush.io/reddit/search/submission"] = post
    fake_http["pullpush.io/reddit/search/comment"] = flat

    thread = reddit.fetch_thread("15gd2g3")

    assert thread.title == "My cheap earbuds..."
    assert count_comments(thread.comments) == 15
    assert any("pullpush.io" in url for url, _ in fake_http.calls)


def test_everything_down_raises_fetch_error(fake_http):
    fake_http["arctic-shift"] = ARCTIC_ERR
    fake_http["pullpush.io"] = {"data": None, "error": "429"}
    with pytest.raises(FetchError, match="unavailable"):
        reddit.fetch_thread("15gd2g3")


def test_comments_unavailable_raises(fake_http, fixtures):
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    fake_http["arctic-shift"] = ARCTIC_ERR
    fake_http["pullpush.io"] = ARCTIC_ERR
    with pytest.raises(FetchError, match="comments for 15gd2g3 unavailable"):
        reddit.fetch_thread("15gd2g3")


def test_search_with_subreddit_uses_arctic_query_search(fake_http, fixtures):
    fake_http["posts/search"] = fixtures("reddit_search.json")
    result = reddit.search("earbuds", "r/headphones", 5)
    hits = result.hits
    _url, params = fake_http.calls[0]
    assert params["subreddit"] == "headphones" and params["query"] == "earbuds" and "title" not in params
    assert result.via == "arctic-shift (r/headphones, fresh)"
    assert len(hits) == 3 and hits[0].url.startswith("https://www.reddit.com/r/")
    assert hits[0].num_comments is not None


def test_arctic_422_timeout_is_retried(fake_http, fixtures):
    answers = iter([FetchError("arctic-shift.photon-reddit.com: HTTP 422", 422), fixtures("reddit_search.json")])
    fake_http["posts/search"] = lambda url, params: next(answers)
    assert len(reddit.search("earbuds", "headphones", 5).hits) == 3


def test_removed_selftext_is_not_a_snippet(fake_http, fixtures):
    data = fixtures("reddit_search.json")
    data["data"][0]["selftext"] = "[removed]"
    fake_http["posts/search"] = data
    assert reddit.search("earbuds", "headphones", 5).hits[0].snippet == ""
