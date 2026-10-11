"""
Tests for the P0 severity-floor fix: deterministic minimum severity for safety-critical rules.

Covers `tools/severity.severity_floor()` in isolation (pure function) and end-to-end through
`orchestrator.run_incident()` to prove that a model returning LOW for a GPU ECC breach still
results in CRITICAL severity and ESCALATE routing.
"""

from datetime import datetime, timezone

import pytest

from agents.schemas import RoutingDecision, Severity
from tools.metrics import Domain, GPUMetrics
from tools.severity import severity_floor
from tools.thresholds import BreachedRule, DetectionResult


def _detection(domain: Domain, rules: list[BreachedRule]) -> DetectionResult:
    """Helper to build a DetectionResult with the given breached rules."""
    return DetectionResult(
        domain=domain,
        is_anomalous=len(rules) > 0,
        breached_rules=rules,
        evaluated_at=datetime.now(timezone.utc),
    )


def _ecc_breach() -> BreachedRule:
    return BreachedRule(rule_name="ecc_errors", observed_value=3.0, threshold=0.0)


def _gpu_temp_breach() -> BreachedRule:
    return BreachedRule(rule_name="gpu_temp_c", observed_value=92.0, threshold=85.0)


def _db_connection_breach() -> BreachedRule:
    return BreachedRule(
        rule_name="connection_pool_utilization_pct", observed_value=95.0, threshold=85.0
    )


# --- Unit tests for severity_floor() ---


@pytest.mark.parametrize(
    "model_severity, expected",
    [
        (Severity.LOW, Severity.CRITICAL),
        (Severity.MEDIUM, Severity.CRITICAL),
        (Severity.HIGH, Severity.CRITICAL),
        (Severity.CRITICAL, Severity.CRITICAL),
    ],
    ids=["low->critical", "medium->critical", "high->critical", "critical-unchanged"],
)
def test_ecc_breach_floors_to_critical(model_severity, expected):
    detections = {Domain.GPU: _detection(Domain.GPU, [_ecc_breach()])}
    assert severity_floor(model_severity, detections) == expected


def test_gpu_temp_breach_without_ecc_does_not_floor():
    detections = {Domain.GPU: _detection(Domain.GPU, [_gpu_temp_breach()])}
    assert severity_floor(Severity.MEDIUM, detections) == Severity.MEDIUM


def test_db_breach_without_floor_rule_preserves_model_severity():
    detections = {Domain.DB: _detection(Domain.DB, [_db_connection_breach()])}
    assert severity_floor(Severity.HIGH, detections) == Severity.HIGH


def test_no_detections_preserves_model_severity():
    assert severity_floor(Severity.LOW, {}) == Severity.LOW


def test_healthy_detection_preserves_model_severity():
    detections = {Domain.GPU: _detection(Domain.GPU, [])}
    assert severity_floor(Severity.LOW, detections) == Severity.LOW


def test_multi_domain_ecc_breach_wins():
    detections = {
        Domain.DB: _detection(Domain.DB, [_db_connection_breach()]),
        Domain.GPU: _detection(Domain.GPU, [_ecc_breach()]),
    }
    assert severity_floor(Severity.LOW, detections) == Severity.CRITICAL


def test_ecc_plus_temp_breach_still_floors():
    detections = {Domain.GPU: _detection(Domain.GPU, [_gpu_temp_breach(), _ecc_breach()])}
    assert severity_floor(Severity.HIGH, detections) == Severity.CRITICAL


# --- End-to-end test through run_incident() ---


def test_ecc_breach_forces_escalate_end_to_end(monkeypatch):
    """A GPU sample with ecc_errors=3 must route to ESCALATE even if the model says LOW."""
    from orchestrator import run_incident

    gpu_sample = GPUMetrics(
        timestamp=datetime.now(timezone.utc),
        source="test:severity_floor",
        gpu_temp_c=45.0,
        gpu_util_pct=30.0,
        gpu_mem_pct=30.0,
        ecc_errors=3,
    )

    report = run_incident({Domain.GPU: gpu_sample}, persist=False)

    assert report.severity == Severity.CRITICAL, (
        f"ECC breach must floor severity to CRITICAL, got {report.severity}"
    )
    assert report.routing_decision == RoutingDecision.ESCALATE, (
        f"CRITICAL severity must route to ESCALATE, got {report.routing_decision}"
    )
