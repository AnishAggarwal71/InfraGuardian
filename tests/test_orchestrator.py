"""
Tests for the LangGraph orchestrator (PRD FR-08, FR-09, FR-10, FR-11).

Uses a shared `FakeInfraGuardianModel` instance (monkeypatched in place of `get_chat_model`)
so call counts can be asserted — this is how "healthy domains never invoke the LLM" (FR-06,
enforced structurally by the graph's conditional routing) gets verified as an executable test,
not just a design claim.
"""

import pytest

from agents.schemas import Domain
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
    report = run_incident(samples)

    assert report.affected_domains == []
    assert report.severity.value == "low"
    assert shared_fake_model.call_count == 0


def test_single_domain_anomaly_invokes_only_that_specialist(shared_fake_model):
    from orchestrator import run_incident

    samples = {Domain.DB: SCENARIOS["db_connection_pool_exhaustion"][-1]}
    report = run_incident(samples)

    assert report.affected_domains == [Domain.DB]
    # one specialist call + one synthesis call
    assert shared_fake_model.call_count == 2


def test_multi_domain_anomaly_invokes_both_specialists_and_correlates(shared_fake_model):
    from orchestrator import run_incident

    samples = {
        Domain.DB: SCENARIOS["db_connection_pool_exhaustion"][-1],
        Domain.CPU_MEM: SCENARIOS["cpu_mem_cpu_saturation"][-1],
    }
    report = run_incident(samples)

    assert set(report.affected_domains) == {Domain.DB, Domain.CPU_MEM}
    assert len(report.correlations) >= 1
    # two specialist calls + one synthesis call
    assert shared_fake_model.call_count == 3
