import json
import threading

import pytest

from forumread import __main__ as cli
from forumread.http import FetchError


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_thread_success_and_json(fake_http, fixtures, capsys):
    fake_http["/items/8863"] = fixtures("hn_item.json")
    code, out, err = run(["thread", "https://news.ycombinator.com/item?id=8863", "--depth", "1"], capsys)
    assert code == 0 and err == ""
    assert out.startswith("# My YC app: Dropbox") and "**zaidf**" in out and "**vlad**" not in out

    code, out, _ = run(["thread", "8863", "--source", "hn", "--json"], capsys)
    (data,) = json.loads(out)
    assert code == 0 and data["source"] == "hn" and data["comments_total"] == 12


def test_fetch_failure_is_one_line_stderr_and_exit_1(fake_http, capsys, monkeypatch):
    fake_http["hn.algolia.com"] = FetchError("hn.algolia.com: HTTP 503 (after 4 attempts)")
    code, out, err = run(["thread", "https://news.ycombinator.com/item?id=1"], capsys)
    assert code == 1 and out == ""
    assert err == "forumread: error: hn.algolia.com: HTTP 503 (after 4 attempts)\n"


def test_bad_target_exit_2(capsys):
    code, out, err = run(["thread", "https://example.com/x"], capsys)
    assert code == 2 and out == "" and err.startswith("forumread: error: unsupported site")


def test_batch_partial_failure_renders_error_block_and_exits_0(fake_http, fixtures, capsys):
    fake_http["/items/8863"] = fixtures("hn_item.json")
    fake_http["/items/1"] = FetchError("hn.algolia.com: HTTP 503 (after 4 attempts)")
    urls = ["https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=8863", "https://example.com/x"]
    code, out, err = run(["thread", *urls, "--depth", "1"], capsys)
    blocks = out.split("\n---\n\n")
    assert code == 0 and err == "" and len(blocks) == 3  # input order kept
    assert blocks[0] == "**Error reading https://news.ycombinator.com/item?id=1:** hn.algolia.com: HTTP 503 (after 4 attempts)\n"
    assert blocks[1].startswith("# My YC app: Dropbox")
    assert blocks[2].startswith("**Error reading https://example.com/x:** unsupported site")


def test_batch_all_failed_exits_nonzero_with_stderr_only(fake_http, capsys):
    fake_http["hn.algolia.com"] = FetchError("hn.algolia.com: HTTP 500")
    urls = ["https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=2"]
    code, out, err = run(["thread", *urls], capsys)
    assert code == 1 and out == ""
    assert err.splitlines() == [f"forumread: error: {u}: hn.algolia.com: HTTP 500" for u in urls]


