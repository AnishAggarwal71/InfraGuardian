"""
Table-driven tests for `tools/routing.py` — this file is effectively the safety-invariant
checklist PRD §9/FR-12-14 calls for: every gate branch is exercised explicitly.
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


def test_critical_severity_always_escalates_regardless_of_confidence():
    report = _report(Severity.CRITICAL, confidence=0.99, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings)
    assert result.decision == RoutingDecision.ESCALATE


def test_low_confidence_always_escalates_regardless_of_severity():
    report = _report(Severity.LOW, confidence=0.1, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings)
    assert result.decision == RoutingDecision.ESCALATE


def test_high_confidence_single_domain_safe_auto_remediates():
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings)
    assert result.decision == RoutingDecision.AUTO_REMEDIATE
    assert result.runbook_id == "db.recycle_connection_pool"


@pytest.mark.parametrize("safety", [AutomationSafety.UNSAFE, AutomationSafety.UNKNOWN])
def test_high_confidence_unsafe_or_unknown_safety_recommends_not_auto_remediates(safety):
    report = _report(Severity.HIGH, confidence=0.9, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, safety)}
    result = route(report, findings)
    assert result.decision == RoutingDecision.RECOMMEND


def test_medium_confidence_recommends():
    report = _report(Severity.MEDIUM, confidence=0.65, affected_domains=[Domain.DB])
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    result = route(report, findings)
    assert result.decision == RoutingDecision.RECOMMEND


def test_multi_domain_high_confidence_all_safe_recommends_not_auto_remediates():
    report = _report(
        Severity.HIGH, confidence=0.95, affected_domains=[Domain.DB, Domain.CPU_MEM]
    )
    findings = {
        Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE),
        Domain.CPU_MEM: _finding(Domain.CPU_MEM, AutomationSafety.SAFE),
    }
    result = route(report, findings)
    assert result.decision == RoutingDecision.RECOMMEND
