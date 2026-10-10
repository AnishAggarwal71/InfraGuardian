"""
Tests for `cli/main.py`'s `watch` subcommand: the edge-triggered alerting rule (`_should_alert`)
in isolation, and `_run_watch_loop`'s persistence/notification wiring against a fake collector
and the real (offline, fake-model) pipeline.
"""

from datetime import datetime, timezone

from agents.schemas import Severity
from cli.main import _run_watch_loop, _should_alert
from tools.metrics import CPUMemMetrics


def _sample(cpu_pct: float) -> CPUMemMetrics:
    return CPUMemMetrics(
        timestamp=datetime.now(timezone.utc),
        source="collector:psutil",
        cpu_pct=cpu_pct,
        mem_pct=20.0,
        swap_pct=0.0,
        load_avg_1m=1.0,
    )


class _FakeCollector:
    """Replays a fixed sequence of samples, one per `.collect()` call — same pattern as
    `tools.collectors.SimulatorCollector`, kept local since the test only needs this much."""

    def __init__(self, samples: list[CPUMemMetrics]) -> None:
        self._samples = samples
        self._index = 0

    def collect(self) -> CPUMemMetrics:
        sample = self._samples[self._index]
        self._index += 1
        return sample


def test_should_alert_healthy_to_healthy_never_alerts():
    assert _should_alert(False, None, prev_anomalous=False, prev_alerted_severity=None) is False


def test_should_alert_fires_on_healthy_to_anomalous_transition():
    assert _should_alert(True, Severity.MEDIUM, prev_anomalous=False, prev_alerted_severity=None) is True


def test_should_alert_does_not_repeat_for_unchanged_severity():
    assert (
        _should_alert(True, Severity.MEDIUM, prev_anomalous=True, prev_alerted_severity=Severity.MEDIUM)
        is False
    )


def test_should_alert_fires_again_when_severity_escalates():
    assert (
        _should_alert(True, Severity.CRITICAL, prev_anomalous=True, prev_alerted_severity=Severity.MEDIUM)
        is True
    )


def test_should_alert_does_not_alert_on_recovery():
    assert _should_alert(False, None, prev_anomalous=True, prev_alerted_severity=Severity.HIGH) is False


def test_watch_loop_persists_and_notifies_only_on_the_anomalous_tick(monkeypatch):
    collector = _FakeCollector([_sample(cpu_pct=20.0), _sample(cpu_pct=99.0)])

    saved_incidents = []
    monkeypatch.setattr("cli.main.incident_store.save_incident", lambda report: saved_incidents.append(report))

    notified = []

    def fake_notify(title: str, message: str) -> None:
        notified.append((title, message))

    _run_watch_loop(collector, interval=0, max_ticks=2, notify=fake_notify, sleep=lambda _seconds: None)

    assert len(saved_incidents) == 1
    assert saved_incidents[0].affected_domains
    assert len(notified) == 1
