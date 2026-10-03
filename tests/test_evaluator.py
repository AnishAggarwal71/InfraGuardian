"""
Tests for the M5 evaluation harness (PRD §10) — not re-asserting every metric's exact value
(the harness's own printed output is the source of truth for that), but guarding the contract
properties that matter: it runs without the fixtures count regressing below the PRD's 100+
floor, and the headline safety metric (false auto-remediations) is exactly what the fixtures
expect, which doubles as a regression check on `tools/routing.py`'s most important invariant.
"""

from evaluation.evaluator import run_evaluation
from evaluation.fixtures.detection_cases import DETECTION_CASES
from evaluation.fixtures.incident_cases import INCIDENT_CASES


def test_fixture_count_meets_prd_floor():
    assert len(DETECTION_CASES) + len(INCIDENT_CASES) >= 100


def test_evaluation_report_against_fixtures():
    report = run_evaluation()
    assert report.detection_accuracy == 1.0
    assert report.rca_accuracy == 1.0
    assert report.routing_accuracy == 1.0
    assert report.false_auto_remediation_count == 0
    assert report.schema_validity_rate == 1.0
