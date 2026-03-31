"""Unit tests for tracing context internals."""

import asyncio
from unittest.mock import MagicMock

import pytest_mock

from grelmicro.tracing._context import (
    _merge_context_into,
    _pop_context,
    _push_context,
    add_context,
    get_context,
)
from grelmicro.tracing._instrument import instrument


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
            # Verify it's a copy (mutation doesn't affect original)
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
        """Test add_context updates the current frame."""
        token = _push_context({"a": 1})
        try:
            add_context(b=2)
            assert get_context() == {"a": 1, "b": 2}
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
            "grelmicro.tracing._context._otel_trace.get_current_span",
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
            "grelmicro.tracing._context._otel_trace.get_current_span",
            return_value=mock_span,
        )

        token = _push_context({"a": 1})
        try:
            add_context(key="value")
            mock_span.set_attribute.assert_not_called()
        finally:
            _pop_context(token)


class TestInstrumentNoOtel:
    """Test @instrument when OTel tracer is None."""

    def test_sync_without_otel(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test sync @instrument works when tracer is None."""
        mocker.patch("grelmicro.tracing._instrument.trace", None)

        @instrument
        def process(order_id: str) -> str:  # noqa: ARG001
            return "done"

        assert process("ORD-1") == "done"

    def test_async_without_otel(
        self,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Test async @instrument works when tracer is None."""
        mocker.patch("grelmicro.tracing._instrument.trace", None)

        @instrument
        async def async_process(order_id: str) -> str:  # noqa: ARG001
            return "done"

        assert asyncio.run(async_process("ORD-1")) == "done"
