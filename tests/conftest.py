import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests off the user's cache file and live Reddit: fresh shared state on a temp DB, guest OAuth unreachable
    (so Reddit code paths exercise the archive fallbacks unless a test installs its own `reddit_oauth._send`)."""
    from forumread import http, state
    from forumread.http import FetchError
    from forumread.sources import reddit_oauth

    def unreachable(method, url, headers, body=None):
        raise FetchError("www.reddit.com: unreachable in tests")

    monkeypatch.setenv("FORUMREAD_CACHE", str(tmp_path / "cache.db"))
    monkeypatch.setattr(reddit_oauth, "_send", unreachable)
    state.reset()
    yield
    http.set_cache(None)
    state.reset()


@pytest.fixture
def fixtures():
    return fixture_json


@pytest.fixture
def fake_http(monkeypatch):
    """Route http.get_json/get_text by URL substring. Values: payload, or callable(url, params) -> payload.
    An exception instance as payload is raised. Unrouted URLs fail the test."""
    from forumread import http

    class Routes(dict):
        calls: list[tuple[str, dict | None]]

    routes = Routes()
    routes["hacker-news.firebaseio.com"] = {}  # HN comment ranking: absent unless a test routes it
    calls = routes.calls = []

    def respond(url, params):
        calls.append((url, params))
        for needle, payload in routes.items():
            if needle in url:
                result = payload(url, params) if callable(payload) else payload
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unrouted request: {url} {params}")

    monkeypatch.setattr(http, "get_json", lambda url, params=None, **kw: respond(url, params))
    monkeypatch.setattr(http, "get_text", lambda url, params=None, **kw: respond(url, params))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return routes
