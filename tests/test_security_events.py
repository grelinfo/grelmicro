"""Tests for the security events authentication writes.

What matters most here is what never reaches a record: a token, a claim from
a token whose signature did not verify, a raw path, or a request value that
could forge a line. Then that every refusal is recorded once, with the route
it was about, whichever framework served it.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from litestar.testing import TestClient as LitestarTestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from starlette.applications import Starlette
from starlette.routing import Route

from grelmicro.http import (
    AuthenticatedRequests,
    AuthenticatedRequestsConfig,
    AuthenticatedRequestsMiddleware,
)
from grelmicro.http._authentication import recorded, refusal_of
from grelmicro.metrics import _hub
from grelmicro.metrics._component import Metrics
from grelmicro.security import (
    ClientBannedError,
    ClientBans,
    ClientBansConfig,
    SigningKeysUnavailableError,
    TokenRejectedError,
    TokenRejectedReason,
    TrustedProxies,
    _events,
)
from grelmicro.security._events import SecurityEvents, _Repeats, encoded
from tests.test_authentication import (
    CALLER,
    FORGER,
    HOUR,
    PROXIES,
    Revocations,
    app_with,
    bearer,
    fastapi_app,
    litestar_app,
    scoped_app,
    token,
    verifier,
    whoami,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping

pytestmark = [pytest.mark.timeout(5)]

LOGGER = "grelmicro.security.events"
ADDRESS = CALLER[0]
HTTP_401 = 401
HTTP_403 = 403
HTTP_429 = 429
HTTP_400 = 400
HTTP_503 = 503


@pytest.fixture
def events(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[list[logging.LogRecord]]:
    """Return the security records written while the test runs."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Keep()
    logger = logging.getLogger(LOGGER)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def metrics() -> Iterator[InMemoryMetricReader]:
    """Activate a `Metrics` component reading into memory."""
    reader = InMemoryMetricReader()
    component = Metrics()
    component._provider = MeterProvider(metric_readers=[reader])
    component._resolved = component._explicit_config
    component._entered = True
    _hub.activate(component)
    try:
        yield reader
    finally:
        _hub.deactivate(component)


def points(
    reader: InMemoryMetricReader, name: str
) -> list[tuple[float, dict[str, Any]]]:
    """Return the data points recorded under `name`."""
    data = reader.get_metrics_data()
    found: list[tuple[float, dict[str, Any]]] = []
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    found.extend(
                        (
                            getattr(point, "value", 0),
                            dict(point.attributes or {}),
                        )
                        for point in metric.data.data_points
                    )
    return found


def field(record: logging.LogRecord, name: str) -> Any:  # noqa: ANN401
    """Return a dotted field of a record, or `None` when it is absent."""
    return record.__dict__.get(name)


