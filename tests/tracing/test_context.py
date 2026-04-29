"""Unit tests for tracing context internals."""

import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_mock

from grelmicro._context import (
    merge_context_into as _merge_context_into,
)
from grelmicro._context import (
    pop_context as _pop_context,
)
from grelmicro._context import (
    push_context as _push_context,
)
from grelmicro.trace._context import (
    add_context,
    get_context,
)
from grelmicro.trace._instrument import _record_exception, instrument
from grelmicro.trace._span import span as tracing_span


class TestGetContext:
    """Test get_context with different stack depths."""

    def test_empty_stack(self) -> None:
        """Test empty stack returns empty dict."""
        assert get_context() == {}

    def test_single_frame(self) -> None:
        """Test single frame returns copy."""
        token = _push_context({"a": 1})
        try:
            ctx = get_context()
            assert ctx == {"a": 1}
            ctx["b"] = 2
            assert get_context() == {"a": 1}
        finally:
            _pop_context(token)

    def test_multiple_frames_merged(self) -> None:
        """Test multiple frames are merged bottom to top."""
        t1 = _push_context({"a": 1})
        t2 = _push_context({"b": 2})
        t3 = _push_context({"c": 3})
        try:
            assert get_context() == {"a": 1, "b": 2, "c": 3}
        finally:
            _pop_context(t3)
            _pop_context(t2)
            _pop_context(t1)

    def test_later_frames_override_earlier(self) -> None:
        """Test later frames override earlier ones."""
        t1 = _push_context({"key": "first"})
        t2 = _push_context({"key": "second"})
        try:
            assert get_context()["key"] == "second"
        finally:
            _pop_context(t2)
            _pop_context(t1)


class TestMergeContextInto:
    """Test _merge_context_into for hot path."""

    def test_empty_stack_no_mutation(self) -> None:
        """Test empty stack doesn't modify target."""
        target: dict[str, object] = {"existing": "value"}
        _merge_context_into(target)
        assert target == {"existing": "value"}

    def test_merges_into_existing_dict(self) -> None:
        """Test context is merged into target dict."""
        token = _push_context({"a": 1})
        try:
            target: dict[str, object] = {"existing": "value"}
            _merge_context_into(target)
            assert target == {"existing": "value", "a": 1}
        finally:
            _pop_context(token)


class TestAddContext:
    """Test add_context with OTel integration."""

    def test_outside_span_is_noop(self) -> None:
        """Test add_context outside span does nothing."""
        add_context(key="value")
        assert get_context() == {}

    def test_updates_current_frame(self) -> None:
        """Test add_context creates new frame with updated fields."""
        token = _push_context({"a": 1})
        try:
            add_context(b=2)
            assert get_context() == {"a": 1, "b": 2}
        finally:
            _pop_context(token)

    def test_does_not_mutate_original_frame(self) -> None:
        """Test add_context replaces frame, not mutates it."""
        original = {"a": 1}
        token = _push_context(original)
        try:
            add_context(b=2)
            assert original == {"a": 1}
        finally:
            _pop_context(token)

    def test_sets_otel_span_attributes(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test add_context sets attributes on active OTel span."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        mocker.patch(
            "grelmicro.trace._context._otel_trace.get_current_span",
            return_value=mock_span,
        )

        token = _push_context({"a": 1})
        try:
            add_context(payment_id="PAY-1", status="ok")
            mock_span.set_attribute.assert_any_call("payment_id", "PAY-1")
            mock_span.set_attribute.assert_any_call("status", "ok")
        finally:
            _pop_context(token)

    def test_skips_otel_when_not_recording(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test add_context skips OTel when span is not recording."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = False

        mocker.patch(
            "grelmicro.trace._context._otel_trace.get_current_span",
            return_value=mock_span,
        )

        token = _push_context({"a": 1})
        try:
            add_context(key="value")
            mock_span.set_attribute.assert_not_called()
        finally:
            _pop_context(token)


class TestExceptionRecording:
    """Test exception recording on OTel spans."""

    def test_instrument_sync_records_exception(self) -> None:
        """Test sync @instrument records exception on OTel span."""

        @instrument
        def failing(order_id: str) -> None:  # noqa: ARG001
            msg = "sync boom"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="sync boom"):
            failing("ORD-1")

        assert get_context() == {}

    def test_instrument_async_records_exception(self) -> None:
        """Test async @instrument records exception on OTel span."""

        @instrument
        async def async_failing(order_id: str) -> None:  # noqa: ARG001
            msg = "async boom"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="async boom"):
            asyncio.run(async_failing("ORD-1"))

        assert get_context() == {}

    def test_span_records_exception(self) -> None:
        """Test span() records exception on OTel span."""

        def _raise_in_span() -> None:
            with tracing_span("test", key="value"):
                msg = "span boom"
                raise ValueError(msg)

        with pytest.raises(ValueError, match="span boom"):
            _raise_in_span()

        assert get_context() == {}

    def test_record_exception_helper(self) -> None:
        """Test _record_exception sets status and records exception."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        try:
            msg = "test error"
            raise ValueError(msg)  # noqa: TRY301
        except ValueError:
            _record_exception(mock_span)

        mock_span.set_status.assert_called_once()
        mock_span.record_exception.assert_called_once()

    def test_record_exception_skips_non_recording(self) -> None:
        """Test _record_exception skips non-recording span."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = False

        try:
            msg = "test"
            raise ValueError(msg)  # noqa: TRY301
        except ValueError:
            _record_exception(mock_span)

        mock_span.set_status.assert_not_called()


class TestExtractFieldsFallback:
    """Test _extract_fields graceful fallback."""

    def test_bind_failure_returns_empty_context(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test _extract_fields returns {} when sig.bind raises."""
        mocker.patch(
            "grelmicro.trace._instrument.inspect.signature",
            return_value=MagicMock(
                bind=MagicMock(side_effect=TypeError("bad bind"))
            ),
        )

        @instrument
        def process(order_id: str) -> str:  # noqa: ARG001
            return "empty" if not get_context() else "has_context"

        assert process("ORD-1") == "empty"


class TestSpanExceptionRecording:
    """Test exception recording in span() context manager."""

    def test_span_exception_is_recorded(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test span() records exception on OTel span when body raises."""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )

        mocker.patch(
            "grelmicro.trace._span._otel_trace.get_tracer",
            return_value=mock_tracer,
        )

        def _raise_in_span() -> None:
            with tracing_span("test", key="val"):
                msg = "span error"
                raise ValueError(msg)

        with pytest.raises(ValueError, match="span error"):
            _raise_in_span()

        mock_span.set_status.assert_called_once()
        mock_span.record_exception.assert_called_once()


class TestInstrumentNoOtel:
    """Test @instrument when OTel tracer is None."""

    def test_sync_without_otel(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test sync @instrument works when tracer is None."""
        mocker.patch("grelmicro.trace._instrument.trace", None)

        @instrument
        def process(order_id: str) -> str:  # noqa: ARG001
            return "done"

        assert process("ORD-1") == "done"

    def test_async_without_otel(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test async @instrument works when tracer is None."""
        mocker.patch("grelmicro.trace._instrument.trace", None)

        @instrument
        async def async_process(order_id: str) -> str:  # noqa: ARG001
            return "done"

        assert asyncio.run(async_process("ORD-1")) == "done"
