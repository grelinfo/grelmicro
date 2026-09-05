"""Credential redaction shared by URL-carrying settings and providers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode

from pydantic_core import MultiHostUrl, Url

MASK = "***"

_USERINFO_RE = re.compile(r"(\A|://|,)([^@/?#]*:)([^@/?#]+)(@)")
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "auth",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "key",
    }
)


_CREDENTIAL_QUERY_PROBE = re.compile(
    r"(?:\A|&)(?:" + "|".join(sorted(_CREDENTIAL_QUERY_KEYS)) + r")(?:=|&|\Z)",
    re.IGNORECASE,
)
"""Whether a query string is worth parsing to redact.

Parsing every query to find the ones that carry nothing costs more than
reading them, and a query string is on the request path. The probe reads
the raw text, so it only answers for text that means what it says: a query
carrying a percent escape is parsed, because `%74oken=` decodes to a key
this would not have seen.
"""


def _redact_query(query: str | None) -> str | None:
    """Return `query` with credential-like values replaced by `***`.

    Matches keys case-insensitively against `_CREDENTIAL_QUERY_KEYS`.
    Returns the input unchanged when no key matches.
    """
    if not query:
        return query
    if "%" not in query and not _CREDENTIAL_QUERY_PROBE.search(query):
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(k.lower() in _CREDENTIAL_QUERY_KEYS for k, _ in pairs):
        return query
    redacted_pairs = [
        (k, MASK if k.lower() in _CREDENTIAL_QUERY_KEYS else v)
        for k, v in pairs
    ]
    # `safe="*"` keeps the `***` marker readable; other values are
    # properly escaped by `urlencode`.
    return urlencode(redacted_pairs, safe="*")


def _redact_single_host(parsed: Url) -> str | None:
    """Rebuild a single-host URL with its password and query redacted.

    Returns `None` when the URL carries nothing to redact, so the caller
    can hand back the original string untouched.
    """
    redacted_query = _redact_query(parsed.query)
    if parsed.password is None and redacted_query == parsed.query:
        return None
    return Url.build(
        scheme=parsed.scheme,
        username=parsed.username,
        password=MASK if parsed.password is not None else None,
        host=parsed.host or "",
        port=parsed.port,
        path=parsed.path.lstrip("/") if parsed.path else None,
        query=redacted_query,
        fragment=parsed.fragment,
    ).unicode_string()


def _redact_multi_host(parsed: MultiHostUrl) -> str | None:
    """Rebuild a multi-host URL with every password and the query redacted.

    Returns `None` when the URL carries nothing to redact, so the caller
    can hand back the original string untouched.
    """
    hosts = parsed.hosts()
    redacted_query = _redact_query(parsed.query)
    if (
        not any(h.get("password") for h in hosts)
        and redacted_query == parsed.query
    ):
        return None
    redacted_hosts: list[Any] = []
    for h in hosts:
        entry: dict[str, Any] = {"host": h.get("host") or ""}
        if h.get("username"):
            entry["username"] = h["username"]
        if h.get("password"):
            entry["password"] = MASK
        port = h.get("port")
        if port is not None:
            entry["port"] = port
        redacted_hosts.append(entry)
    return MultiHostUrl.build(
        scheme=parsed.scheme,
        hosts=redacted_hosts,
        path=parsed.path.lstrip("/") if parsed.path else None,
        query=redacted_query,
        fragment=parsed.fragment,
    ).unicode_string()


def redact_url(url: str, *, multi_host: bool = False) -> str:
    """Redact userinfo password and credential-like query values with `***`.

    Tries structured parsing first, then sweeps with a conservative regex
    so a malformed URL, or one whose credential hides in a scheme-less
    `user:password@host:port` form, still cannot leak the password. A URL
    with nothing to redact is returned exactly as it came in. Set
    `multi_host` for URLs that carry several `host:port` pairs, such as a
    Postgres or MongoDB DSN.
    """
    if not url:
        return url
    try:
        parsed = MultiHostUrl(url) if multi_host else Url(url)
    except ValueError:
        return _USERINFO_RE.sub(rf"\1\2{MASK}\4", url)
    redacted = (
        _redact_multi_host(parsed)
        if isinstance(parsed, MultiHostUrl)
        else _redact_single_host(parsed)
    )
    if redacted is not None:
        return redacted
    # Structured parsing found nothing. A scheme-less `user:pw@host:port`
    # parses as a path and hides its credential that way, so sweep the
    # original once more. The substitution is a no-op when there is
    # genuinely nothing to redact, which keeps the input string intact.
    return _USERINFO_RE.sub(rf"\1\2{MASK}\4", url)
