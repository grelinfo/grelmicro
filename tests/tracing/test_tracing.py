"""Parametrized Tests for Tracing Module.

Tests verify that @instrument, span(), and add_context() produce
consistent log output across all three logging backends.
"""

import asyncio

import pytest

from grelmicro.logging import configure_logging
from grelmicro.tracing import add_context, get_context, instrument, span
from grelmicro.tracing._context import _context_stack
from tests.logging.conftest import log_message, parse_json_log, parse_json_logs

BACKENDS = ["loguru", "structlog", "stdlib"]


def _setup_json_logging(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setenv("LOG_BACKEND", backend)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_OTEL_ENABLED", "false")
    configure_logging()


class TestInstrument:
    """Test @instrument decorator across all backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_captures_args(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument captures function arguments in log output."""
        _setup_json_logging(monkeypatch, backend)

        @instrument
        def process(order_id: str, user_id: str) -> None:  # noqa: ARG001
            log_message(backend, "processing")

        process("ORD-1", "USR-1")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "processing"
        assert log_record["order_id"] == "ORD-1"
        assert log_record["user_id"] == "USR-1"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_skip(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument(skip=...) excludes specified args."""
        _setup_json_logging(monkeypatch, backend)

        @instrument(skip={"password"})
        def login(username: str, password: str) -> None:  # noqa: ARG001
            log_message(backend, "login attempt")

        login("alice", "secret123")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["username"] == "alice"
        assert "password" not in log_record

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_skip_all(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument(skip_all=True) excludes all args."""
        _setup_json_logging(monkeypatch, backend)

        @instrument(skip_all=True)
        def bulk(payload: bytes) -> None:  # noqa: ARG001
            log_message(backend, "processing")

        bulk(b"data")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["msg"] == "processing"
        assert "payload" not in log_record

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_custom_name(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument(name=...) with custom span name."""
        _setup_json_logging(monkeypatch, backend)

        @instrument(name="db.query")
        def fetch(user_id: str) -> None:  # noqa: ARG001
            log_message(backend, "fetching")

        fetch("USR-1")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["user_id"] == "USR-1"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_context_cleanup(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test context is cleaned up after @instrument exits."""
        _setup_json_logging(monkeypatch, backend)

        @instrument
        def inner(order_id: str) -> None:  # noqa: ARG001
            log_message(backend, "inside")

        inner("ORD-1")
        log_message(backend, "outside")

        logs = parse_json_logs(capsys.readouterr().out)
        assert logs[0]["order_id"] == "ORD-1"
        assert "order_id" not in logs[1]

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_async(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument works with async functions."""
        _setup_json_logging(monkeypatch, backend)

        @instrument
        async def async_process(order_id: str) -> str:  # noqa: ARG001
            log_message(backend, "async processing")
            return "done"

        result = asyncio.run(async_process("ORD-1"))

        assert result == "done"
        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["order_id"] == "ORD-1"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_skips_self(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument skips 'self' parameter."""
        _setup_json_logging(monkeypatch, backend)

        class Service:
            @instrument
            def process(self, order_id: str) -> None:  # noqa: ARG002
                log_message(backend, "processing")

        Service().process("ORD-1")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["order_id"] == "ORD-1"
        assert "self" not in log_record

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_skips_cls(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument skips 'cls' parameter on classmethod."""
        _setup_json_logging(monkeypatch, backend)

        class Service:
            @classmethod
            @instrument
            def process(cls, order_id: str) -> None:  # noqa: ARG003
                log_message(backend, "processing")

        Service.process("ORD-1")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["order_id"] == "ORD-1"
        assert "cls" not in log_record


class TestSpan:
    """Test span() context manager across all backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_adds_context(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test span() adds fields to log output."""
        _setup_json_logging(monkeypatch, backend)

        with span("db_query", table="users"):
            log_message(backend, "querying")

        log_record = parse_json_log(capsys.readouterr().out)
        assert log_record["table"] == "users"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_cleanup(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test span context is removed after exit."""
        _setup_json_logging(monkeypatch, backend)

        with span("db_query", table="users"):
            log_message(backend, "inside")
        log_message(backend, "outside")

        logs = parse_json_logs(capsys.readouterr().out)
        assert logs[0]["table"] == "users"
        assert "table" not in logs[1]

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_nested(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test nested spans accumulate and pop context correctly."""
        _setup_json_logging(monkeypatch, backend)

        with span("outer", request_id="REQ-1"):
            log_message(backend, "outer")
            with span("inner", table="users"):
                log_message(backend, "inner")
            log_message(backend, "back to outer")

        logs = parse_json_logs(capsys.readouterr().out)
        assert logs[0]["request_id"] == "REQ-1"
        assert "table" not in logs[0]
        assert logs[1]["request_id"] == "REQ-1"
        assert logs[1]["table"] == "users"
        assert logs[2]["request_id"] == "REQ-1"
        assert "table" not in logs[2]

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_with_instrument(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test @instrument combined with span()."""
        _setup_json_logging(monkeypatch, backend)

        @instrument
        def process(order_id: str) -> None:  # noqa: ARG001
            log_message(backend, "started")
            with span("payment", provider="stripe"):
                log_message(backend, "charging")
            log_message(backend, "done")

        process("ORD-1")

        logs = parse_json_logs(capsys.readouterr().out)
        assert logs[0]["order_id"] == "ORD-1"
        assert "provider" not in logs[0]
        assert logs[1]["order_id"] == "ORD-1"
        assert logs[1]["provider"] == "stripe"
        assert logs[2]["order_id"] == "ORD-1"
        assert "provider" not in logs[2]


class TestAddContext:
    """Test add_context() across all backends."""

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_enriches_logs(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test add_context() adds fields to subsequent log records."""
        _setup_json_logging(monkeypatch, backend)

        @instrument
        def process(order_id: str) -> None:  # noqa: ARG001
            log_message(backend, "before")
            add_context(payment_status="pending")
            log_message(backend, "after")

        process("ORD-1")

        logs = parse_json_logs(capsys.readouterr().out)
        assert "payment_status" not in logs[0]
        assert logs[1]["payment_status"] == "pending"

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_persists_after_span(
        self,
        backend: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test add_context() fields persist after nested span exits."""
        _setup_json_logging(monkeypatch, backend)

        @instrument
        def process(order_id: str) -> None:  # noqa: ARG001
            add_context(status="active")
            with span("db", table="users"):
                log_message(backend, "inside span")
            log_message(backend, "after span")

        process("ORD-1")

        logs = parse_json_logs(capsys.readouterr().out)
        assert logs[0]["status"] == "active"
        assert logs[0]["table"] == "users"
        assert logs[1]["status"] == "active"
        assert "table" not in logs[1]

    def test_outside_span_is_noop(self) -> None:
        """Test add_context() is a no-op when called outside a span."""
        add_context(orphan="value")
        assert get_context() == {}


class TestContextIsolation:
    """Test context isolation and cleanup."""

    def test_context_stack_empty_by_default(self) -> None:
        """Test context stack is empty by default."""
        assert _context_stack.get() == ()
        assert get_context() == {}

    def test_cleanup_on_instrument_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reset_backend: None,  # noqa: ARG002
    ) -> None:
        """Test context is cleaned up even when function raises."""
        _setup_json_logging(monkeypatch, "stdlib")

        @instrument
        def failing(order_id: str) -> None:  # noqa: ARG001
            msg = "boom"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="boom"):
            failing("ORD-1")

        assert get_context() == {}

    def test_cleanup_on_span_exception(self) -> None:
        """Test span context is cleaned up even when body raises."""

        def _raise_in_span() -> None:
            with span("test", key="value"):
                msg = "boom"
                raise ValueError(msg)

        with pytest.raises(ValueError, match="boom"):
            _raise_in_span()

        assert get_context() == {}

    def test_concurrent_task_isolation(self) -> None:
        """Test add_context in one async task does not affect another."""
        results: dict[str, str | None] = {}

        @instrument
        async def worker(name: str) -> None:
            await asyncio.sleep(0)
            add_context(worker_name=name)
            await asyncio.sleep(0)
            results[name] = get_context().get("worker_name")

        async def run() -> None:
            await asyncio.gather(worker("A"), worker("B"))

        asyncio.run(run())

        assert results["A"] == "A"
        assert results["B"] == "B"
