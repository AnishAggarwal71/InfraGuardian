"""
Tests for self-observability (PRD §11): agent call/incident recording and the derived snapshot
metrics (failure rate, latency percentiles, routing/confidence distributions).
"""

import pytest

from agents.schemas import RoutingDecision
from tools.observability import _MetricsRecorder


@pytest.fixture
def recorder():
    return _MetricsRecorder()


def test_empty_snapshot_has_zeroed_metrics(recorder):
    snap = recorder.snapshot()
    assert snap.total_agent_calls == 0
    assert snap.agent_failure_rate == 0.0
    assert snap.total_incidents == 0
    assert snap.cost_per_incident_usd is None


def test_agent_failure_rate_computed_correctly(recorder):
    recorder.record_agent_call("db", 10.0, success=True)
    recorder.record_agent_call("db", 20.0, success=False)
    recorder.record_agent_call("gpu", 5.0, success=True)

    snap = recorder.snapshot()
    assert snap.total_agent_calls == 3
    assert snap.agent_call_failures == 1
    assert snap.agent_failure_rate == pytest.approx(1 / 3)
    assert snap.collector_failures == 1
    assert snap.per_agent_avg_latency_ms["db"] == pytest.approx(15.0)
    assert snap.per_agent_avg_latency_ms["gpu"] == pytest.approx(5.0)


def test_incident_recording_tracks_routing_and_confidence(recorder):
    recorder.record_incident(100.0, RoutingDecision.AUTO_REMEDIATE, 0.9)
    recorder.record_incident(200.0, RoutingDecision.ESCALATE, 0.3)

    snap = recorder.snapshot()
    assert snap.total_incidents == 2
    assert snap.routing_decision_counts == {"auto_remediate": 1, "escalate": 1}
    assert snap.confidence_mean == pytest.approx(0.6)
    assert snap.confidence_min == pytest.approx(0.3)
    assert snap.confidence_max == pytest.approx(0.9)
    assert snap.end_to_end_latency_p50_ms == pytest.approx(150.0)


def test_reset_clears_all_recorded_data(recorder):
    recorder.record_agent_call("db", 10.0, success=True)
    recorder.record_incident(100.0, RoutingDecision.RECOMMEND, 0.7)
    recorder.reset()

    snap = recorder.snapshot()
    assert snap.total_agent_calls == 0
    assert snap.total_incidents == 0
