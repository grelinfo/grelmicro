"""Tests for Health Check Models."""

from grelmicro.health._models import (
    CheckResult,
    HealthReport,
    HealthStatus,
)
from grelmicro.health._types import HealthDetails  # noqa: TC001


def test_health_status_values() -> None:
    """HealthStatus is binary: ok and error."""
    assert HealthStatus.OK == "ok"
    assert HealthStatus.ERROR == "error"


def test_check_result_ok() -> None:
    """CheckResult carries status, critical flag, error, details."""
    result = CheckResult(
        status=HealthStatus.OK,
        critical=True,
        error=None,
        details=None,
    )

    assert result["status"] == HealthStatus.OK
    assert result["critical"] is True
    assert result["error"] is None
    assert result["details"] is None


def test_check_result_error_with_message() -> None:
    """A failing CheckResult carries the error message."""
    result = CheckResult(
        status=HealthStatus.ERROR,
        critical=True,
        error="Connection refused",
        details=None,
    )

    assert result["status"] == HealthStatus.ERROR
    assert result["error"] == "Connection refused"


def test_check_result_with_details() -> None:
    """CheckResult preserves the details dict."""
    details: HealthDetails = {"latency_ms": 1.5, "version": "7.2"}
    result = CheckResult(
        status=HealthStatus.OK,
        critical=True,
        error=None,
        details=details,
    )

    assert result["details"] == details


def test_check_result_non_critical() -> None:
    """critical=False is preserved."""
    result = CheckResult(
        status=HealthStatus.ERROR,
        critical=False,
        error="timeout",
        details=None,
    )

    assert result["critical"] is False


def test_health_report_ok() -> None:
    """HealthReport uses checks keyed by name."""
    report = HealthReport(
        status=HealthStatus.OK,
        checks={
            "db": CheckResult(
                status=HealthStatus.OK,
                critical=True,
                error=None,
                details=None,
            ),
        },
    )

    assert report["status"] == HealthStatus.OK
    assert list(report["checks"]) == ["db"]


def test_health_report_error() -> None:
    """A failing aggregate carries its component in checks."""
    report = HealthReport(
        status=HealthStatus.ERROR,
        checks={
            "db": CheckResult(
                status=HealthStatus.ERROR,
                critical=True,
                error="timeout",
                details=None,
            ),
        },
    )

    assert report["status"] == HealthStatus.ERROR


def test_health_status_serializes_as_string() -> None:
    """HealthStatus values serialize as their string form."""
    assert str(HealthStatus.OK) == "ok"
    assert str(HealthStatus.ERROR) == "error"
