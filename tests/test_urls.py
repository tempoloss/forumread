import pytest

from forumread.urls import Target, UsageError, parse_target


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://www.reddit.com/r/headphones/comments/15gd2g3/my_cheap_earbuds/", Target("reddit", "15gd2g3")),
        ("https://old.reddit.com/r/x/comments/ABC123/title", Target("reddit", "abc123")),
        ("https://reddit.com/comments/15gd2g3", Target("reddit", "15gd2g3")),
        ("reddit.com/r/x/comments/15gd2g3/t/?utm=1", Target("reddit", "15gd2g3")),
        ("https://redd.it/15gd2g3", Target("reddit", "15gd2g3")),
        ("15gd2g3", Target("reddit", "15gd2g3")),
        ("t3_15gd2g3", Target("reddit", "15gd2g3")),
        ("https://news.ycombinator.com/item?id=8863", Target("hn", "8863")),
        ("https://news.ycombinator.com/item?id=8863&p=2", Target("hn", "8863")),
        ("https://stackoverflow.com/questions/1711631/improve-insert", Target("se", "1711631", "stackoverflow")),
        ("https://superuser.com/questions/42/x", Target("se", "42", "superuser")),
        ("https://serverfault.com/questions/42", Target("se", "42", "serverfault")),
        ("https://askubuntu.com/q/42/x", Target("se", "42", "askubuntu")),
        ("https://unix.stackexchange.com/questions/7/x", Target("se", "7", "unix")),
        ("https://meta.unix.stackexchange.com/questions/7/x", Target("se", "7", "unix.meta")),
    ],
)
def test_detects_source(value, expected):
    assert parse_target(value) == expected


def test_bare_numeric_id_needs_source():
    with pytest.raises(UsageError, match="ambiguous"):
        parse_target("8863")
    assert parse_target("8863", "hn") == Target("hn", "8863")
    assert parse_target("42", "se", "askubuntu") == Target("se", "42", "askubuntu")
    assert parse_target("42", "se") == Target("se", "42", "stackoverflow")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://www.reddit.com/r/headphones/",
        "https://www.reddit.com/r/x/comments/",
        "https://news.ycombinator.com/news",
        "https://news.ycombinator.com/item?id=abc",
        "https://stackoverflow.com/users/1/x",
        "https://example.com/post/1",
        "not a thread!",
    ],
)
def test_invalid_targets(value):
    with pytest.raises(UsageError):
        parse_target(value)


def test_bare_id_wrong_shape_for_source():
    with pytest.raises(UsageError):
        parse_target("abc", "hn")
    with pytest.raises(UsageError):
        parse_target("a", "reddit")
