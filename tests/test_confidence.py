"""
Tests for `tools/confidence.py`'s calibration formula (PRD §9, M3 decision 2 in DECISIONS.md).
"""

from agents.schemas import AutomationSafety, CrossDomainCorrelation, Domain, Severity, SpecialistFinding
from tools.confidence import calibrate


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


def test_single_domain_unsafe_caps_below_auto_remediate_threshold():
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.UNKNOWN)}
    assert calibrate(0.99, findings, []) == 0.65


def test_single_domain_safe_uses_default_cap():
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    assert calibrate(0.99, findings, []) == 0.8


def test_multi_domain_corroborated_all_safe_uses_high_cap():
    findings = {
        Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE),
        Domain.CPU_MEM: _finding(Domain.CPU_MEM, AutomationSafety.SAFE),
    }
    correlations = [
        CrossDomainCorrelation(
            domains=[Domain.DB, Domain.CPU_MEM],
            relationship="shared cause",
            supporting_evidence=["x"],
        )
    ]
    assert calibrate(0.99, findings, correlations) == 0.95


def test_multi_domain_without_correlation_uses_default_cap():
    findings = {
        Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE),
        Domain.CPU_MEM: _finding(Domain.CPU_MEM, AutomationSafety.SAFE),
    }
    assert calibrate(0.99, findings, []) == 0.8


def test_calibration_never_exceeds_model_confidence():
    findings = {Domain.DB: _finding(Domain.DB, AutomationSafety.SAFE)}
    assert calibrate(0.3, findings, []) == 0.3
