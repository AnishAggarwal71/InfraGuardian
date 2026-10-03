"""
Tests for the LangGraph orchestrator (PRD FR-08, FR-09, FR-10, FR-11, FR-12-16).

Uses a shared `FakeInfraGuardianModel` instance (monkeypatched in place of `get_chat_model`)
so call counts can be asserted — this is how "healthy domains never invoke the LLM" (FR-06,
enforced structurally by the graph's conditional routing) gets verified as an executable test,
not just a design claim. All tests pass `persist=False` to avoid touching a real SQLite file,
except the dedicated auto-remediate test, which uses a `tmp_path` DB to also verify the FR-16
audit row.
"""

import pytest

from agents.schemas import Domain, RoutingDecision
from tools.llm import FakeInfraGuardianModel
from tools.scenarios import SCENARIOS


@pytest.fixture
def shared_fake_model(monkeypatch):
    fake = FakeInfraGuardianModel()
    monkeypatch.setattr("tools.llm.get_chat_model", lambda: fake)
    # Each agent/orchestrator module imported `get_chat_model` into its own namespace, so patch
    # every call site directly rather than relying on patching the origin module alone.
    monkeypatch.setattr("agents.db_agent.get_chat_model", lambda: fake)
    monkeypatch.setattr("agents.cpu_mem_agent.get_chat_model", lambda: fake)
    monkeypatch.setattr("agents.gpu_agent.get_chat_model", lambda: fake)
    monkeypatch.setattr("orchestrator.get_chat_model", lambda: fake)
    return fake


def test_all_healthy_domains_produce_empty_report_with_no_llm_calls(shared_fake_model):
    from orchestrator import run_incident

    samples = {Domain.DB: SCENARIOS["db_healthy"][-1]}
    report = run_incident(samples, persist=False)

    assert report.affected_domains == []
    assert report.severity.value == "low"
    assert shared_fake_model.call_count == 0


def test_single_domain_anomaly_invokes_only_that_specialist(shared_fake_model):
    from orchestrator import run_incident

    samples = {Domain.DB: SCENARIOS["db_connection_pool_exhaustion"][-1]}
    report = run_incident(samples, persist=False)

    assert report.affected_domains == [Domain.DB]
    # one specialist call + one synthesis call
    assert shared_fake_model.call_count == 2


def test_multi_domain_anomaly_invokes_both_specialists_and_correlates(shared_fake_model):
    from orchestrator import run_incident

    samples = {
        Domain.DB: SCENARIOS["db_connection_pool_exhaustion"][-1],
        Domain.CPU_MEM: SCENARIOS["cpu_mem_cpu_saturation"][-1],
    }
    report = run_incident(samples, persist=False)

    assert set(report.affected_domains) == {Domain.DB, Domain.CPU_MEM}
    assert len(report.correlations) >= 1
    # two specialist calls + one synthesis call
    assert shared_fake_model.call_count == 3


def test_single_domain_high_confidence_safe_anomaly_auto_remediates_and_logs_audit_row(
    shared_fake_model, tmp_path, monkeypatch
):
    """
    `db_connection_pool_exhaustion` maps (via the fake model's canned rule info) to a SAFE,
    single-domain finding — this is the end-to-end auto-remediate path the M3 demo criterion
    calls for: routing decision AUTO_REMEDIATE and a `remediation_actions` audit row present.
    """
    import functools

    import orchestrator
    from storage import incident_store

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(
        orchestrator.incident_store,
        "save_incident",
        functools.partial(incident_store.save_incident, db_path=db_path),
    )
    monkeypatch.setattr(
        orchestrator.incident_store,
        "was_already_executed",
        functools.partial(incident_store.was_already_executed, db_path=db_path),
    )
    monkeypatch.setattr(
        orchestrator.incident_store,
        "record_remediation_action",
        functools.partial(incident_store.record_remediation_action, db_path=db_path),
    )

    samples = {Domain.DB: SCENARIOS["db_connection_pool_exhaustion"][-1]}
    report = orchestrator.run_incident(samples, persist=True)

    assert report.routing_decision == RoutingDecision.AUTO_REMEDIATE
    assert report.selected_runbook_id == "db.recycle_connection_pool"
    assert incident_store.was_already_executed(
        report.incident_id, "db.recycle_connection_pool", db_path=db_path
    )


def test_specialist_failure_fails_closed_to_escalate_instead_of_crashing(
    shared_fake_model, monkeypatch
):
    """Directly tests orchestrator decision 7: a raising specialist must not crash the run."""
    import orchestrator

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated specialist outage")

    monkeypatch.setitem(orchestrator._SPECIALISTS, Domain.DB, _raise)

    samples = {Domain.DB: SCENARIOS["db_connection_pool_exhaustion"][-1]}
    report = orchestrator.run_incident(samples, persist=False)

    assert report.affected_domains == [Domain.DB]
    assert any("simulated specialist outage" in e for e in report.evidence)
    assert report.routing_decision == RoutingDecision.ESCALATE

