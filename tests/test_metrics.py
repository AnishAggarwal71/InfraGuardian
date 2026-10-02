"""Tests for metric contracts (PRD FR-01): valid samples construct; invalid ones are rejected."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tools.metrics import CPUMemMetrics, DBMetrics, Domain, GPUMetrics


def test_db_metrics_valid_sample_constructs():
    sample = DBMetrics(
        timestamp=datetime.now(timezone.utc),
        source="simulator:db",
        query_latency_p95_ms=50,
        connections_active=10,
        connections_max=100,
        replication_lag_sec=0.1,
        error_rate_pct=0.0,
    )
    assert sample.domain == Domain.DB


def test_db_metrics_rejects_out_of_bounds_error_rate():
    with pytest.raises(ValidationError):
        DBMetrics(
            timestamp=datetime.now(timezone.utc),
            source="simulator:db",
            query_latency_p95_ms=50,
            connections_active=10,
            connections_max=100,
            replication_lag_sec=0.1,
            error_rate_pct=150,  # > 100, invalid
        )


def test_cpu_mem_metrics_rejects_negative_cpu_pct():
    with pytest.raises(ValidationError):
        CPUMemMetrics(
            timestamp=datetime.now(timezone.utc),
            source="simulator:cpu_mem",
            cpu_pct=-5,
            mem_pct=50,
            swap_pct=0,
            load_avg_1m=1.0,
        )


def test_gpu_metrics_rejects_negative_ecc_errors():
    with pytest.raises(ValidationError):
        GPUMetrics(
            timestamp=datetime.now(timezone.utc),
            source="simulator:gpu",
            gpu_util_pct=50,
            gpu_mem_pct=50,
            gpu_temp_c=60,
            ecc_errors=-1,
        )


def test_db_metrics_rejects_zero_connections_max():
    with pytest.raises(ValidationError):
        DBMetrics(
            timestamp=datetime.now(timezone.utc),
            source="simulator:db",
            query_latency_p95_ms=50,
            connections_active=10,
            connections_max=0,  # must be > 0, divides utilization calc downstream
            replication_lag_sec=0.1,
            error_rate_pct=0.0,
        )
