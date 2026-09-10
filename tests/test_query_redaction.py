"""A credential in a query string never reaches a sink.

The check runs on the request path, through the access log, so it reads the
raw text before it parses it. Reading is cheaper than parsing, and the two
have to agree on every query, which is what these hold.
"""

from __future__ import annotations

import pytest
from pydantic_core import MultiHostUrl

from grelmicro._redact import _redact_query, _redact_query_values, redact_url


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
    ],
)
def test_ambiguous_multi_host_segments_fail_closed(
    url: str, expected: str
) -> None:
    """Every password-shaped segment is masked when authority syntax is bad."""
    assert redact_url(url, multi_host=True) == expected


@pytest.mark.parametrize("port", ["0", "5432", "65535"])
def test_unambiguous_multi_host_structure_stays_readable(port: str) -> None:
    """Plain hosts and ports survive a malformed credential on another host."""
    assert (
        redact_url(
            f"postgresql:/host1:{port},u:SECRET@bad host/db",
            multi_host=True,
        )
        == f"postgresql:/host1:{port},u:***@bad host/db"
    )


@pytest.mark.parametrize("port", ["65536", "999999", "\uff11\uff12\uff13"])
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


def test_fully_encoded_innocent_assignment_and_empty_url_are_preserved() -> (
    None
):
    """Encoded assignment support does not rewrite non-credential material."""
    url = "https://example.test/?state%3Dok"

    assert redact_url(url) == url
    assert redact_url("") == ""


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
    ],
)
def test_url_assignment_separators_do_not_expose_credentials(
    url: str, expected: str
) -> None:
    """Query and path-like fragment assignments use the same key policy."""
    assert redact_url(url) == expected
