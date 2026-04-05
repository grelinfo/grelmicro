"""Tests for Health Check Models."""

from grelmicro.health._models import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    OverallStatus,
)


def test_health_status_values() -> None:
    """Test that HealthStatus has the expected string values."""
    assert HealthStatus.HEALTHY == "healthy"
    assert HealthStatus.UNHEALTHY == "unhealthy"


def test_overall_status_values() -> None:
    """Test that OverallStatus has the expected string values."""
    assert OverallStatus.HEALTHY == "healthy"
    assert OverallStatus.DEGRADED == "degraded"


def test_component_health_creation() -> None:
    """Test creating a ComponentHealth dict."""
    component = ComponentHealth(
        name="db",
        status=HealthStatus.HEALTHY,
        critical=True,
        error=None,
        details=None,
    )

    assert component["name"] == "db"
    assert component["status"] == HealthStatus.HEALTHY
    assert component["critical"] is True
    assert component["error"] is None
    assert component["details"] is None


def test_component_health_with_error() -> None:
    """Test ComponentHealth with error."""
    component = ComponentHealth(
        name="redis",
        status=HealthStatus.UNHEALTHY,
        critical=True,
        error="Connection refused",
        details=None,
    )

    assert component["status"] == HealthStatus.UNHEALTHY
    assert component["error"] == "Connection refused"


def test_component_health_with_details() -> None:
    """Test ComponentHealth with details."""
    component = ComponentHealth(
        name="redis",
        status=HealthStatus.HEALTHY,
        critical=True,
        error=None,
        details={"latency_ms": 1.5, "version": "7.2"},
    )

    assert component["status"] == HealthStatus.HEALTHY
    assert component["details"] == {"latency_ms": 1.5, "version": "7.2"}


def test_component_health_non_critical() -> None:
    """Test ComponentHealth with critical=False."""
    component = ComponentHealth(
        name="external-api",
        status=HealthStatus.UNHEALTHY,
        critical=False,
        error="timeout",
        details=None,
    )

    assert component["critical"] is False


def test_health_report_healthy() -> None:
    """Test creating a healthy HealthReport."""
    report = HealthReport(
        status=OverallStatus.HEALTHY,
        components=[
            ComponentHealth(
                name="db",
                status=HealthStatus.HEALTHY,
                critical=True,
                error=None,
                details=None,
            ),
        ],
    )

    assert report["status"] == OverallStatus.HEALTHY
    assert len(report["components"]) == 1


def test_health_report_degraded() -> None:
    """Test creating a degraded HealthReport."""
    report = HealthReport(
        status=OverallStatus.DEGRADED,
        components=[
            ComponentHealth(
                name="db",
                status=HealthStatus.HEALTHY,
                critical=True,
                error=None,
                details=None,
            ),
            ComponentHealth(
                name="redis",
                status=HealthStatus.UNHEALTHY,
                critical=True,
                error="timeout",
                details=None,
            ),
        ],
    )

    assert report["status"] == OverallStatus.DEGRADED
    assert [c["name"] for c in report["components"]] == ["db", "redis"]


def test_health_status_serializes_as_string() -> None:
    """Test that HealthStatus values serialize as strings."""
    assert str(HealthStatus.HEALTHY) == "healthy"
    assert str(HealthStatus.UNHEALTHY) == "unhealthy"


def test_overall_status_serializes_as_string() -> None:
    """Test that OverallStatus values serialize as strings."""
    assert str(OverallStatus.HEALTHY) == "healthy"
    assert str(OverallStatus.DEGRADED) == "degraded"
