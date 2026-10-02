"""
Scenario catalog for InfraGuardian's deterministic simulator (PRD FR-02).

Each scenario is a hand-authored, fixed sequence of samples showing a believable trajectory —
not seeded-random generation. This keeps "deterministic" a literal, trivial-to-verify property
(same list every run) rather than something that depends on a seed and an RNG implementation,
which is both simpler to maintain and a cleaner thing to defend in review ("the trajectory is
spelled out in code — there is nothing to reproduce").

Four scenarios per domain (one healthy, three degraded) — one above the PRD's floor of three,
to give the M5 evaluation harness more fixture diversity later without revisiting this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools.metrics import CPUMemMetrics, DBMetrics, GPUMetrics, MetricSample

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ticks(n: int, step_sec: int = 30) -> list[datetime]:
    """n evenly spaced timestamps, step_sec apart, starting at a fixed epoch for reproducibility."""
    return [_T0 + timedelta(seconds=step_sec * i) for i in range(n)]


# --------------------------------------------------------------------------- DB scenarios

def _db_healthy() -> list[MetricSample]:
    ts = _ticks(6)
    return [
        DBMetrics(
            timestamp=t,
            source="simulator:db",
            query_latency_p95_ms=45 + i,
            connections_active=18 + i,
            connections_max=100,
            replication_lag_sec=0.2,
            error_rate_pct=0.1,
        )
        for i, t in enumerate(ts)
    ]


def _db_slow_query_degradation() -> list[MetricSample]:
    ts = _ticks(8)
    latencies = [50, 70, 110, 180, 260, 340, 410, 460]
    return [
        DBMetrics(
            timestamp=t,
            source="simulator:db",
            query_latency_p95_ms=latencies[i],
            connections_active=25,
            connections_max=100,
            replication_lag_sec=0.3,
            error_rate_pct=0.2,
        )
        for i, t in enumerate(ts)
    ]


def _db_connection_pool_exhaustion() -> list[MetricSample]:
    ts = _ticks(8)
    active = [20, 35, 50, 68, 82, 93, 98, 100]
    return [
        DBMetrics(
            timestamp=t,
            source="simulator:db",
            query_latency_p95_ms=60 + active[i],
            connections_active=active[i],
            connections_max=100,
            replication_lag_sec=0.2,
            error_rate_pct=0.1 * (i + 1),
        )
        for i, t in enumerate(ts)
    ]


def _db_replication_lag() -> list[MetricSample]:
    ts = _ticks(7)
    lag = [0.2, 1.0, 4.0, 9.0, 16.0, 24.0, 32.0]
    return [
        DBMetrics(
            timestamp=t,
            source="simulator:db",
            query_latency_p95_ms=55,
            connections_active=22,
            connections_max=100,
            replication_lag_sec=lag[i],
            error_rate_pct=0.1,
        )
        for i, t in enumerate(ts)
    ]


# --------------------------------------------------------------------- CPU/memory scenarios

def _cpu_mem_healthy() -> list[MetricSample]:
    ts = _ticks(6)
    return [
        CPUMemMetrics(
            timestamp=t,
            source="simulator:cpu_mem",
            cpu_pct=30 + i,
            mem_pct=40,
            swap_pct=0,
            load_avg_1m=1.2,
        )
        for i, t in enumerate(ts)
    ]


def _cpu_mem_cpu_saturation() -> list[MetricSample]:
    ts = _ticks(8)
    cpu = [40, 55, 68, 78, 86, 92, 96, 98]
    return [
        CPUMemMetrics(
            timestamp=t,
            source="simulator:cpu_mem",
            cpu_pct=cpu[i],
            mem_pct=50,
            swap_pct=0,
            load_avg_1m=1.0 + cpu[i] / 20,
        )
        for i, t in enumerate(ts)
    ]


def _cpu_mem_memory_pressure() -> list[MetricSample]:
    ts = _ticks(8)
    mem = [50, 62, 73, 83, 90, 95, 97, 98]
    swap = [0, 0, 2, 8, 18, 30, 42, 55]
    return [
        CPUMemMetrics(
            timestamp=t,
            source="simulator:cpu_mem",
            cpu_pct=45,
            mem_pct=mem[i],
            swap_pct=swap[i],
            load_avg_1m=1.5,
        )
        for i, t in enumerate(ts)
    ]


def _cpu_mem_load_spike() -> list[MetricSample]:
    ts = _ticks(7)
    load = [1.0, 2.5, 5.0, 9.0, 14.0, 20.0, 28.0]
    return [
        CPUMemMetrics(
            timestamp=t,
            source="simulator:cpu_mem",
            cpu_pct=60,
            mem_pct=55,
            swap_pct=0,
            load_avg_1m=load[i],
        )
        for i, t in enumerate(ts)
    ]


# ------------------------------------------------------------------------------ GPU scenarios

def _gpu_healthy() -> list[MetricSample]:
    ts = _ticks(6)
    return [
        GPUMetrics(
            timestamp=t,
            source="simulator:gpu",
            gpu_util_pct=20 + i,
            gpu_mem_pct=30,
            gpu_temp_c=55,
            ecc_errors=0,
        )
        for i, t in enumerate(ts)
    ]


def _gpu_thermal_pressure() -> list[MetricSample]:
    ts = _ticks(8)
    temp = [55, 62, 70, 77, 83, 88, 91, 93]
    return [
        GPUMetrics(
            timestamp=t,
            source="simulator:gpu",
            gpu_util_pct=90,
            gpu_mem_pct=60,
            gpu_temp_c=temp[i],
            ecc_errors=0,
        )
        for i, t in enumerate(ts)
    ]


def _gpu_memory_saturation() -> list[MetricSample]:
    ts = _ticks(8)
    mem = [40, 55, 68, 79, 88, 94, 97, 99]
    return [
        GPUMetrics(
            timestamp=t,
            source="simulator:gpu",
            gpu_util_pct=85,
            gpu_mem_pct=mem[i],
            gpu_temp_c=70,
            ecc_errors=0,
        )
        for i, t in enumerate(ts)
    ]


def _gpu_ecc_errors() -> list[MetricSample]:
    ts = _ticks(7)
    ecc = [0, 1, 2, 4, 7, 11, 16]
    return [
        GPUMetrics(
            timestamp=t,
            source="simulator:gpu",
            gpu_util_pct=75,
            gpu_mem_pct=50,
            gpu_temp_c=65,
            ecc_errors=ecc[i],
        )
        for i, t in enumerate(ts)
    ]


SCENARIOS: dict[str, list[MetricSample]] = {
    "db_healthy": _db_healthy(),
    "db_slow_query_degradation": _db_slow_query_degradation(),
    "db_connection_pool_exhaustion": _db_connection_pool_exhaustion(),
    "db_replication_lag": _db_replication_lag(),
    "cpu_mem_healthy": _cpu_mem_healthy(),
    "cpu_mem_cpu_saturation": _cpu_mem_cpu_saturation(),
    "cpu_mem_memory_pressure": _cpu_mem_memory_pressure(),
    "cpu_mem_load_spike": _cpu_mem_load_spike(),
    "gpu_healthy": _gpu_healthy(),
    "gpu_thermal_pressure": _gpu_thermal_pressure(),
    "gpu_memory_saturation": _gpu_memory_saturation(),
    "gpu_ecc_errors": _gpu_ecc_errors(),
}

HEALTHY_SCENARIOS = [name for name in SCENARIOS if name.endswith("_healthy")]
DEGRADED_SCENARIOS = [name for name in SCENARIOS if not name.endswith("_healthy")]
