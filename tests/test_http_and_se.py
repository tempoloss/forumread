import gzip
import io
import json
import urllib.error

import pytest

from forumread import http
from forumread.sources import stackexchange


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def gz(payload):
    return gzip.compress(json.dumps(payload).encode())


def test_se_gzip_and_html_to_text(monkeypatch, fixtures):
    bodies = {"/answers": fixtures("se_answers.json"), "/questions/1711631?": fixtures("se_question.json")}
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        key = "/answers" if "/answers" in request.full_url else "/questions/1711631?"
        return FakeResponse(gz(bodies[key]))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    thread = stackexchange.fetch_thread("1711631", "stackoverflow")

    assert seen[0].get_header("User-agent").startswith("forumread/0.1")
    assert thread.title == "Improve INSERT-per-second performance of SQLite"
    assert [c.score for c in thread.comments] == [887, 180]
    accepted = thread.comments[0]
    assert accepted.accepted and accepted.author == "Snazzer"
    assert "<p>" not in accepted.body and "<code>" not in accepted.body
    assert "- Put inserts/updates in a transaction." in accepted.body
    assert "`pragma journal_mode`" in accepted.body
    assert "[`SQLITE_STATIC`](https://www.sqlite.org/c3ref/c_static.html)" in thread.comments[1].body


def test_se_error_message_raises(fake_http):
    fake_http["api.stackexchange.com"] = {"error_message": "too many requests", "items": []}
    with pytest.raises(http.FetchError, match="too many requests"):
        stackexchange.fetch_thread("1", "stackoverflow")


def test_se_missing_question(fake_http):
    fake_http["api.stackexchange.com"] = {"items": []}
    with pytest.raises(http.FetchError, match="not found"):
        stackexchange.fetch_thread("1", "stackoverflow")


def test_retry_on_429_then_success(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {}, None)
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert http.get_json("https://example.org/x", {"a": 1}) == {"ok": True}
    assert len(calls) == 3


def test_non_retryable_status_fails_fast(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(http.FetchError, match="HTTP 403"):
        http.get_json("https://example.org/x")
    assert len(calls) == 1