def test_batch_json_is_a_list_with_error_entries(fake_http, fixtures, capsys):
    fake_http["/items/8863"] = fixtures("hn_item.json")
    code, out, _ = run(["thread", "https://news.ycombinator.com/item?id=8863", "https://example.com/x", "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and data[0]["source"] == "hn" and data[1]["target"] == "https://example.com/x" and "error" in data[1]


def test_search_without_results_is_a_normal_result(fake_http, capsys):
    fake_http["hn.algolia.com"] = {"hits": []}
    code, out, err = run(["search", "zzz", "--source", "hn"], capsys)
    assert code == 0 and err == ""
    assert out == "# Search: zzz\n_via hn.algolia.com_\n\n_No results._\n"


def test_search_hits_name_the_backend(fake_http, fixtures, capsys):
    fake_http["posts/search"] = fixtures("reddit_search.json")
    code, out, _ = run(["search", "earbuds", "--source", "reddit", "--subreddit", "headphones"], capsys)
    assert code == 0 and out.startswith("# Search: earbuds\n_via arctic-shift (r/headphones, fresh)_\n\n1. **")


def test_global_reddit_search_goes_to_pullpush(fake_http, fixtures, capsys):
    fake_http["pullpush.io"] = fixtures("reddit_search.json")
    code, out, _ = run(["search", "earbuds", "--source", "reddit"], capsys)
    assert code == 0 and "_via pullpush" in out


def test_multi_query_search_groups_per_query_and_isolates_failures(fake_http, fixtures, capsys):
    fake_http["hn.algolia.com"] = lambda url, params: (
        fixtures("hn_search.json") if params["query"] == "rust" else {"hits": []}
    )
    code, out, err = run(["search", "rust", "-q", "nothing-here", "--query", "rust", "--source", "hn"], capsys)
    blocks = out.split("\n---\n\n")
    assert code == 0 and err == "" and len(blocks) == 2  # duplicate query collapsed
    assert blocks[0].startswith("# Search: rust\n_via hn.algolia.com_")
    assert blocks[1] == "# Search: nothing-here\n_via hn.algolia.com_\n\n_No results._\n"

    code, out, _ = run(["search", "rust", "--source", "hn", "--json"], capsys)
    (entry,) = json.loads(out)
    assert code == 0 and entry["query"] == "rust" and entry["via"] == "hn.algolia.com" and entry["hits"]


def test_search_needs_a_query(capsys):
    code, _, err = run(["search", "--source", "hn"], capsys)
    assert code == 2 and "no search query" in err


def test_subreddit_only_for_reddit(capsys):
    code, _, err = run(["search", "x", "--source", "hn", "--subreddit", "a"], capsys)
    assert code == 2 and "--subreddit" in err


def test_deadline_prints_finished_threads_and_marks_unfinished_ones_timed_out(fake_http, fixtures, capsys):
    release = threading.Event()
    fake_http["/items/8863"] = fixtures("hn_item.json")
    fake_http["/items/1"] = lambda url, params: release.wait(10) and fixtures("hn_item.json")
    urls = ["https://news.ycombinator.com/item?id=8863", "https://news.ycombinator.com/item?id=1"]
    try:
        code, out, err = run(["thread", *urls, "--depth", "1", "--deadline", "0.3"], capsys)
    finally:
        release.set()
    blocks = out.split("\n---\n\n")
    assert code == 0 and err == "" and len(blocks) == 2
    assert blocks[0].startswith("# My YC app: Dropbox")
    assert blocks[1] == "**Timed out:** https://news.ycombinator.com/item?id=1\n"


def test_deadline_with_nothing_finished_is_an_error(fake_http, capsys):
    release = threading.Event()
    fake_http["/items/1"] = lambda url, params: release.wait(10)
    try:
        code, out, err = run(["thread", "https://news.ycombinator.com/item?id=1", "--deadline", "0.2"], capsys)
    finally:
        release.set()
    assert code == 1 and out == "" and err == "forumread: error: timed out after 0.2s\n"


def test_deadline_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["thread", "x", "--deadline", "0"])


def test_search_that_rerank_emptied_says_no_strong_match_not_no_results(fake_http, capsys):
    fake_http["pullpush.io"] = {"data": [{"id": "z1", "title": "Infosys interview experience", "score": 900,
                                          "selftext": "", "num_comments": 1, "created_utc": 1, "permalink": "/r/x/comments/z1/t/"}]}
    code, out, _ = run(["search", "git rebase vs merge", "--source", "reddit"], capsys)
    note = "_No strong match in pullpush global top-100 (archive, lags ~1 week); try fewer words or source=hn/se._"
    assert code == 0 and out.endswith(f"\n{note}\n") and "_No results._" not in out

    code, out, _ = run(["search", "git rebase vs merge", "--source", "reddit", "--json"], capsys)
    assert json.loads(out)[0]["note"] == note

    fake_http["pullpush.io"] = {"data": []}  # pullpush itself found nothing: that really is "no results"
    code, out, _ = run(["search", "git rebase vs merge", "--source", "reddit"], capsys)
    assert out.endswith("\n_No results._\n")


def test_thread_header_reports_comments_the_source_did_not_load(fake_http, fixtures, capsys):
    fake_http["posts/ids"] = fixtures("reddit_post.json")
    tree = fixtures("reddit_tree.json")
    tree["data"][0]["data"]["replies"]["data"]["children"].append({"kind": "more", "data": {"children": ["a"], "count": 4}})
    fake_http["comments/tree"] = tree
    code, out, _ = run(["thread", "15gd2g3", "--max-comments", "5"], capsys)
    assert code == 0 and "_4 more comments exist but were not loaded" in out
