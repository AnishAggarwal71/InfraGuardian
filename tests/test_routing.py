"""
Table-driven tests for `tools/routing.py` — this file is effectively the safety-invariant
checklist PRD §9/FR-12-14 calls for: every gate branch is exercised explicitly, including the
runbook-precondition gate from `tools/preconditions.py` (`REAL_MODEL_VALIDATION.md` P1): a
model-diagnosed SAFE finding must not be enough to auto-remediate on its own when the
deterministic preconditions for that runbook aren't met.
"""

from datetime import datetime, timezone

import pytest

from agents.schemas import (
    AutomationSafety,
    Domain,
    IncidentReport,
    RoutingDecision,
    Severity,
    SpecialistFinding,
)
from tools.routing import route
from tools.thresholds import BreachedRule, DetectionResult


def _finding(domain: Domain, automation_safety: AutomationSafety) -> SpecialistFinding:
    return SpecialistFinding(
        domain=domain,
        is_anomalous=True,
        root_cause_hypothesis="x",
        severity=Severity.MEDIUM,
        predictive_risk="x",
        evidence=["x"],
        automation_safety=automation_safety,
        automation_safety_reason="x",
    )


def _report(severity: Severity, confidence: float, affected_domains: list[Domain]) -> IncidentReport:
    return IncidentReport(
        incident_id="test",
        timestamp=datetime.now(timezone.utc),
        duration_sec=0,
        affected_domains=affected_domains,
        observed_anomalies=[],
        evidence=[],
        unified_root_cause="x",
        correlations=[],
        severity=severity,
        confidence=confidence,
        predictive_risk="x",
        priority_ordered_actions=[],
        routing_decision=RoutingDecision.ESCALATE,
        automation_safety_reason="x",
    )


def _detection(domain: Domain, rule_names: list[str]) -> DetectionResult:
    """Build a `DetectionResult` whose breached rules are exactly `rule_names` (dummy values)."""
    return DetectionResult(
        domain=domain,
        is_anomalous=len(rule_names) > 0,
        breached_rules=[
            BreachedRule(rule_name=name, observed_value=99.0, threshold=1.0) for name in rule_names
        ],
        evaluated_at=datetime.now(timezone.utc),
    )


# Isolated-breach detections satisfying each of the 3 allowlisted runbooks' preconditions.
_DB_ISOLATED_BREACH = {Domain.DB: _detection(Domain.DB, ["connection_pool_utilization_pct"])}


def test_critical_severity_always_escalates_regardless_of_confidence():
    report = _report(Severity.CRITICAL, confidence=0.99, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings, _DB_ISOLATED_BREACH)
    assert result.decision == RoutingDecision.ESCALATE


def test_low_confidence_always_escalates_regardless_of_severity():
    report = _report(Severity.LOW, confidence=0.1, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings, _DB_ISOLATED_BREACH)
    assert result.decision == RoutingDecision.ESCALATE


def test_high_confidence_single_domain_safe_auto_remediates():
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings, _DB_ISOLATED_BREACH)
    assert result.decision == RoutingDecision.AUTO_REMEDIATE
    assert result.runbook_id == "db.recycle_connection_pool"


@pytest.mark.parametrize("safety", [AutomationSafety.UNSAFE, AutomationSafety.UNKNOWN])
def test_high_confidence_unsafe_or_unknown_safety_recommends_not_auto_remediates(safety):
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, safety)}
    result = route(report, findings, _DB_ISOLATED_BREACH)
    assert result.decision == RoutingDecision.RECOMMEND


def test_medium_confidence_recommends():
    report = _report(Severity.MEDIUM, confidence=0.65, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings, _DB_ISOLATED_BREACH)
    assert result.decision == RoutingDecision.RECOMMEND


def test_multi_domain_high_confidence_all_safe_recommends_not_auto_remediates():
    report = _report(
        Severity.HIGH, confidence=0.95, affected_domains=[Domain.DB, Domain.CPU_MEM]
    )
    findings = {
        Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE),
        Domain.CPU_MEM: _finding(Domain.CPU_MEM, AutomationSafety.SAFE),
    }
    detections = {
        Domain.DB: _detection(Domain.DB, ["connection_pool_utilization_pct"]),
        Domain.CPU_MEM: _detection(Domain.CPU_MEM, ["cpu_pct"]),
    }
    result = route(report, findings, detections)
    assert result.decision == RoutingDecision.RECOMMEND


# --- Runbook precondition gate (REAL_MODEL_VALIDATION.md P1) ----------------------------------


def test_high_confidence_safe_but_concurrent_breach_recommends_not_auto_remediates():
    """
    A model may mark the finding SAFE, but if the DB domain ALSO breached a second rule
    concurrently (e.g. error_rate_pct alongside the connection-pool breach), the precondition for
    `db.recycle_connection_pool` isn't met — automation is not authorized without a narrower,
    isolated signal, regardless of what the model's SAFE verdict said.
    """
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    detections = {
        Domain.DB: _detection(Domain.DB, ["connection_pool_utilization_pct", "error_rate_pct"])
    }
    result = route(report, findings, detections)
    assert result.decision == RoutingDecision.RECOMMEND
    assert result.runbook_id == "db.recycle_connection_pool"


def test_high_confidence_safe_but_required_rule_not_breached_recommends():
    """
    Defensive case: the specialist is SAFE and the only finding, but the detection for that
    domain doesn't actually contain the rule the runbook requires (e.g. stale/mismatched
    detection data) — preconditions must fail closed here too.
    """
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    detections = {Domain.DB: _detection(Domain.DB, ["replication_lag_sec"])}
    result = route(report, findings, detections)
    assert result.decision == RoutingDecision.RECOMMEND


def test_high_confidence_safe_but_missing_detection_recommends():
    """No detection result at all for the affected domain must also fail closed, not crash."""
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings, {})
    assert result.decision == RoutingDecision.RECOMMEND


@pytest.mark.parametrize(
    "domain,rule_name",
    [
        (Domain.CPU_MEM, "cpu_pct"),
        (Domain.GPU, "gpu_mem_pct"),
    ],
)
def test_isolated_breach_auto_remediates_for_each_allowlisted_runbook(domain, rule_name):
    """Confirms the isolated-breach precondition is satisfiable for every runbook, not just DB's."""
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[domain])
    findings = {domain: _finding(domain, AutomationSafety.SAFE)}
    detections = {domain: _detection(domain, [rule_name])}
    result = route(report, findings, detections)
    assert result.decision == RoutingDecision.AUTO_REMEDIATE

