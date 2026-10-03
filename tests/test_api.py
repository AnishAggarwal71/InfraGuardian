"""
Tests for the FastAPI surface (PRD FR-17-19) using FastAPI's `TestClient`.

Uses the shared fake model (same pattern as `test_orchestrator.py`) so these tests need no
network access or API key. Covers the health check, both `/analyze` input shapes (scenario
names and raw samples), and the two `HTTPException` paths in `_resolve_samples`.
"""

import pytest
from fastapi.testclient import TestClient

from api.server import app


@pytest.fixture
def client(monkeypatch):
    from tools.llm import FakeInfraGuardianModel

    fake = FakeInfraGuardianModel()
    monkeypatch.setattr("tools.llm.get_chat_model", lambda: fake)
    monkeypatch.setattr("agents.db_agent.get_chat_model", lambda: fake)
    monkeypatch.setattr("agents.cpu_mem_agent.get_chat_model", lambda: fake)
    monkeypatch.setattr("agents.gpu_agent.get_chat_model", lambda: fake)
    monkeypatch.setattr("orchestrator.get_chat_model", lambda: fake)
    # Avoid touching a real infraguardian.db from API tests.
    monkeypatch.setattr("api.server.run_incident", lambda samples, **kw: _run_no_persist(samples))
    return TestClient(app)


def _run_no_persist(samples):
    from orchestrator import run_incident

    return run_incident(samples, persist=False)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_analyze_with_scenario_name(client):
    response = client.post("/analyze", json={"scenarios": ["db_connection_pool_exhaustion"]})
    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] in {"auto_remediate", "recommend", "escalate"}


def test_analyze_with_raw_samples(client):
    sample = {
        "timestamp": "2026-01-01T00:00:00Z",
        "domain": "db",
        "source": "test",
        "query_latency_p95_ms": 50.0,
        "connections_active": 10,
        "connections_max": 100,
        "replication_lag_sec": 0.1,
        "error_rate_pct": 0.1,
    }
    response = client.post("/analyze", json={"samples": [sample]})
    assert response.status_code == 200
    body = response.json()
    assert body["affected_domains"] == []


def test_analyze_unknown_scenario_returns_400(client):
    response = client.post("/analyze", json={"scenarios": ["not_a_real_scenario"]})
    assert response.status_code == 400


def test_analyze_empty_request_returns_400(client):
    response = client.post("/analyze", json={})
    assert response.status_code == 400
