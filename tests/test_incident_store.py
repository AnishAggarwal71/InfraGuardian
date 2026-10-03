"""Tests for `storage/incident_store.py` — using a temp SQLite file per test (`tmp_path`)."""

from datetime import datetime, timezone

from agents.schemas import Domain, IncidentReport, RoutingDecision, Severity
from storage import incident_store
from tools.remediation import RunbookResult


def _report(incident_id: str = "inc-1") -> IncidentReport:
    return IncidentReport(
        incident_id=incident_id,
        timestamp=datetime.now(timezone.utc),
        duration_sec=10,
        affected_domains=[Domain.DB],
        observed_anomalies=["db: connection_pool_utilization_pct"],
        evidence=["connection_pool_utilization_pct=99 (max 85)"],
        unified_root_cause="Connection pool exhaustion.",
        correlations=[],
        severity=Severity.HIGH,
        confidence=0.9,
        predictive_risk="x",
        priority_ordered_actions=["recycle pool"],
        selected_runbook_id="db.recycle_connection_pool",
        recommended_or_executed_runbook="Recycle idle/leaked connections.",
        routing_decision=RoutingDecision.AUTO_REMEDIATE,
        automation_safety_reason="x",
    )


def test_save_and_get_round_trips_full_report(tmp_path):
    db_path = str(tmp_path / "test.db")
    report = _report()

    incident_store.save_incident(report, db_path=db_path)
    loaded = incident_store.get_incident(report.incident_id, db_path=db_path)

    assert loaded == report


def test_get_incident_returns_none_when_not_found(tmp_path):
    db_path = str(tmp_path / "test.db")
    incident_store.init_db(db_path)
    assert incident_store.get_incident("does-not-exist", db_path=db_path) is None


def test_was_already_executed_false_then_true_after_recording(tmp_path):
    db_path = str(tmp_path / "test.db")
    report = _report()
    incident_store.save_incident(report, db_path=db_path)

    assert incident_store.was_already_executed(
        report.incident_id, "db.recycle_connection_pool", db_path=db_path
    ) is False

    result = RunbookResult(
        runbook_id="db.recycle_connection_pool",
        success=True,
        message="Simulated.",
        executed_at=datetime.now(timezone.utc),
    )
    incident_store.record_remediation_action(
        incident_id=report.incident_id,
        runbook_id="db.recycle_connection_pool",
        rationale="high confidence, safe",
        confidence=0.9,
        result=result,
        db_path=db_path,
    )

    assert incident_store.was_already_executed(
        report.incident_id, "db.recycle_connection_pool", db_path=db_path
    ) is True
