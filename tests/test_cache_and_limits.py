import io
import json
import threading
import time
import urllib.error

import pytest

from forumread import __main__ as cli
from forumread import http
from forumread.cache import Cache


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


# --- per-host limiter -------------------------------------------------------------------------------------


def test_gate_spaces_request_starts_by_min_interval():
    clock = FakeClock()
    gate = http.HostGate(1, 1.0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        with gate.slot():
            pass
    assert clock.sleeps == [1.0, 1.0]  # the first request starts at once, later ones wait out the interval

    clock.now += 10  # idle for longer than the interval: no wait
    with gate.slot():
        pass
    assert clock.sleeps == [1.0, 1.0]


def test_gate_bounds_concurrency():
    gate = http.HostGate(2)
    active, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal active, peak
        with gate.slot():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


# --- disk cache ---------------------------------------------------------------------------------------------


def test_cache_hit_miss_and_ttl_expiry(tmp_path):
    clock = FakeClock()
    cache = Cache(tmp_path / "c.db", clock=clock)
    assert cache.get("u", 60) is None
    cache.put("u", "body")
    assert cache.get("u", 60) == "body"
    clock.now += 61
    assert cache.get("u", 60) is None  # expired for this lookup's TTL
    assert cache.get("u", 3600) == "body"  # ...but the TTL belongs to the lookup, not the row
    cache.put("u", "newer")
    assert cache.get("u", 60) == "newer"


def test_cache_is_shared_between_instances_like_processes(tmp_path):
    Cache(tmp_path / "c.db").put("u", "body")
    assert Cache(tmp_path / "c.db").get("u", 60) == "body"


def test_unusable_cache_degrades_to_a_miss_with_one_note(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cache = Cache(blocker / "sub" / "c.db")  # parent is a file: cannot be created
    assert cache.get("u", 60) is None
    cache.put("u", "x")
    assert capsys.readouterr().err.count("cache unavailable") == 1


@pytest.fixture
def network(monkeypatch):
    """Fake urlopen returning queued results (bytes or exception); records every request URL."""
    queue, urls = [], []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake_urlopen.queue, fake_urlopen.urls = queue, urls
    return fake_urlopen


def test_get_json_serves_second_call_from_cache(network, tmp_path):
    http.set_cache(Cache(tmp_path / "c.db"))
    network.queue.append(b'{"hits": [1]}')
    assert http.get_json("https://example.org/x", {"a": 1}, cache_ttl=60) == {"hits": [1]}
    assert http.get_json("https://example.org/x", {"a": 1}, cache_ttl=60) == {"hits": [1]}
    assert network.urls == ["https://example.org/x?a=1"]
    network.queue.append(b'{"hits": [2]}')
    assert http.get_json("https://example.org/x", {"a": 2}, cache_ttl=60) == {"hits": [2]}  # key includes params


def test_without_cache_ttl_nothing_is_stored(network, tmp_path):
    http.set_cache(Cache(tmp_path / "c.db"))
    network.queue.extend([b'{"n": 1}', b'{"n": 2}'])
    assert http.get_json("https://example.org/x")["n"] == 1
    assert http.get_json("https://example.org/x")["n"] == 2


def test_error_responses_are_never_cached(network, tmp_path):
    http.set_cache(Cache(tmp_path / "c.db"))
    url = "https://example.org/x"
    network.queue.append(urllib.error.HTTPError(url, 404, "Not Found", {}, None))
    with pytest.raises(http.FetchError):
        http.get_json(url, cache_ttl=60)
    network.queue.append(b"<html>not json</html>")
    with pytest.raises(http.FetchError, match="not JSON"):
        http.get_json(url, cache_ttl=60)
    network.queue.append(b'{"data": null, "error": "Timeout. Maybe slow down a bit"}')  # archive error: 200 + error
    assert http.get_json(url, cache_ttl=60)["error"]
    network.queue.append(b'{"data": []}')  # empty archive page: may be an indexing gap, not worth keeping for hours
    http.get_json(url, cache_ttl=60)
    network.queue.append(b'{"error_message": "too many requests", "items": []}')
    http.get_json(url, cache_ttl=60)
    network.queue.append(b'{"data": [{"id": "a"}]}')
    assert http.get_json(url, cache_ttl=60) == {"data": [{"id": "a"}]}  # every earlier call hit the network
    assert len(network.urls) == 6
    assert http.get_json(url, cache_ttl=60) == {"data": [{"id": "a"}]}
    assert len(network.urls) == 6


def run_cli(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out


def test_cli_cache_and_no_cache_flag(network, capsys):
    item = json.dumps({"id": 7, "type": "story", "author": "a", "title": "T", "points": 1, "created_at_i": 1, "children": []})
    network.queue.extend([item.encode()] * 6)  # each read asks Algolia for the item and the official API for the ranking
    url = "https://news.ycombinator.com/item?id=7"
    assert run_cli(["thread", url], capsys)[0] == 0
    assert run_cli(["thread", url], capsys)[0] == 0
    assert len(network.urls) == 2  # second run read the cache
    assert run_cli(["thread", url, "--no-cache"], capsys)[0] == 0
    assert len(network.urls) == 4
    assert run_cli(["thread", url, "--no-cache"], capsys)[0] == 0
    assert len(network.urls) == 6  # --no-cache never reads the stored response
