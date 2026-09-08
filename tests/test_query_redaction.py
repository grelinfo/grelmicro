"""A credential in a query string never reaches a sink.

The check runs on the request path, through the access log, so it reads the
raw text before it parses it. Reading is cheaper than parsing, and the two
have to agree on every query, which is what these hold.
"""

from __future__ import annotations

import pytest

from grelmicro._redact import _redact_query, _redact_query_values


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
