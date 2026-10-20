import pathlib

import pytest

from forumread import discover
from forumread.http import FetchError


def test_parse_results_decodes_redirects_and_keeps_threads_only():
    markup = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.reddit.com%2Fr%2FEarbuds%2Fcomments%2F16mb97s%2Fbest%2F&amp;rut=1">Best &amp; budget</a>'
        '<a class="result__snippet" href="//x">I&#x27;m looking</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.reddit.com%2Fr%2FEarbuds%2F&amp;rut=2">Subreddit</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.ycombinator.com%2Fitem%3Fid%3D5&amp;rut=3">HN</a>'
    )
    hits = discover.parse_results(markup)
    assert [(h.source, h.url) for h in hits] == [
        ("reddit", "https://www.reddit.com/r/Earbuds/comments/16mb97s/best/"),
        ("hn", "https://news.ycombinator.com/item?id=5"),
    ]
    assert hits[0].title == "Best & budget" and hits[0].snippet == "I'm looking"


def test_parse_real_sample(fixtures):
    text = (pathlib.Path(__file__).parent / "fixtures" / "ddg.html").read_text(encoding="utf-8")
    hits = discover.parse_results(text)
    assert hits and all(h.url.startswith("https://") and h.source == "reddit" for h in hits)


def test_search_interleaves_domains_and_dedupes(fake_http):
    pages = {
        "reddit.com": '<a class="result__a" href="https://www.reddit.com/r/a/comments/aaaa1/x/">R1</a>',
        "news.ycombinator.com": '<a class="result__a" href="https://news.ycombinator.com/item?id=1">H1</a>',
        "stackoverflow.com": '<a class="result__a" href="https://stackoverflow.com/questions/9/x">S1</a>',
    }
    fake_http["duckduckgo"] = lambda url, params: pages[params["q"].rsplit("site:", 1)[1]]
    result = discover.search("q", 2)
    assert [h.title for h in result.hits] == ["R1", "H1"]
    assert result.via == "duckduckgo (reddit: 1 hit; hn: 1 hit; stackoverflow: 1 hit)"


def test_challenge_falls_back_to_apis_and_reports_per_source_status(fake_http, fixtures, capsys):
    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    fake_http["pullpush.io"] = fixtures("reddit_search.json")
    fake_http["hn.algolia.com"] = fixtures("hn_search.json")
    fake_http["api.stackexchange.com"] = fixtures("se_search.json")
    result = discover.search("earbuds", 6)  # pullpush hits must share a word with the query
    assert [h.source for h in result.hits] == ["reddit", "hn", "se", "reddit", "hn", "se"]
    assert result.via.startswith("API fallback, duckduckgo unavailable (duckduckgo.com: no result list")
    for label in ("reddit (pullpush, lags ~1 week): ", "hn: ", "stackoverflow: "):
        assert f"{label}" in result.via and "hit" in result.via
    assert capsys.readouterr().err == ""


def test_challenge_is_remembered_so_later_queries_skip_duckduckgo(fake_http, fixtures):
    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    fake_http["hn.algolia.com"] = fixtures("hn_search.json")
    fake_http["pullpush.io"] = FetchError("api.pullpush.io: HTTP 429")
    fake_http["api.stackexchange.com"] = FetchError("api.stackexchange.com: HTTP 502")
    discover.search("one", 3)
    discover.search("two", 3)
    assert sum("duckduckgo" in url for url, _ in fake_http.calls) == 1


def test_fallback_tolerates_partial_failure_and_says_which_source_failed(fake_http, fixtures):
    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    fake_http["pullpush.io"] = FetchError("api.pullpush.io: HTTP 429")
    fake_http["hn.algolia.com"] = {"hits": []}
    fake_http["api.stackexchange.com"] = FetchError("api.stackexchange.com: HTTP 502")
    result = discover.search("q", 5)
    assert result.hits == []
    assert "reddit (pullpush, lags ~1 week): error reddit live: " in result.via
    assert "pullpush: api.pullpush.io: HTTP 429" in result.via
    assert "hn: 0 hits" in result.via
    assert "stackoverflow: error api.stackexchange.com: HTTP 502" in result.via
    assert "showing" not in result.via


def test_everything_failing_is_an_error(fake_http):
    fake_http["duckduckgo"] = "<html>anomaly challenge</html>"
    for host in ("pullpush.io", "hn.algolia.com", "api.stackexchange.com"):
        fake_http[host] = FetchError(f"{host}: HTTP 500")
    with pytest.raises(FetchError, match="web search failed"):
        discover.search("q", 5)


def test_ddg_failure_after_first_domain_keeps_earlier_results(fake_http, capsys):
    def respond(url, params):
        if "site:reddit.com" in params["q"]:
            return '<a class="result__a" href="https://www.reddit.com/r/a/comments/aaaa1/x/">R1</a>'
        return "<html>anomaly challenge</html>"

    fake_http["duckduckgo"] = respond
    result = discover.search("q", 5)
    assert [h.title for h in result.hits] == ["R1"]
    assert len(fake_http.calls) == 2  # reddit ok, hn challenged, stackoverflow never queried
    assert result.via.startswith("duckduckgo (reddit: 1 hit; hn: error duckduckgo.com: no result list")
    assert capsys.readouterr().err == ""
