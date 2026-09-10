"""Credential redaction shared by URL-carrying settings and providers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote_plus, unquote_plus, urlencode

from pydantic_core import MultiHostUrl, Url

MASK = "***"

_USERINFO_RE = re.compile(r"(\A|://|:/|//)([^:/?#]*:)([^/?#]+)(@)")
_MULTI_HOST_USERINFO_RE = re.compile(
    r"(\A|://|:/|//|,)([^:,/?#]*:)"
    r"((?:(?!,[^:,/?#]*:)[^/?#])+)(@)"
)
_AMBIGUOUS_MULTI_HOST_USERINFO_RE = re.compile(
    r"(\A|://|:/|//|,)([^:,/?#]*:)([^,/@?#]+)(?=,[^/?#]*@)"
)
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
_QUALIFIER_SEPARATOR = re.compile(r"[./\[\]]")
_ENCODED_PARAMETER_BOUNDARY = re.compile(r"%(?:2f|3b|26|3f)", re.IGNORECASE)
_QUERY_DELIMITER = re.compile(r"([/?&;])")
_FRAGMENT_DELIMITER = re.compile(r"([/&;])")
_MAX_PORT = 65535
_MAX_PORT_DIGITS = len(str(_MAX_PORT))


def _is_credential_query_key(key: str) -> bool:
    """Return whether `key` conventionally names credential material."""
    key = unquote_plus(key)
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
    return _redact_assignment_fields(query, _QUERY_DELIMITER)


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


def _redact_fragment_path(path: str) -> str:
    """Mask credential assignments embedded in a path-like fragment."""
    return _redact_assignment_fields(path, _FRAGMENT_DELIMITER)


def _redact_assignment_fields(
    value: str,
    delimiter: re.Pattern[str],
) -> str:
    """Mask credential fields without treating encoded separators as endings."""
    parts = delimiter.split(value)
    for index in range(0, len(parts), 2):
        parts[index] = _redact_assignment_segment(parts[index])
    return "".join(parts)


def _redact_assignment_segment(segment: str) -> str:
    """Mask one raw-delimiter-bounded assignment segment."""
    key, separator, _raw_value = segment.partition("=")
    if _is_credential_query_key(key):
        normalized = quote_plus(unquote_plus(key), safe="*")
        return f"{normalized}={MASK}"
    if not separator:
        return segment
    boundaries = tuple(_ENCODED_PARAMETER_BOUNDARY.finditer(segment))
    for index, boundary in enumerate(boundaries):
        field_end = (
            boundaries[index + 1].start()
            if index + 1 < len(boundaries)
            else len(segment)
        )
        field = segment[boundary.end() : field_end]
        embedded_key, embedded_separator, _embedded_value = field.partition("=")
        if not embedded_separator:
            continue
        if _is_credential_query_key(embedded_key):
            return (
                f"{segment[: boundary.start()]}{boundary.group(0)}"
                f"{unquote_plus(embedded_key)}={MASK}"
            )
    return segment


def _redact_fragment(fragment: str | None) -> str | None:
    """Redact credential-like parameters carried in a URL fragment."""
    if not fragment:
        return fragment
    path, separator, parameters = fragment.partition("?")
    safe_path = _redact_fragment_path(path)
    if separator:
        return f"{safe_path}?{_redact_query(parameters)}"
    return safe_path


def _userinfo_pattern(*, multi_host: bool) -> re.Pattern[str]:
    """Return the userinfo grammar for one host or a comma-separated DSN."""
    return _MULTI_HOST_USERINFO_RE if multi_host else _USERINFO_RE


def _redact_unparsed_url(url: str, *, multi_host: bool = False) -> str:
    """Redact credentials without relying on the URL being structurally valid."""
    redacted = _userinfo_pattern(multi_host=multi_host).sub(
        rf"\1\2{MASK}\4", url
    )
    if multi_host:
        redacted = _AMBIGUOUS_MULTI_HOST_USERINFO_RE.sub(
            _redact_ambiguous_multi_host_userinfo, redacted
        )
    before_fragment, fragment_separator, fragment = redacted.partition("#")
    before_query, query_separator, query = before_fragment.partition("?")
    safe_query = _redact_query(query)
    safe_fragment = _redact_fragment(fragment)
    return (
        f"{before_query}{query_separator}{safe_query}"
        f"{fragment_separator}{safe_fragment}"
    )


def _redact_ambiguous_multi_host_userinfo(match: re.Match[str]) -> str:
    """Mask a malformed authority segment that could be password material."""
    candidate = match.group(3)
    host = match.group(2)[:-1]
    if _is_valid_host_port(host, candidate):
        # In multi-host syntax this is the unambiguous host:port form.
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{MASK}"


def _is_valid_host_port(host: str, port: str) -> bool:
    """Return whether one malformed-authority segment is plainly host:port."""
    if not port.isascii() or not port.isdecimal():
        return False
    normalized = port.lstrip("0")
    if len(normalized) > _MAX_PORT_DIGITS or (
        len(normalized) == _MAX_PORT_DIGITS and normalized > str(_MAX_PORT)
    ):
        return False
    number = int(normalized or "0")
    try:
        parsed = MultiHostUrl(f"postgresql://{host}:{number}")
    except ValueError:
        return False
    hosts = parsed.hosts()
    return (
        len(hosts) == 1
        and hosts[0].get("username") is None
        and hosts[0].get("password") is None
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
    pattern = _userinfo_pattern(multi_host=multi_host)
    swept = pattern.sub(rf"\1\2{MASK}\4", url)
    try:
        parsed = MultiHostUrl(swept) if multi_host else Url(swept)
    except ValueError:
        return _redact_unparsed_url(swept, multi_host=multi_host)
    redacted = (
        _redact_multi_host(parsed)
        if isinstance(parsed, MultiHostUrl)
        else _redact_single_host(parsed)
    )
    if redacted is not None:
        if isinstance(parsed, MultiHostUrl):
            return redacted
        return _redact_unparsed_url(redacted, multi_host=multi_host)
    # Structured parsing found nothing. A scheme-less `user:pw@host:port`
    # parses as a path and hides its credential that way, so sweep the
    # original once more. The substitution is a no-op when there is
    # genuinely nothing to redact, which keeps the input string intact.
    return _redact_unparsed_url(swept, multi_host=multi_host)
