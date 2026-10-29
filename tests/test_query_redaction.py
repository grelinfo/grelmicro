"""A credential in a query string never reaches a sink.

The check runs on the request path, through the access log, so it reads the
raw text before it parses it. Reading is cheaper than parsing, and the two
have to agree on every query, which is what these hold.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic_core import MultiHostUrl

from grelmicro._redact import (
    MASK,
    _redact_query,
    _redact_query_values,
    _redact_url_fully,
    redact_url,
)


def _postgres_url(value: str) -> str:
    """Build a URL without presenting fake credentials to output scrubbing."""
    return f"postgresql://{value}"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("page=2&sort=asc", "page=2&sort=asc", id="nothing"),
        pytest.param("token=abc&page=2", "token=***&page=2", id="token"),
        pytest.param("TOKEN=abc", "TOKEN=***", id="upper-case"),
        pytest.param("a=1&api_key=x", "a=1&api_key=***", id="not-first"),
        pytest.param("mytoken=x", "mytoken=x", id="not-a-credential-key"),
        pytest.param(
            "refresh_token=x&id_token=y",
            "refresh_token=***&id_token=***",
            id="token-suffixes",
        ),
        pytest.param(
            "X-Amz-Credential=x&X-Amz-Signature=y&X-Amz-Security-Token=z",
            "X-Amz-Credential=***&X-Amz-Signature=***&X-Amz-Security-Token=***",
            id="aws-signed-url",
        ),
        pytest.param(
            "db_password=a&x-api-key=b&private_key=c",
            "db_password=***&x-api-key=***&private_key=***",
            id="qualified-credentials",
        ),
        pytest.param(
            "accessToken=a&clientSecret=b",
            "accessToken=***&clientSecret=***",
            id="camel-case-credentials",
        ),
        pytest.param(
            "db.password=a&client.secret=b&auth[token]=c",
            "db.password=***&client.secret=***&auth%5Btoken%5D=***",
            id="structured-credentials",
        ),
        pytest.param(
            "sslmode=verify-full&sslpassword=secret",
            "sslmode=verify-full&sslpassword=***",
            id="libpq-ssl-password",
        ),
        pytest.param("code=x&sig=y", "code=***&sig=***", id="oauth-and-sas"),
        pytest.param("*_token=x", "*_token=***", id="asterisk-in-key"),
        pytest.param("token", "token=***", id="no-value"),
        # A percent escape decodes to a key the raw text does not show, so
        # the fast path never answers for one.
        pytest.param("%74oken=secret", "token=***", id="percent-encoded"),
        pytest.param("a=%41&b=2", "a=%41&b=2", id="percent-but-innocent"),
    ],
)
def test_a_query_reads_the_same_parsed_or_probed(
    query: str, expected: str
) -> None:
    """The probe and the parse agree, whichever one answers."""
    assert _redact_query(query) == expected


def test_nothing_to_redact_is_returned_as_it_came() -> None:
    """An empty query is not worth reading at all."""
    assert _redact_query("") == ""
    assert _redact_query(None) is None
    assert _redact_query_values("") == ""
    assert _redact_query_values(None) is None


def test_access_log_redaction_masks_every_query_value() -> None:
    """Unknown bearer capabilities cannot pass an access-log denylist."""
    query = (
        "page=2&refresh_token=known&id_token=known&X-Amz-Security-Token=known"
    )

    assert _redact_query_values(query) == (
        "page=***&refresh_token=***&id_token=***&X-Amz-Security-Token=***"
    )


def test_access_log_redaction_keeps_a_parameter_with_a_blank_value() -> None:
    """A parameter sent without a value still shows its name."""
    assert _redact_query_values("debug=&page=2") == "debug=***&page=***"


def test_multi_host_url_redacts_fragment_credential() -> None:
    """Structured multi-host rebuilding masks an OAuth-style fragment."""
    assert (
        redact_url(
            "postgresql://host/db#access_token=sensitive",
            multi_host=True,
        )
        == "postgresql://host/db#access_token=***"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example.test/callback?access_token=FIRST%26part=SECOND",
            "https://example.test/callback?access_token=***",
        ),
        (
            "https://example.test/callback?access_token=FIRST%3bpart=SECOND",
            "https://example.test/callback?access_token=***",
        ),
        (
            "https://example.test/callback?AcCeSs_ToKeN=FIRST%2fpart=SECOND",
            "https://example.test/callback?AcCeSs_ToKeN=***",
        ),
        (
            "https://example.test/#access_token=FIRST%26part=SECOND",
            "https://example.test/#access_token=***",
        ),
        (
            "https://example.test/#/callback?token=FIRST%3Bpart=SECOND",
            "https://example.test/#/callback?token=***",
        ),
        (
            "https://example.test/#/access_token=FIRST%2Fpart=SECOND",
            "https://example.test/#/access_token=***",
        ),
    ],
)
def test_encoded_separators_cannot_end_a_credential_value(
    url: str, expected: str
) -> None:
    """Ambiguous encoded continuation after a credential is masked whole."""
    assert redact_url(url) == expected


def test_embedded_credential_masks_its_ambiguous_encoded_continuation() -> None:
    """An encoded separator can start a credential, never finish one."""
    assert (
        redact_url(
            "https://example.test/?state=ok%26access_token="
            "FIRST%3bpart=SECOND&visible=yes"
        )
        == "https://example.test/?state=ok%26access_token=***&visible=yes"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example.test/#state=ok%3Faccess_token=FRAGMENT_SECRET",
            "https://example.test/#state=ok%3Faccess_token=***",
        ),
        (
            "https://example.test/?redirect=callback%3Faccess_token=QUERY_SECRET",
            "https://example.test/?redirect=callback%3Faccess_token=***",
        ),
        (
            "https://example.test/?redirect=callback%3faccess_token=QUERY_SECRET",
            "https://example.test/?redirect=callback%3faccess_token=***",
        ),
        (
            (
                "https://example.test/?redirect=callback%3Fstate=ok"
                "%26access_token=QUERY_SECRET&visible=yes"
            ),
            (
                "https://example.test/?redirect=callback%3Fstate=ok"
                "%26access_token=***&visible=yes"
            ),
        ),
        (
            (
                "https://example.test/#state=ok%3Fcontinue"
                "%2Fnext%3Btoken=FRAGMENT_SECRET"
            ),
            "https://example.test/#state=ok%3Fcontinue%2Fnext%3Btoken=***",
        ),
        (
            "https://example.test/?redirect=callback%3Fstate=ok",
            "https://example.test/?redirect=callback%3Fstate=ok",
        ),
        (
            "https://example.test/?redirect=callback%3Ftoken%3DLEAKME",
            "https://example.test/?redirect=***",
        ),
    ],
)
def test_encoded_question_marks_start_nested_parameters(
    url: str, expected: str
) -> None:
    """Every encoded boundary is scanned without shortening secret values."""
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql://[::1]:5432,user:pw@db.example:5433/app",
            "postgresql://[::1]:5432,user:***@db.example:5433/app",
        ),
        (
            "postgresql://user:SECRET@[::1]:5432,db.example:5433/app",
            "postgresql://user:***@[::1]:5432,db.example:5433/app",
        ),
        (
            "postgresql://user:SECRET@db.example:5433,[::1]:5432/app",
            "postgresql://user:***@db.example:5433,[::1]:5432/app",
        ),
        (
            "postgresql://db.example:5433,user:SECRET@[2001:db8::1]:5432/app",
            "postgresql://db.example:5433,user:***@[2001:db8::1]:5432/app",
        ),
    ],
)
def test_valid_ipv6_multi_host_urls_remain_structured(
    url: str, expected: str
) -> None:
    """Structured redaction preserves IPv6 hosts in every host position."""
    rendered = redact_url(url, multi_host=True)

    assert rendered == expected
    assert MultiHostUrl(rendered).hosts()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "user:PART1@PART2@collector:4317",
            "user:***@collector:4317",
        ),
        (
            "//user:PART1@PART2@collector:4317",
            "//user:***@collector:4317",
        ),
        (
            _postgres_url("user:PART1@PART2@bad host:4317"),
            _postgres_url("user:***@bad host:4317"),
        ),
    ],
)
def test_ambiguous_userinfo_masks_through_the_last_at_sign(
    url: str, expected: str
) -> None:
    """Malformed userinfo cannot expose a suffix of its password."""
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http:/user:PART1,PART2@bad host/path",
            "http:/user:***@bad host/path",
        ),
        (
            "http:/user:PART1:PART2@PART3@bad host/path",
            "http:/user:***@bad host/path",
        ),
        (
            "http:/user@realm:PWSECRET@bad host/path",
            "http:/user@realm:***@bad host/path",
        ),
        (
            "http:/user:PART1,x:PART2@bad host/path",
            "http:/user:***@bad host/path",
        ),
    ],
)
def test_malformed_scheme_userinfo_is_redacted(url: str, expected: str) -> None:
    """A single slash after a scheme cannot evade fallback redaction."""
    assert redact_url(url) == expected


def test_malformed_multi_host_userinfo_is_redacted() -> None:
    """Every malformed DSN authority is masked without hiding its hosts."""
    assert (
        redact_url(
            "postgresql:/u@realm:PART1@PART2@bad host,u2:OTHER@also bad/db",
            multi_host=True,
        )
        == "postgresql:/u@realm:***@bad host,u2:***@also bad/db"
    )


def test_comma_inside_malformed_multi_host_password_is_redacted() -> None:
    """An ambiguous comma cannot expose a malformed authority password."""
    assert (
        redact_url(
            "postgresql:/u:FIRST,SECOND@bad host,u2:OTHER@also bad/db",
            multi_host=True,
        )
        == "postgresql:/u:***@bad host,u2:***@also bad/db"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql:/u:SECRET1,part:SECRET2@bad host,host2/db",
            "postgresql:/u:***,part:***@bad host,host2/db",
        ),
        (
            "postgresql:/u:SECRET1,a:b,c:d@bad host",
            "postgresql:/u:***,a:***,c:***@bad host",
        ),
        (
            "postgresql:/user@host1:5432,u:SECRET@bad host/db",
            "postgresql:/user@host1:***,u:***@bad host/db",
        ),
    ],
)
def test_ambiguous_multi_host_segments_fail_closed(
    url: str, expected: str
) -> None:
    """Every password-shaped segment is masked when authority syntax is bad."""
    assert redact_url(url, multi_host=True) == expected


@pytest.mark.parametrize("port", ["0", "5432", "9999", "65535", "00065535"])
def test_unambiguous_multi_host_structure_stays_readable(port: str) -> None:
    """Plain hosts and ports survive a malformed credential on another host."""
    assert (
        redact_url(
            f"postgresql:/host1:{port},u:SECRET@bad host/db",
            multi_host=True,
        )
        == f"postgresql:/host1:{port},u:***@bad host/db"
    )


def test_a_one_letter_host_keeps_its_port() -> None:
    """A single-letter host is still a plain host and its port stays readable."""
    assert (
        redact_url("postgresql:/h:5432,u:SECRET@bad host/db", multi_host=True)
        == "postgresql:/h:5432,u:***@bad host/db"
    )


@pytest.mark.parametrize(
    "port",
    [
        "65536",
        "999999",
        "1000000",
        "\uff11\uff12\uff13",
        pytest.param("9" * 5000, id="thousands-of-digits"),
    ],
)
def test_invalid_ambiguous_multi_host_ports_are_masked(port: str) -> None:
    """Out-of-range decimal material is not mistaken for a host port."""
    assert (
        redact_url(
            f"postgresql:/user:{port},other:SECOND_SECRET@bad host/db",
            multi_host=True,
        )
        == "postgresql:/user:***,other:***@bad host/db"
    )


def test_numeric_multi_host_password_is_masked() -> None:
    """A numeric password directly followed by an authority marker is secret."""
    assert (
        redact_url(
            "postgresql:/user:5432@bad host,other:SECOND@also bad/db",
            multi_host=True,
        )
        == "postgresql:/user:***@bad host,other:***@also bad/db"
    )


def test_invalid_host_with_port_shaped_material_is_masked() -> None:
    """A valid port range does not rescue a malformed surrounding host."""
    assert (
        redact_url(
            "postgresql:/bad host:5432,other:SECOND@also bad/db",
            multi_host=True,
        )
        == "postgresql:/bad host:***,other:***@also bad/db"
    )


def test_credential_before_fragment_question_mark_is_redacted() -> None:
    """A query-shaped fragment prefix cannot expose a credential."""
    assert (
        redact_url("https://example.test/#access_token=FRAGSECRET?state=x")
        == "https://example.test/#access_token=***?state=x"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/?access_token=FIRST/LEAKME",
        (
            "https://example.test/?redirect=https%3A%2F%2Fidp.test%2Fcb"
            "%3Faccess_token%3DLEAKME%26state%3Dok"
        ),
        "https://example.test/?access_token%3DLEAKME",
        (
            "https://example.test/?redirect%3Dhttps%3A%2F%2Fuser%3ALEAKME"
            "%40inner.test%2F"
        ),
    ],
)
def test_credential_continuations_and_encoded_assignments_mask_the_enclosing_value(
    url: str,
) -> None:
    """Raw suffixes and fully encoded nested assignments cannot leak."""
    redacted = redact_url(url)

    assert "FIRST" not in redacted
    assert "LEAKME" not in redacted
    assert "***" in redacted


@pytest.mark.parametrize(
    "nested",
    [
        "https%3A%2F%2Finner.test%2F%23token%3DLEAKME",
        "https%3A%2F%2Fuser%3ALEAKME%40inner.test%2F",
        "https%3A%2F%2Finner.test%2F%3Fclient_secret%3DLEAKME",
        "HTTPS%3a%2f%2finner.test%2f%23ToKeN%3dLEAKME",
        "https%3A%2Fuser%3ALEAKME%40inner.test%2F",
        "https%253A%252F%252Finner.test%252F%2523token%253DLEAKME",
        "https%253A%252F%252Finner.test%252F%253Fclient_secret%253DLEAKME",
        (
            "https%253A%252F%252Finner.test%252F%253Fclient_secret%253DLEAKME"
            "%253Fstate%253Dok"
        ),
        "https%253A%252F%252Finner.test%252F%253F%252574oken%253DLEAKME",
    ],
    ids=[
        "fragment",
        "userinfo",
        "query",
        "mixed-case",
        "malformed",
        "double-encoded",
        "double-encoded-query",
        "double-encoded-second-question-mark",
        "double-encoded-encoded-key",
    ],
)
def test_encoded_nested_url_credentials_mask_the_outer_value(
    nested: str,
) -> None:
    """A nested URL is one outer value, so none of its continuation survives."""
    redacted = redact_url(f"https://outer.test/?redirect={nested}&state=ok")

    assert redacted == "https://outer.test/?redirect=***&state=ok"
    assert "LEAKME" not in redacted


@pytest.mark.parametrize(
    "nested",
    [
        "https%3A%2F%2Finner.test%2F%3Fstate%3Dok%23ready",
        "https%253A%252F%252Finner.test%252F%253Fstate%253Dok",
        "https%3A%2Fbroken%25redirect",
    ],
    ids=["single-encoded", "double-encoded", "malformed"],
)
def test_innocent_encoded_nested_urls_are_preserved(nested: str) -> None:
    """Bounded inspection does not rewrite redirects carrying no credential."""
    url = f"https://outer.test/?redirect={nested}&state=ok"

    assert redact_url(url) == url


@pytest.mark.parametrize(
    "nested",
    [
        "https%3A%2F%2Finner.test%2F%3Fstate%3Dok",
        "https%3A%2F%2Finner.test%2F%23ready",
    ],
    ids=["query", "fragment"],
)
def test_innocent_encoded_nested_url_beside_a_credential_is_preserved(
    nested: str,
) -> None:
    """A full redaction keeps a nested URL that carries no credential.

    The credential beside it makes the redaction parse the whole query, so
    the nested URL goes through the full path, not the shortcut.
    """
    # Arrange
    url = f"https://outer.test/?redirect={nested}&token=LEAKME"

    # Act
    redacted = redact_url(url)

    # Assert
    assert redacted == f"https://outer.test/?redirect={nested}&token={MASK}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            "https%3A%2F%2Fidp%2F%3Fnext%3Dhttps%253A%252F%252Fx%252F%253Fcb"
            "%253Dhttps%25253A%25252F%25252Fu%25253Ap%252540c%25252F",
            id="three-level-redirect-chain",
        ),
        pytest.param("faq%253Ftoken%253Dhelp", id="double-encoded-text"),
        pytest.param("faq%3Ftoken%3Dhelp", id="single-encoded-text"),
        pytest.param("note%25252541", id="triple-encoded-innocent"),
    ],
)
def test_a_value_still_encoded_after_two_decodes_is_masked_whole(
    value: str,
) -> None:
    """A value that hides a credential or an escape past two decodes is masked.

    Nested inspection decodes a value at most twice. A credential it reveals
    is masked, and so is a value that still holds a percent-escape after
    the second decode, whatever it carries.
    """
    assert redact_url(f"https://outer.test/?q={value}&state=ok") == (
        f"https://outer.test/?q={MASK}&state=ok"
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            "caf%25C3%25A9",  # codespell:ignore
            id="double-encoded-text",
        ),
        pytest.param("100%2525", id="double-encoded-percent-sign"),
        pytest.param("https%3A%2Fbroken%25redirect", id="invalid-escape"),
        pytest.param(
            "https%3A%2F%2Finner.test%2F%3Fq%3Dcaf%25C3%25A9",
            id="nested-url-with-encoded-value",
        ),
    ],
)
def test_a_value_decoded_within_two_decodes_is_kept_beside_a_credential(
    value: str,
) -> None:
    """A value with no credential and no escape left after two decodes stays.

    The credential beside it makes the redaction parse the whole query, so
    the value goes through the full path, not the shortcut.
    """
    # Arrange
    url = f"https://outer.test/?q={value}&token=LEAKME"

    # Act
    redacted = redact_url(url)

    # Assert
    assert redacted == f"https://outer.test/?q={value}&token={MASK}"


@pytest.mark.parametrize(
    ("url", "multi_host", "expected"),
    [
        pytest.param(
            "https://user:pw@é.test/p",
            False,
            f"https://user:{MASK}@xn--9ca.test/p",
            id="single-host-password",
        ),
        pytest.param(
            "https://user:pw@é.test,h2/p",
            True,
            f"https://user:{MASK}@xn--9ca.test,h2/p",
            id="multi-host-password",
        ),
        pytest.param(
            "https://é.test/p?token=x",
            False,
            f"https://xn--9ca.test/p?token={MASK}",
            id="no-userinfo",
        ),
    ],
)
def test_a_redacted_international_host_renders_in_punycode(
    url: str, *, multi_host: bool, expected: str
) -> None:
    """A rebuilt URL shows an international host in its ASCII form."""
    assert redact_url(url, multi_host=multi_host) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://h/#a#token=x", f"https://h/#a#token={MASK}", id="path"
        ),
        pytest.param(
            "https://h/#a?b#token=x",
            f"https://h/#a?b#token={MASK}",
            id="parameters",
        ),
        pytest.param("https://h/#a#b", "https://h/#a#b", id="innocent"),
        pytest.param(
            "https://h/#a#b?token=x",
            f"https://h/#a#b?token={MASK}",
            id="innocent-beside-a-credential",
        ),
    ],
)
def test_a_later_hash_in_a_fragment_separates_a_credential(
    url: str, expected: str
) -> None:
    """A `#` after the one that opens the fragment starts a new field."""
    assert redact_url(url) == expected


def test_fully_encoded_innocent_assignment_and_empty_url_are_preserved() -> (
    None
):
    """Encoded assignment support does not rewrite non-credential material."""
    url = "https://example.test/?state%3Dok"

    assert redact_url(url) == url
    assert redact_url("") == ""


def test_encoded_innocent_assignment_beside_a_credential_is_preserved() -> None:
    """A full redaction keeps an encoded non-credential assignment as it is.

    The credential beside it makes the redaction parse the whole query, so
    the encoded assignment goes through the full path, not the shortcut.
    """
    # Arrange
    url = "https://example.test/?state%3Dok&token=LEAKME"

    # Act
    redacted = redact_url(url)

    # Assert
    assert redacted == f"https://example.test/?state%3Dok&token={MASK}"


def test_path_prefixed_fragment_credential_is_redacted() -> None:
    """A credential assignment embedded in a fragment path is masked."""
    assert (
        redact_url(
            "https://example.test/#/callback/token=FRAGMENT_VALUE?state=ok"
        )
        == "https://example.test/#/callback/token=***?state=ok"
    )


def test_innocent_fragment_path_assignment_is_preserved() -> None:
    """Fragment path assignments that are not credentials remain readable."""
    assert (
        redact_url("https://example.test/#/callback/state=ok?token=SECRET")
        == "https://example.test/#/callback/state=ok?token=***"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example/#/callback?state=x;token=SECRET",
            "https://example/#/callback?state=x;token=***",
        ),
        (
            "https://example/?state=x/access_token=SECRET",
            "https://example/?state=x/access_token=***",
        ),
        (
            "https://example/#/callback?state=x/access_token=SECRET",
            "https://example/#/callback?state=x/access_token=***",
        ),
        (
            "https://example/?state=x/access%5Ftoken=SECRET",
            "https://example/?state=x/access_token=***",
        ),
        (
            "https://example/?state=x;token=SECRET",
            "https://example/?state=x;token=***",
        ),
        (
            "https://example/#/callback/access%5Ftoken=SECRET",
            "https://example/#/callback/access_token=***",
        ),
        (
            "https://example/#/callback;client_secret=SECRET?state=x",
            "https://example/#/callback;client_secret=***?state=x",
        ),
        (
            "https://example.test/?state=x%2Faccess%5Ftoken=SECRET_VALUE",
            "https://example.test/?state=x%2Faccess_token=***",
        ),
        (
            "https://example.test/#/callback?state=x%3Btoken=SECRET_VALUE",
            "https://example.test/#/callback?state=x%3Btoken=***",
        ),
        (
            "https://example.test/#/callback?state=x%26token=SECRET_VALUE",
            "https://example.test/#/callback?state=x%26token=***",
        ),
        (
            "https://example.test/?state=x%2fAcCeSs%5ftOkEn=SECRET_VALUE",
            "https://example.test/?state=x%2fAcCeSs_tOkEn=***",
        ),
        (
            "https://example/#/callback/state=readable?result=ok",
            "https://example/#/callback/state=readable?result=ok",
        ),
        (
            "https://example/?state=x%2Fcallback=readable%26result=ok",
            "https://example/?state=x%2Fcallback=readable%26result=ok",
        ),
        (
            "https://example/?state=x/callback=readable&result=ok",
            "https://example/?state=x/callback=readable&result=ok",
        ),
        (
            "https://example/?state=x/callback=readable&token=SECRET",
            "https://example/?state=x/callback=readable&token=***",
        ),
        (
            "https://example/?state=x/callback=readable/token=SECRET/next",
            "https://example/?state=x/callback=readable/token=***",
        ),
        (
            "https://example/?state=x/%7Euser/token=SECRET",
            "https://example/?state=x/%7Euser/token=***",
        ),
    ],
)
def test_url_assignment_separators_do_not_expose_credentials(
    url: str, expected: str
) -> None:
    """Query and path-like fragment assignments use the same key policy."""
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    "key",
    [
        "sort_key",
        "partition_key",
        "routing_key",
        "cache_key",
        "shard_key",
        "sortKey",
        "partition.key",
        "sort-key",
        "partition-key",
    ],
)
def test_a_key_that_addresses_a_row_is_not_a_credential(key: str) -> None:
    """A name ending in `key` that holds no secret keeps its value.

    These read as credentials to the broad `*_key` rule, and masking them
    takes away the value an operator reads the log for.
    """
    assert redact_url(f"https://h/p?{key}=name") == f"https://h/p?{key}=name"


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "api-key",
        "x-api-key",
        "session_key",
        "secret_key",
        "signing_key",
        "unknown_key",
        "idempotency_key",
        "idempotency-key",
        "foreign_key",
        "group_key",
        "order_key",
        "primary_key",
    ],
)
def test_an_unrecognised_key_name_is_still_masked(key: str) -> None:
    """Only the settled names are let through, so a new one stays masked.

    `idempotency_key` is here on purpose: a leaked one can replay a
    request, so it is masked whatever the issue that raised it said. The
    four ending in `key` that are not credentials are here too, because
    which names stopped being redacted is the whole risk of this rule.
    """
    assert redact_url(f"https://h/p?{key}=abc") == f"https://h/p?{key}={MASK}"


def test_a_colon_after_a_double_slash_is_masked_wherever_it_sits() -> None:
    """A path colon is masked too, because narrowing the rule leaked.

    `//path:x@y` carries no credential and reading it as one costs a
    diagnostic. Every attempt to exempt it missed a position where a `//`
    really does open one, `?a=1&//u:pw@h` and `#cb//u:pw@h` among them, so
    the rule stays wide. Masking too much is recoverable, printing a
    password is not.
    """
    assert (
        redact_url("https://example.com//path:x@y")
        == f"https://example.com//path:{MASK}@y"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://h/p?next=%20//u:pw@h",
            f"https://h/p?next={MASK}",
            id="nested-behind-encoded-space",
        ),
        pytest.param(
            "https://h/p#//u:pw@host",
            f"https://h/p#//u:{MASK}@host",
            id="fragment",
        ),
        pytest.param(
            "https://h/p?next=//u:pw@h",
            f"https://h/p?next={MASK}",
            id="nested-in-query",
        ),
        pytest.param(
            "https://h/p?a=1&//u:pw@host",
            f"https://h/p?a=1&//u:{MASK}@host",
            id="after-a-query-separator",
        ),
        pytest.param(
            "https://h/p#cb//u:pw@host",
            f"https://h/p#cb//u:{MASK}@host",
            id="inside-a-fragment",
        ),
        pytest.param(
            "https://h/p#/callback//u:pw@host",
            f"https://h/p#/callback//u:{MASK}@host",
            id="deeper-in-a-fragment",
        ),
        pytest.param(
            "https://h/p?next=https://i/?db=u:pw@host",
            f"https://h/p?next={MASK}",
            id="scheme-less-value-in-a-nested-query",
        ),
        pytest.param(
            "https://h/p?next=https%3A%2F%2Fi%2F%3Fdb%3Du%3Apw%40host",
            f"https://h/p?next={MASK}",
            id="scheme-less-value-in-an-encoded-nested-query",
        ),
        pytest.param(
            "https://h/p?next=https%3A%2F%2Fi%2F%23db%3Du%3Apw%40host",
            f"https://h/p?next={MASK}",
            id="scheme-less-value-in-an-encoded-nested-fragment",
        ),
    ],
)
def test_userinfo_is_masked_wherever_a_component_opens(
    url: str, expected: str
) -> None:
    """A `//` that opens a query, a fragment or a nested value still hides.

    Only a `//` in path position carries no credential. Anchoring the
    pattern to the very start of the text would let every one of these
    through, which is a leak rather than a cosmetic difference.
    """
    assert redact_url(url) == expected


def test_multi_host_userinfo_survives_the_path_exception() -> None:
    """Every host in a multi-host DSN still has its password masked."""
    assert (
        redact_url("postgres://u:pw@a:1,v:pw2@b:2/d", multi_host=True)
        == f"postgres://u:{MASK}@a:1,v:{MASK}@b:2/d"
    )


def test_a_protocol_relative_url_still_masks_its_userinfo() -> None:
    """A url that opens with `//` still has userinfo, and it is masked."""
    assert redact_url("//user:pw@host/p") == f"//user:{MASK}@host/p"


@pytest.mark.parametrize(
    ("url", "multi_host", "expected"),
    [
        pytest.param(
            "https://h/p?TOKEN=abc", False, "https://h/p?TOKEN=***", id="upper"
        ),
        pytest.param(
            "https://h/p?Token=abc", False, "https://h/p?Token=***", id="title"
        ),
        pytest.param(
            "https://h/p?accessToken=abc",
            False,
            "https://h/p?accessToken=***",
            id="camel-case",
        ),
        pytest.param(
            "https://h/p?%74oken=abc",
            False,
            "https://h/p?token=***",
            id="percent-encoded-key",
        ),
        pytest.param(
            "https://h/p?%2574oken=abc",
            False,
            "https://h/p?%2574oken=abc",
            id="double-encoded-key",
        ),
        pytest.param(
            "https://h/p?\u212aey=abc",
            False,
            "https://h/p?%E2%84%AAey=***",
            id="kelvin-sign-key",
        ),
        pytest.param(
            "https://h/p?\u017fecret=abc",
            False,
            "https://h/p?%C5%BFecret=***",
            id="long-s-secret",
        ),
        pytest.param(
            "https://h/p#access_token=abc",
            False,
            "https://h/p#access_token=***",
            id="fragment",
        ),
        pytest.param(
            "https://h/p#/cb?code=abc",
            False,
            "https://h/p#/cb?code=***",
            id="fragment-path-query",
        ),
        pytest.param(
            "https://h/p?a=1;token=abc",
            False,
            "https://h/p?a=1;token=***",
            id="semicolon",
        ),
        pytest.param(
            "https://h/p?token=a&token=b",
            False,
            "https://h/p?token=***&token=***",
            id="repeated-key",
        ),
        pytest.param(
            "https://h/p?tok\ten=abc",
            False,
            "https://h/p?token=***",
            id="tab-inside-key",
        ),
        pytest.param(
            "https://h/p?tok%\t65n=abc",
            False,
            "https://h/p?token=***",
            id="tab-inside-escape",
        ),
        pytest.param(
            "https://h/p?next=https%3A%2F%2Fu%3Apw%40x%2F",
            False,
            "https://h/p?next=***",
            id="encoded-nested-userinfo",
        ),
        pytest.param(
            "https://h/p?next=https%253A%252F%252Fu%253Apw%2540x%252F",
            False,
            "https://h/p?next=***",
            id="double-encoded-nested-userinfo",
        ),
        pytest.param(
            "https://h/p#next=https%3A%2F%2Fu%3Apw%40x%2F",
            False,
            "https://h/p#next=***",
            id="encoded-nested-userinfo-in-fragment",
        ),
        pytest.param(
            "https://h/p#/cb?next=https%3A%2F%2Fu%3Apw%40x%2F",
            False,
            "https://h/p#/cb?next=***",
            id="encoded-nested-userinfo-in-fragment-query",
        ),
        pytest.param(
            "postgresql://h?token=abc",
            False,
            "postgresql://h?token=***",
            id="no-path",
        ),
        pytest.param(
            "postgresql://h?token=abc",
            True,
            "postgresql://h?token=***",
            id="multi-host-no-path",
        ),
        pytest.param(
            "https://user:@h/p", False, "https://user:@h/p", id="empty-password"
        ),
        pytest.param(
            "https://user:pw@h/p",
            False,
            f"https://user:{MASK}@h/p",
            id="userinfo",
        ),
        pytest.param(
            "https://user:pw@[::1]:8080/p",
            False,
            f"https://user:{MASK}@[::1]:8080/p",
            id="ipv6-userinfo",
        ),
        pytest.param(
            "https://[::1]:8080/p?page=2",
            False,
            "https://[::1]:8080/p?page=2",
            id="ipv6-clean",
        ),
        pytest.param(
            "mailto:user:pw@example.com",
            False,
            f"mailto:{MASK}@example.com",
            id="scheme-without-slashes",
        ),
        pytest.param(
            "user:pw@host:5432/db",
            False,
            f"user:{MASK}@host:5432/db",
            id="scheme-less",
        ),
        pytest.param(
            "postgresql://u:pw@a:1,b:2/d?sslpassword=x",
            True,
            f"postgresql://u:{MASK}@a:1,b:2/d?sslpassword={MASK}",
            id="multi-host",
        ),
        pytest.param(
            "https://h/p?page=2&limit=50",
            False,
            "https://h/p?page=2&limit=50",
            id="clean-query",
        ),
        pytest.param(
            "https://h/a%20b?q=caf%C3%A9",  # codespell:ignore
            False,
            "https://h/a%20b?q=caf%C3%A9",  # codespell:ignore
            id="clean-percent-encoded",
        ),
        pytest.param(
            "https://h/oauth/authorize?page=2",
            False,
            "https://h/oauth/authorize?page=2",
            id="credential-word-in-path",
        ),
        pytest.param("not a url", False, "not a url", id="not-a-url"),
    ],
)
def test_every_spelling_redacts_the_same_through_the_shortcut(
    url: str, *, multi_host: bool, expected: str
) -> None:
    """A clean URL skips the parse, and no credential spelling slips past."""
    assert redact_url(url, multi_host=multi_host) == expected
    assert _redact_url_fully(url, multi_host=multi_host) == expected


_URL_PIECES = st.sampled_from(
    [
        "https://",
        "postgresql://",
        "mailto:",
        "//",
        ":",
        "@",
        "?",
        "#",
        "&",
        ";",
        "=",
        "/",
        ",",
        "+",
        "%",
        "%25",
        "%3D",
        "%3d",
        "%40",
        "%3F",
        "%2F",
        "%74",
        "%6F",
        "%2574",
        "%E2%84%AA",
        "[::1]",
        "\t",
        "\n",
        " ",
        "token",
        "Token",
        "tok",
        "en",
        "key",
        "\u212a",
        "\u017f",
        "ecret",  # codespell:ignore
        "code",
        "pass",
        "word",
        "sig",
        "auth",
        "api_key",
        "cache_key",
        "credential",
        "pwd",
        "page",
        "h",
        "1",
        "é",
    ]
)


@given(
    pieces=st.lists(_URL_PIECES | st.text(max_size=3), max_size=16),
    multi_host=st.booleans(),
)
@settings(max_examples=2000, deadline=None)
def test_the_shortcut_answers_as_the_full_redaction_would(
    pieces: list[str], *, multi_host: bool
) -> None:
    """Skipping the parse never changes what comes out."""
    url = "".join(pieces)
    assert redact_url(url, multi_host=multi_host) == _redact_url_fully(
        url, multi_host=multi_host
    )
