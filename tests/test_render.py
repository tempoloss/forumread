from forumread.model import Comment, Thread, assign_depths
from forumread.render import Limits, prepare, render_hits, render_thread
from forumread.model import SearchHit, SearchResult


def c(author, score, body="text", created=0, children=()):
    return Comment(author, score, created, body, children=list(children))


def make_thread(comments, body="Post body"):
    assign_depths(comments)
    return Thread("reddit", "t1", "https://www.reddit.com/r/s/comments/t1/x/", "Title &", "op", 10, 1700000000, body, comments)


def test_siblings_sorted_by_score_and_deleted_dropped():
    thread = make_thread([c("low", 1), c("gone", 50, "[deleted]"), c("high", 9, children=[c("a", 1), c("b", 5)])])
    out = render_thread(prepare(thread, Limits()))
    assert out.index("**high**") < out.index("**low**")
    assert out.index("**b**") < out.index("**a**")
    assert "gone" not in out
    assert "  - **b** (5): text" in out  # replies indented under parent


def test_deleted_parent_with_replies_is_kept():
    thread = make_thread([c("gone", 5, "[removed]", children=[c("reply", 3)])])
    out = render_thread(prepare(thread, Limits()))
    assert "**gone** (5): [deleted]" in out and "**reply**" in out


def test_max_comments_truncation_notice_keeps_parents():
    thread = make_thread([c("a", 10, children=[c("a1", 1)]), c("b", 8), c("c", 2, children=[c("c1", 99)])])
    view = prepare(thread, Limits(max_comments=3))
    out = render_thread(view)
    assert view.total == 5 and view.shown == 3
    assert "_Showing 3 of 5 comments, sorted by top (limits: max_comments=3)._" in out
    assert "**a**" in out and "**b**" in out and "**c**" in out  # c kept: c1's parent must precede it
    assert "c1" not in out and "a1" not in out


def test_best_first_selection_pulls_in_high_scoring_reply():
    thread = make_thread([c("a", 10, children=[c("deep", 50)]), c("b", 1)])
    out = render_thread(prepare(thread, Limits(max_comments=2)))
    assert "**deep**" in out and "**b**" not in out


def test_min_score_and_depth():
    thread = make_thread([c("a", 10, children=[c("low", 0, children=[c("hidden", 100)]), c("ok", 5)]), c("b", -3)])
    view = prepare(thread, Limits(min_score=1))
    out = render_thread(view)
    assert "low" not in out and "hidden" not in out and "**b**" not in out and "**ok**" in out
    assert "min_score=1" in out and "--min-score" not in out
    flat = render_thread(prepare(thread, Limits(depth=1)))
    assert "**ok**" not in flat and "**a**" in flat and "depth=1" in flat


def test_sort_new():
    thread = make_thread([c("old", 100, created=1), c("fresh", 1, created=9)])
    out = render_thread(prepare(thread, Limits(sort="new")))
    assert out.index("**fresh**") < out.index("**old**")


def test_no_notice_when_nothing_cut_and_header():
    thread = make_thread([c("a", 1)])
    out = render_thread(prepare(thread, Limits()))
    assert "_Showing" not in out
    assert out.startswith("# Title &\nhttps://www.reddit.com/r/s/comments/t1/x/\nReddit r/s · by op · score 10 · 2023-11-14 · 1 comment\n")
    assert "\nPost body\n" in out


def test_multiline_and_code_bodies_stay_indented():
    thread = make_thread([c("a", 1, "line1\n\nline2\n```\ncode\n\nmore\n```")])
    out = render_thread(prepare(thread, Limits()))
    assert "- **a** (1): line1\n  line2\n  ```\n  code\n\n  more\n  ```\n" in out


def test_json_view():
    thread = make_thread([c("a", 1), c("b", 2)])
    data = prepare(thread, Limits(max_comments=1)).as_dict()
    assert data["comments_total"] == 2 and data["comments_shown"] == 1
    assert data["comments"][0]["author"] == "b"


def test_render_hits():
    hit = SearchHit("reddit", "T", "https://u", 5, 3, 1700000000, "snip  ets")
    out = render_hits("q", SearchResult([hit], "pullpush (test)"))
    assert out.startswith("# Search: q\n_via pullpush (test)_\n\n")
    assert "1. **T**\n   https://u · reddit · score 5 · 3 comments · 2023-11-14\n   snip ets" in out


def test_bots_dropped_by_default_and_restored_with_include_bots():
    footer = "Useful reminder.\n\n^(I am a bot, and this action was performed automatically. Contact the mods.)"
    thread = make_thread(
        [
            c("AutoModerator", 1, "Please read the rules", children=[c("human_reply", 4)]),
            c("RemindBot", 2, footer),
            c("RealPersonBot", 3, "My username ends in bot but I am a person"),
            c("someone", 9, "I think I am a bot sometimes, honestly.\nThanks for reading"),
        ]
    )
    view = prepare(thread, Limits())
    out = render_thread(view)
    assert "AutoModerator" not in out and "RemindBot" not in out
    assert "**RealPersonBot**" in out and "**someone**" in out
    assert "**human_reply** (4)" in out  # a reply to a bot is kept, moved up a level
    assert view.total == 3 and view.bots_hidden == 2 and "5 comments (3 shown) · 2 bot comments hidden" in out
    assert prepare(thread, Limits(depth=1)).shown == 3  # hoisted reply counts as top level

    full = prepare(thread, Limits(include_bots=True))
    out = render_thread(full)
    assert "**AutoModerator**" in out and "**RemindBot**" in out and full.bots_hidden == 0
    assert "bot comments hidden" not in out
