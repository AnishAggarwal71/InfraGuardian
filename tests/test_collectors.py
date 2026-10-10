"""
Tests for the collector abstraction (`tools/collectors.py`, PRD FR-03): `SimulatorCollector`'s
replay/reset behavior, and `PsutilCollector` producing a schema-valid `CPUMemMetrics` from this
host's real metrics (with `psutil` itself monkeypatched to fixed values, so the test is
deterministic and doesn't depend on the actual machine's load at test time).
"""

from datetime import datetime, timezone

import pytest

from tools.collectors import PsutilCollector, SimulatorCollector
from tools.metrics import CPUMemMetrics, Domain


def _sample(i: int) -> CPUMemMetrics:
    return CPUMemMetrics(
        timestamp=datetime.now(timezone.utc),
        source="simulator:cpu_mem",
        cpu_pct=float(i),
        mem_pct=10.0,
        swap_pct=0.0,
        load_avg_1m=1.0,
    )


def test_simulator_collector_replays_samples_in_order():
    samples = [_sample(1), _sample(2), _sample(3)]
    collector = SimulatorCollector(samples)

    assert len(collector) == 3
    assert collector.collect() is samples[0]
    assert collector.collect() is samples[1]
    assert collector.collect() is samples[2]


def test_simulator_collector_raises_stop_iteration_when_exhausted():
    collector = SimulatorCollector([_sample(1)])
    collector.collect()
    with pytest.raises(StopIteration):
        collector.collect()


def test_simulator_collector_reset_rewinds_to_first_sample():
    samples = [_sample(1), _sample(2)]
    collector = SimulatorCollector(samples)
    collector.collect()
    collector.collect()
    collector.reset()
    assert collector.collect() is samples[0]


class _FakeVirtualMemory:
    percent = 42.5


class _FakeSwapMemory:
    percent = 3.0


def test_psutil_collector_returns_schema_valid_cpu_mem_sample(monkeypatch):
    monkeypatch.setattr("tools.collectors.psutil.cpu_percent", lambda interval=None: 55.0)
    monkeypatch.setattr("tools.collectors.psutil.virtual_memory", lambda: _FakeVirtualMemory())
    monkeypatch.setattr("tools.collectors.psutil.swap_memory", lambda: _FakeSwapMemory())
    monkeypatch.setattr("tools.collectors.psutil.getloadavg", lambda: (1.23, 1.1, 0.9))

    sample = PsutilCollector().collect()

    assert isinstance(sample, CPUMemMetrics)
    assert sample.domain == Domain.CPU_MEM
    assert sample.source == "collector:psutil"
    assert sample.cpu_pct == 55.0
    assert sample.mem_pct == 42.5
    assert sample.swap_pct == 3.0
    assert sample.load_avg_1m == 1.23
