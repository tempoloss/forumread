"""Detect the source and thread id from a URL or bare id."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

SOURCES = ("reddit", "hn", "se")

_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com"}
_SE_NETWORK_HOSTS = {
    "stackoverflow.com": "stackoverflow",
    "superuser.com": "superuser",
    "serverfault.com": "serverfault",
    "askubuntu.com": "askubuntu",
    "mathoverflow.net": "mathoverflow.net",
}
_REDDIT_ID = re.compile(r"[a-z0-9]{4,10}")


class UsageError(ValueError):
    """The user-supplied target or option is not understood."""


@dataclass(frozen=True)
class Target:
    source: str
    id: str
    site: str | None = None  # Stack Exchange API site parameter


def se_site_for_host(host: str) -> str | None:
    host = host.removeprefix("www.")
    if host in _SE_NETWORK_HOSTS:
        return _SE_NETWORK_HOSTS[host]
    if host.endswith(".stackexchange.com"):
        site = host.removesuffix(".stackexchange.com")
        if site.startswith("meta."):
            site = f"{site.removeprefix('meta.')}.meta"  # API names meta sites "<site>.meta"
        return site or None
    return None


def parse_target(value: str, source: str | None = None, site: str | None = None) -> Target:
    """Resolve `value` (URL or bare id) to a Target. `source`/`site` only matter for bare ids."""
    value = value.strip()
    if not value:
        raise UsageError("empty thread URL or id")
    if "/" in value or "." in value:
        return _from_url(value)
    return _from_bare_id(value, source, site)


def _from_url(value: str) -> Target:
    parts = urlsplit(value if "//" in value else f"https://{value}")
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]

    if host in _REDDIT_HOSTS:
        if "comments" in segments:
            index = segments.index("comments")
            if index + 1 < len(segments) and _REDDIT_ID.fullmatch(segments[index + 1].lower()):
                return Target("reddit", segments[index + 1].lower())
        raise UsageError(f"not a reddit thread URL (expected .../comments/<id>/...): {value}")
    if host == "redd.it":
        if segments and _REDDIT_ID.fullmatch(segments[0].lower()):
            return Target("reddit", segments[0].lower())
        raise UsageError(f"not a redd.it thread URL: {value}")

    if host == "news.ycombinator.com":
        item = parse_qs(parts.query).get("id", [""])[0]
        if parts.path.rstrip("/") == "/item" and item.isdigit():
            return Target("hn", item)
        raise UsageError(f"not a Hacker News item URL (expected /item?id=<n>): {value}")

    site = se_site_for_host(host)
    if site is not None:
        if len(segments) >= 2 and segments[0] in ("questions", "q") and segments[1].isdigit():
            return Target("se", segments[1], site)
        raise UsageError(f"not a Stack Exchange question URL (expected /questions/<id>): {value}")

    raise UsageError(f"unsupported site {host or value!r}; supported: reddit, Hacker News, Stack Exchange")


def _from_bare_id(value: str, source: str | None, site: str | None) -> Target:
    if source is None:
        if value.isdigit():
            raise UsageError(f"ambiguous numeric id {value!r}: pass --source hn or --source se (with --site)")
        source = "reddit"
    if source == "reddit":
        thread_id = value.lower().removeprefix("t3_")
        if not _REDDIT_ID.fullmatch(thread_id):
            raise UsageError(f"not a reddit thread id: {value}")
        return Target("reddit", thread_id)
    if not value.isdigit():
        raise UsageError(f"{source} thread ids are numeric: {value}")
    if source == "hn":
        return Target("hn", value)
    return Target("se", value, site or "stackoverflow")