class TestRefusalRecord:
    """The record one refusal writes."""

    def test_a_forged_token_writes_one_categorized_record(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A SIEM rule on category and outcome finds it, named and routed."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier())), client=CALLER
        )
        forged = token(FORGER)

        response = client.get(
            "/whoami", headers={**bearer(forged), "user-agent": "curl/8.4"}
        )

        assert response.status_code == HTTP_401
        [record] = events
        assert record.levelno == logging.WARNING
        assert record.getMessage() == "GET /whoami 401 signature"
        assert field(record, "otel.event.name") == (
            "grelmicro.authentication.refused"
        )
        assert field(record, "event.kind") == "event"
        assert field(record, "event.category") == ["authentication"]
        assert field(record, "event.type") == ["start"]
        assert field(record, "event.outcome") == "failure"
        assert field(record, "event.action") == (
            "grelmicro.authentication.refused"
        )
        assert field(record, "error.type") == "signature"
        assert field(record, "http.request.method") == "GET"
        assert field(record, "http.route") == "/whoami"
        assert field(record, "http.response.status_code") == HTTP_401
        assert field(record, "client.address") == ADDRESS
        assert field(record, "user_agent.original") == "curl/8.4"
        assert field(record, "enduser.id") is None
        assert field(record, "grelmicro.security.suppressed") is None
        assert all(forged not in str(value) for value in vars(record).values())

    def test_a_request_with_no_credential_is_written_at_info(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A browser without a token is ordinary."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        client.get("/whoami")

        [record] = events
        assert record.levelno == logging.INFO
        assert field(record, "error.type") == "authentication-required"

    def test_two_credentials_are_recorded_as_ambiguous(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The `400` a caller sending two tokens gets is recorded too."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami",
            headers=[
                ("authorization", f"Bearer {token()}"),
                ("authorization", f"Bearer {token()}"),
            ],
        )

        assert response.status_code == HTTP_400
        [record] = events
        assert field(record, "error.type") == "ambiguous-credentials"
        assert field(record, "http.response.status_code") == HTTP_400

    def test_keys_that_have_not_loaded_are_recorded(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A `503` for missing keys says so."""

        class Unloaded:
            def verify(self, token: str) -> Any:  # noqa: ANN401, ARG002
                msg = "no keys"
                raise SigningKeysUnavailableError(msg)

        client = TestClient(app_with(AuthenticatedRequests(Unloaded())))

        response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_503
        [record] = events
        assert field(record, "error.type") == "signing-keys-unavailable"

    def test_an_authenticated_request_writes_nothing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The access log is where a served request is written."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        client.get("/whoami", headers=bearer(token()))

        assert events == []

    def test_a_path_authentication_leaves_alone_records_nothing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A token sent to an excluded path is never read, so never refused."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier(), exclude=("/livez",)))
        )

        client.get("/livez", headers=bearer(token(FORGER)))

        assert events == []

    def test_a_public_route_sent_no_token_records_nothing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """`Anonymous()` served without a credential is not a refusal."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        client.get("/catalog")

        assert events == []

    def test_an_error_check_raises_is_not_a_refusal(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Only a refusal is recorded, never a failure of the check itself."""

        def broken(caller: Any, scope: Any) -> Any:  # noqa: ANN401, ARG001
            msg = "store down"
            raise RuntimeError(msg)

        client = TestClient(
            app_with(AuthenticatedRequests(verifier(), check=broken)),
            raise_server_exceptions=False,
        )

        client.get("/whoami", headers=bearer(token()))

        assert events == []

    def test_a_websocket_handshake_is_recorded_as_one(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A websocket refusal carries the protocol rather than a method."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        with (
            pytest.raises(Exception),  # noqa: B017, PT011
            client.websocket_connect("/ws", headers=bearer(token(FORGER))),
        ):
            pass  # pragma: no cover

        [record] = events
        assert field(record, "network.protocol.name") == "websocket"
        assert field(record, "http.request.method") is None
        assert record.getMessage() == "WEBSOCKET /ws 401 signature"


class TestRoute:
    """The route a refusal names, never the path it was sent to."""

    def test_fastapi_names_the_template_before_routing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The middleware refused before the router ran, and still names it."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        client.delete("/orders/7", headers=bearer(token(FORGER)))

        [record] = events
        assert field(record, "http.route") == "/orders/{order_id}"
        assert "/orders/7" not in record.getMessage()

    def test_a_method_the_route_does_not_answer_names_it_anyway(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The route a `405` would be about is the one named."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        client.put("/me", headers=bearer(token(FORGER)))

        [record] = events
        assert field(record, "http.route") == "/me"

    def test_a_path_no_route_answers_names_no_route(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Nothing is guessed, and the raw path is not written instead."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        client.get("/nowhere/42", headers=bearer(token(FORGER)))

        [record] = events
        assert field(record, "http.route") is None
        assert record.getMessage() == "GET - 401 signature"

    def test_a_proxy_prefix_goes_back_on_as_the_access_log_writes_it(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The record and the access record name the route the same way."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier())), root_path="/api"
        )

        client.get("/api/whoami", headers=bearer(token(FORGER)))

        [record] = events
        assert field(record, "http.route") == "/api/whoami"

    def test_litestar_names_the_template(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Litestar records the template it routed with."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            client.delete("/orders/7", headers=bearer(token(FORGER)))

        [record] = events
        assert field(record, "http.route") == "/orders/{order_id}"

    def test_a_middleware_added_by_hand_names_no_route(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Without `install` no route was read, and no path is written."""
        app = Starlette(routes=[Route("/whoami", whoami)])
        app.add_middleware(AuthenticatedRequestsMiddleware, verifier=verifier())
        client = TestClient(app)

        client.get("/whoami", headers=bearer(token(FORGER)))

        [record] = events
        assert field(record, "http.route") is None


class TestAuthorization:
    """A missing scope a route refuses."""

    @pytest.mark.parametrize("enduser", [False, True])
    def test_fastapi_records_the_missing_scope(
        self, events: list[logging.LogRecord], *, enduser: bool
    ) -> None:
        """The `403` is an authorization event, naming the caller on opt-in."""
        client = TestClient(
            fastapi_app(AuthenticatedRequests(verifier(), enduser=enduser))
        )

        response = client.delete("/orders/7", headers=bearer(token()))

        assert response.status_code == HTTP_403
        [record] = events
        assert field(record, "otel.event.name") == (
            "grelmicro.authorization.refused"
        )
        assert field(record, "event.category") == ["web"]
        assert field(record, "event.type") == ["access"]
        assert field(record, "error.type") == "insufficient-scope"
        assert field(record, "http.route") == "/orders/{order_id}"
        assert field(record, "enduser.id") == ("user-1" if enduser else None)

    def test_starlette_records_the_missing_scope(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The decorator records what it refuses."""
        client = TestClient(scoped_app(AuthenticatedRequests(verifier())))

        response = client.delete("/async", headers=bearer(token()))

        assert response.status_code == HTTP_403
        [record] = events
        assert field(record, "error.type") == "insufficient-scope"

    def test_litestar_records_the_missing_scope(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The guard records what it refuses."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            response = client.delete("/orders/7", headers=bearer(token()))

        assert response.status_code == HTTP_403
        [record] = events
        assert field(record, "error.type") == "insufficient-scope"
        assert field(record, "http.route") == "/orders/{order_id}"

    def test_a_refusal_raised_outside_authentication_records_nothing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A request authentication never handled has no recorder."""
        error = TokenRejectedError(TokenRejectedReason.EXPIRED)

        assert recorded({"type": "http"}, error) is error
        assert events == []


class TestCaller:
    """Naming the caller, and only one whose token verified."""

    def test_a_revoked_caller_is_named_on_opt_in(
        self, events: list[logging.LogRecord]
    ) -> None:
        """`check` refused a caller whose token verified."""
        revocations = Revocations()
        revocations.revoked.add("t-1")
        client = TestClient(
            app_with(
                AuthenticatedRequests(
                    verifier(), check=revocations, enduser=True
                )
            )
        )

        client.get("/whoami", headers=bearer(token(jti="t-1")))

        [record] = events
        assert field(record, "error.type") == "revoked"
        assert field(record, "enduser.id") == "user-1"

    def test_an_expired_token_names_its_caller_on_opt_in(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Its signature verified, so its subject is the caller's."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier(), enduser=True))
        )

        client.get(
            "/whoami",
            headers=bearer(token(exp=int(time.time()) - HOUR)),
        )

        [record] = events
        assert field(record, "error.type") == "expired"
        assert field(record, "enduser.id") == "user-1"

    def test_a_forged_token_names_nobody(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A claim from a token that did not verify never reaches a record."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier(), enduser=True))
        )

        client.get("/whoami", headers=bearer(token(FORGER, sub="admin")))

        [record] = events
        assert field(record, "enduser.id") is None

    def test_the_caller_is_not_named_without_opt_in(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A subject can be personal data."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        client.get(
            "/whoami",
            headers=bearer(token(exp=int(time.time()) - HOUR)),
        )

        [record] = events
        assert field(record, "enduser.id") is None

    def test_enduser_is_part_of_the_config(self) -> None:
        """`from_config` reads it like every other setting."""
        config = AuthenticatedRequestsConfig(enduser=True)

        component = AuthenticatedRequests.from_config(config, verifier())

        assert component.asgi_middleware()[1]["enduser"] is True


class TestRepeats:
    """Refusals from one address, written once per interval."""

    def test_repeats_are_held_back_and_counted_into_the_next_record(
        self,
        events: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three refusals write one record, and the next says two were held."""
        clock = [1000.0]
        monkeypatch.setattr(_events, "monotonic", lambda: clock[0])
        client = TestClient(
            app_with(AuthenticatedRequests(verifier())), client=CALLER
        )
        forged = bearer(token(FORGER))

        for _ in range(3):
            client.get("/whoami", headers=forged)
        clock[0] += _events.REPEAT_INTERVAL
        client.get("/whoami", headers=forged)

        first, second = events
        assert field(first, "grelmicro.security.suppressed") is None
        assert field(second, "grelmicro.security.suppressed") == 2  # noqa: PLR2004

    def test_the_table_drops_the_address_seen_longest_ago(self) -> None:
        """An attacker with many addresses spends its own history."""
        repeats = _Repeats(60.0, 2)

        assert repeats.admit("a") == 0
        assert repeats.admit("b") == 0
        assert repeats.admit("a") is None
        assert repeats.admit("c") == 0
        assert repeats.admit("a") is None
        assert repeats.admit("b") == 0


class TestBans:
    """The record a ban writes, and the requests it refuses."""

    def test_a_ban_writes_one_record_and_its_refusals_write_none(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The ban is the event, and each refusal it answers is only counted."""
        bans = ClientBans(failures=1, duration=60.0, name="edge")
        client = TestClient(
            app_with(
                AuthenticatedRequests(
                    verifier(), bans=bans, trusted=TrustedProxies(PROXIES)
                )
            ),
            client=CALLER,
        )

        client.get("/whoami", headers=bearer(token(FORGER)))
        refused = client.get("/whoami", headers=bearer(token()))
        client.get("/whoami", headers=bearer(token()))

        assert refused.status_code == HTTP_429
        started = [
            record
            for record in events
            if field(record, "otel.event.name")
            == "grelmicro.client_bans.started"
        ]
        [ban] = started
        assert field(ban, "event.category") == ["intrusion_detection"]
        assert field(ban, "event.type") == ["denied"]
        assert field(ban, "event.outcome") == "success"
        assert field(ban, "client.address") == ADDRESS
        assert field(ban, "grelmicro.client_bans.name") == "edge"
        assert field(ban, "grelmicro.client_bans.failures") == 1
        assert field(ban, "grelmicro.client_bans.duration") == 60.0  # noqa: PLR2004
        assert field(ban, "grelmicro.client_bans.until") is not None
        assert [field(record, "error.type") for record in events] == [
            None,
            "signature",
        ]

    def test_a_ban_extended_while_it_runs_writes_nothing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Only a ban that starts is an event."""
        bans = ClientBans(failures=1, duration=60.0)

        for _ in range(3):
            bans.record(ADDRESS, TokenRejectedReason.SIGNATURE)

        assert len(events) == 1

    def test_a_ban_starting_again_after_it_ran_out_writes_again(
        self,
        events: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A new ban is a new event."""
        from grelmicro.security import bans as module  # noqa: PLC0415

        clock = [1000.0]
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
        bans = ClientBans(failures=1, window=1.0, duration=5.0)

        bans.record(ADDRESS, TokenRejectedReason.SIGNATURE)
        clock[0] += 10.0
        bans.record(ADDRESS, TokenRejectedReason.SIGNATURE)

        assert len(events) == 2  # noqa: PLR2004

    def test_active_counts_the_bans_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ban that ran out is not counted."""
        from grelmicro.security import bans as module  # noqa: PLC0415

        clock = [1000.0]
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
        bans = ClientBans.from_config(
            ClientBansConfig(failures=1, duration=5.0), name="edge"
        )

        bans.record(ADDRESS, TokenRejectedReason.SIGNATURE)
        bans.record("203.0.113.8", TokenRejectedReason.SIGNATURE)
        assert bans.active() == 2  # noqa: PLR2004
        assert bans.name == "edge"
        clock[0] += 10.0
        assert bans.active() == 0

    def test_a_silenced_logger_still_counts_the_ban(
        self, metrics: InMemoryMetricReader
    ) -> None:
        """Silencing the records never loses the counter."""
        logger = logging.getLogger(LOGGER)
        previous = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            ClientBans(failures=1, name="quiet").record(
                ADDRESS, TokenRejectedReason.SIGNATURE
            )
        finally:
            logger.setLevel(previous)

        assert points(metrics, "grelmicro.client_bans.started") == [
            (1, {"grelmicro.client_bans.name": "quiet"})
        ]


class TestMetrics:
    """What the counters and the gauge carry, and never carry."""

    def test_attempts_count_success_and_refusal_without_a_caller(
        self, metrics: InMemoryMetricReader
    ) -> None:
        """No subject and no address ever becomes an attribute."""
        client = TestClient(
            fastapi_app(AuthenticatedRequests(verifier(), enduser=True)),
            client=CALLER,
        )

        client.get("/me", headers=bearer(token()))
        client.get("/me", headers=bearer(token(FORGER)))

        recorded_points = points(metrics, "grelmicro.authentication.attempts")
        assert sorted(recorded_points, key=lambda point: len(point[1])) == [
            (1, {"grelmicro.outcome": "success"}),
            (
                1,
                {
                    "grelmicro.outcome": "refused",
                    "error.type": "signature",
                    "http.route": "/me",
                },
            ),
        ]
        text = repr(recorded_points)
        assert "user-1" not in text
        assert ADDRESS not in text

    def test_active_bans_are_read_when_metrics_are_collected(
        self, metrics: InMemoryMetricReader
    ) -> None:
        """The gauge reports a table's running bans by its name."""
        bans = ClientBans(failures=1, name="gauged")
        bans.record(ADDRESS, TokenRejectedReason.SIGNATURE)

        assert (1, {"grelmicro.client_bans.name": "gauged"}) in points(
            metrics, "grelmicro.client_bans.active"
        )

    def test_a_gauge_registered_while_metrics_run_is_created_at_once(
        self, metrics: InMemoryMetricReader
    ) -> None:
        """A module imported after `Metrics` started still reports."""

        def observe(options: Any) -> Iterator[Any]:  # noqa: ANN401, ARG001
            from opentelemetry.metrics import Observation  # noqa: PLC0415

            yield Observation(7)

        _hub.observe_with("grelmicro.test.late", observe, "1")
        try:
            assert points(metrics, "grelmicro.test.late") == [(7, {})]
        finally:
            _hub._observed.pop("grelmicro.test.late")


class TestSpan:
    """What the current span is told."""

    async def test_a_refusal_marks_the_span_and_leaves_its_status(self) -> None:
        """A `4xx` is not a server error, so status and `error.type` stay unset."""
        exporter, tracer = _tracing()
        middleware = AuthenticatedRequestsMiddleware(
            _served, verifier=verifier(), enduser=True
        )

        with tracer.start_as_current_span("request"):
            await _call(middleware, bearer(token(exp=int(time.time()) - HOUR)))

        [span] = exporter.get_finished_spans()
        assert span.attributes is not None
        assert span.attributes["grelmicro.authentication.refusal"] == "expired"
        assert span.attributes["enduser.id"] == "user-1"
        assert "error.type" not in span.attributes
        assert span.status.status_code is StatusCode.UNSET

    @pytest.mark.parametrize(
        ("enduser", "named"), [(True, "user-1"), (False, None)]
    )
    async def test_an_authenticated_request_names_its_caller_on_opt_in(
        self, *, enduser: bool, named: str | None
    ) -> None:
        """The server span carries `enduser.id` only when asked to."""
        exporter, tracer = _tracing()
        middleware = AuthenticatedRequestsMiddleware(
            _served, verifier=verifier(), enduser=enduser
        )

        with tracer.start_as_current_span("request"):
            await _call(middleware, bearer(token()))

        [span] = exporter.get_finished_spans()
        assert (span.attributes or {}).get("enduser.id") == named

    async def test_no_span_recording_is_no_error(self) -> None:
        """Outside a span, nothing is set and nothing fails."""
        middleware = AuthenticatedRequestsMiddleware(
            _served, verifier=verifier(), enduser=True
        )

        sent = await _call(middleware, bearer(token()))

        assert sent[0]["status"] == 200  # noqa: PLR2004


class TestEncoding:
    """A value from the request never forges a line."""

    def test_control_characters_are_escaped(self) -> None:
        """A line break is written as its escape."""
        assert (
            encoded("a\nb\r\x00\x7f\u2028") == "a\\x0ab\\x0d\\x00\\x7f\\u2028"
        )

    def test_a_long_value_is_cut(self) -> None:
        """The record stays bounded whatever the request sent."""
        value = encoded("x" * 1000)

        assert value == "x" * _events.VALUE_LIMIT + "..."

    def test_a_user_agent_forging_a_line_is_escaped_in_the_record(
        self, events: list[logging.LogRecord]
    ) -> None:
        """The header reaches the record on one line."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        client.get(
            "/whoami",
            headers={**bearer(token(FORGER)), "user-agent": "ok\tfake"},
        )

        [record] = events
        assert field(record, "user_agent.original") == "ok\\x09fake"

    def test_a_subject_is_escaped_too(
        self, events: list[logging.LogRecord]
    ) -> None:
        """A subject is verified, and still never trusted as text."""
        recorder = SecurityEvents(enduser=True)

        recorder.refused(
            {"type": "http", "method": "GET", "headers": []},
            refusal="expired",
            status=401,
            template=None,
            subject="evil\nline",
        )

        [record] = events
        assert field(record, "enduser.id") == "evil\\x0aline"

    def test_a_caller_whose_attribute_raises_names_nobody(self) -> None:
        """Recording a refusal never becomes an error of its own."""

        class Broken:
            @property
            def is_authenticated(self) -> bool:
                raise RuntimeError

        assert _events.subject_of(Broken()) is None
        assert _events.subject_of(object()) is None


class TestWithoutTracing:
    """Nothing to mark, and nobody to name."""

    def test_without_opentelemetry_nothing_is_set_and_nothing_fails(
        self,
        events: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The record is still written when there is no span to mark."""
        monkeypatch.setattr(_events, "_otel", lambda: None)
        recorder = SecurityEvents(enduser=True)

        recorder.refused(
            {"type": "http", "method": "GET", "headers": []},
            refusal="expired",
            status=401,
            template=None,
            subject="user-1",
        )

        [record] = events
        assert field(record, "enduser.id") == "user-1"

    def test_a_caller_with_no_subject_is_not_named_on_the_span(self) -> None:
        """An authenticated caller naming nobody leaves `enduser.id` unset."""
        exporter, tracer = _tracing()
        recorder = SecurityEvents(enduser=True)

        with tracer.start_as_current_span("request"):
            recorder.authenticated(object())

        [span] = exporter.get_finished_spans()
        assert "enduser.id" not in (span.attributes or {})


class TestRefusalOf:
    """The word each refusal is recorded as."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (TokenRejectedError(TokenRejectedReason.EXPIRED), ("expired", 401)),
            (ClientBannedError(retry_after=1.0), ("client-banned", 429)),
            (RuntimeError(), None),
        ],
    )
    def test_each_refusal_has_one_word(
        self, error: BaseException, expected: tuple[str, int] | None
    ) -> None:
        """A rejected token by its reason, anything else by its type."""
        assert refusal_of(error) == expected


def _tracing() -> tuple[InMemorySpanExporter, Any]:
    """Return an exporter and a tracer recording into it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("tests")


async def _served(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
    """Answer `200`."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _call(
    middleware: AuthenticatedRequestsMiddleware, headers: dict[str, str]
) -> list[MutableMapping[str, Any]]:
    """Send one request through `middleware`, and return what it sent."""
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}  # pragma: no cover

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/whoami",
        "root_path": "",
        "headers": [
            (name.encode(), value.encode()) for name, value in headers.items()
        ],
        "query_string": b"",
        "client": CALLER,
    }
    await middleware(scope, receive, send)
    return sent
