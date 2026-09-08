"""Credential redaction shared by URL-carrying settings and providers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode

from pydantic_core import MultiHostUrl, Url

MASK = "***"

_USERINFO_RE = re.compile(r"(\A|://|//|,)([^:@/?#]*:)([^@/?#]+)(@)")
_EXACT_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "authorization",
        "code",
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "auth",
        "secret",
        "client_secret",
        "sig",
        "signature",
        "sslpassword",
        "api_key",
        "apikey",
        "key",
    }
)


_CREDENTIAL_QUERY_KEY_PATTERN = re.compile(
    r"(?:^|[_-])(?:credential|key|password|secret|signature|token)(?:$|[_-])",
    re.IGNORECASE,
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_QUALIFIER_SEPARATOR = re.compile(r"[.\[\]]")


def _is_credential_query_key(key: str) -> bool:
    """Return whether `key` conventionally names credential material."""
    lowered = key.lower()
    normalized = _QUALIFIER_SEPARATOR.sub(
        "_", _CAMEL_CASE_BOUNDARY.sub("_", key)
    )
    return (
        lowered in _EXACT_CREDENTIAL_QUERY_KEYS
        or _CREDENTIAL_QUERY_KEY_PATTERN.search(normalized) is not None
    )


def _redact_query(query: str | None) -> str | None:
    """Return `query` with credential-like values replaced by `***`.

    Matches exact credential names and separator-delimited credential
    patterns case-insensitively. Returns the input unchanged when no key
    matches.
    """
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(_is_credential_query_key(key) for key, _value in pairs):
        return query
    redacted_pairs = [
        (key, MASK if _is_credential_query_key(key) else value)
        for key, value in pairs
    ]
    # `safe="*"` keeps the `***` marker readable; other values are
    # properly escaped by `urlencode`.
    return urlencode(redacted_pairs, safe="*")


def _redact_query_values(query: str | None) -> str | None:
    """Return a query with every value masked and parameter names retained."""
    if not query:
        return query
    return urlencode(
        [
            (key, MASK)
            for key, _value in parse_qsl(query, keep_blank_values=True)
        ],
        safe="*",
    )


def _redact_fragment(fragment: str | None) -> str | None:
    """Redact credential-like parameters carried in a URL fragment."""
    if not fragment:
        return fragment
    path, separator, parameters = fragment.partition("?")
    if separator:
        return f"{path}?{_redact_query(parameters)}"
    return _redact_query(fragment)


def _redact_unparsed_url(url: str) -> str:
    """Redact credentials without relying on the URL being structurally valid."""
    redacted = _USERINFO_RE.sub(rf"\1\2{MASK}\4", url)
    before_fragment, fragment_separator, fragment = redacted.partition("#")
    before_query, query_separator, query = before_fragment.partition("?")
    safe_query = _redact_query(query)
    safe_fragment = _redact_fragment(fragment)
    return (
        f"{before_query}{query_separator}{safe_query}"
        f"{fragment_separator}{safe_fragment}"
    )


def _redact_single_host(parsed: Url) -> str | None:
    """Rebuild a single-host URL with its password and query redacted.

    Returns `None` when the URL carries nothing to redact, so the caller
    can hand back the original string untouched.
    """
    redacted_query = _redact_query(parsed.query)
    redacted_fragment = _redact_fragment(parsed.fragment)
    if (
        parsed.password is None
        and redacted_query == parsed.query
        and redacted_fragment == parsed.fragment
    ):
        return None
    return Url.build(
        scheme=parsed.scheme,
        username=parsed.username,
        password=MASK if parsed.password is not None else None,
        host=parsed.host or "",
        port=parsed.port,
        path=parsed.path.lstrip("/") if parsed.path else None,
        query=redacted_query,
        fragment=redacted_fragment,
    ).unicode_string()


def _redact_multi_host(parsed: MultiHostUrl) -> str | None:
    """Rebuild a multi-host URL with every password and the query redacted.

    Returns `None` when the URL carries nothing to redact, so the caller
    can hand back the original string untouched.
    """
    hosts = parsed.hosts()
    redacted_query = _redact_query(parsed.query)
    redacted_fragment = _redact_fragment(parsed.fragment)
    if (
        not any(h.get("password") for h in hosts)
        and redacted_query == parsed.query
        and redacted_fragment == parsed.fragment
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
        fragment=redacted_fragment,
    ).unicode_string()


def redact_url(url: str, *, multi_host: bool = False) -> str:
    """Redact userinfo and credential-like query or fragment values with `***`.

    Tries structured parsing first, then sweeps with a conservative regex
    so a malformed URL, or one whose credential hides in a scheme-less
    `user:password@host:port` form, still cannot leak the password. A URL
    with nothing to redact is returned exactly as it came in. Set
    `multi_host` for URLs that carry several `host:port` pairs, such as a
    Postgres or MongoDB DSN.
    """
    if not url:
        return url
    swept = _USERINFO_RE.sub(rf"\1\2{MASK}\4", url)
    try:
        parsed = MultiHostUrl(swept) if multi_host else Url(swept)
    except ValueError:
        return _redact_unparsed_url(swept)
    redacted = (
        _redact_multi_host(parsed)
        if isinstance(parsed, MultiHostUrl)
        else _redact_single_host(parsed)
    )
    if redacted is not None:
        return _redact_unparsed_url(redacted)
    # Structured parsing found nothing. A scheme-less `user:pw@host:port`
    # parses as a path and hides its credential that way, so sweep the
    # original once more. The substitution is a no-op when there is
    # genuinely nothing to redact, which keeps the input string intact.
    return _redact_unparsed_url(swept)
