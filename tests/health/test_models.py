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
        detail=None,
    )

    assert component["name"] == "db"
    assert component["status"] == HealthStatus.HEALTHY
    assert component["detail"] is None


def test_component_health_with_detail() -> None:
    """Test ComponentHealth with error detail."""
    component = ComponentHealth(
        name="redis",
        status=HealthStatus.UNHEALTHY,
        detail="Connection refused",
    )

    assert component["status"] == HealthStatus.UNHEALTHY
    assert component["detail"] == "Connection refused"


def test_health_report_healthy() -> None:
    """Test creating a healthy HealthReport."""
    report = HealthReport(
        status=OverallStatus.HEALTHY,
        components=[
            ComponentHealth(
                name="db", status=HealthStatus.HEALTHY, detail=None
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
                name="db", status=HealthStatus.HEALTHY, detail=None
            ),
            ComponentHealth(
                name="redis",
                status=HealthStatus.UNHEALTHY,
                detail="timeout",
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
