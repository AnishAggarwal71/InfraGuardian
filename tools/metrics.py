"""
Metric contracts for InfraGuardian.

Defines the typed, validated data model every downstream component (rule engine, specialist
agents, orchestrator) consumes. Keeping these as Pydantic models — rather than passing raw
dicts — is what satisfies PRD FR-01 ("typed metric contracts... validate incoming data") and
lets malformed input fail fast at the ingestion boundary instead of surfacing confusingly deep
in a specialist agent or the orchestrator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class Domain(StrEnum):
    """The three infrastructure domains InfraGuardian monitors in the MVP."""

    DB = "db"
    CPU_MEM = "cpu_mem"
    GPU = "gpu"


class MetricSample(BaseModel):
    """
    Base contract shared by every domain-specific metric sample.

    `timestamp` and `source`/`domain` metadata are required on every sample (PRD FR-04) so a
    sample can always be attributed to when and where it came from, regardless of whether it
    originated from a simulator or a real collector.
    """

    timestamp: datetime
    domain: Domain
    source: str = Field(
        description='Origin of this sample, e.g. "simulator:db" or "collector:psutil".'
    )

    @staticmethod
    def now() -> datetime:
        """Convenience for building fixture timestamps in UTC."""
        return datetime.now(timezone.utc)


class DBMetrics(MetricSample):
    """Database health signals: query latency, connection pool pressure, replication, errors."""

    domain: Domain = Domain.DB
    query_latency_p95_ms: float = Field(ge=0)
    connections_active: int = Field(ge=0)
    connections_max: int = Field(gt=0)
    replication_lag_sec: float = Field(ge=0)
    error_rate_pct: float = Field(ge=0, le=100)


class CPUMemMetrics(MetricSample):
    """Host-level compute pressure signals: CPU, memory, swap, and load."""

    domain: Domain = Domain.CPU_MEM
    cpu_pct: float = Field(ge=0, le=100)
    mem_pct: float = Field(ge=0, le=100)
    swap_pct: float = Field(ge=0, le=100)
    load_avg_1m: float = Field(ge=0)


class GPUMetrics(MetricSample):
    """GPU health signals: utilization, memory, thermal, and ECC error pressure."""

    domain: Domain = Domain.GPU
    gpu_util_pct: float = Field(ge=0, le=100)
    gpu_mem_pct: float = Field(ge=0, le=100)
    gpu_temp_c: float = Field(ge=0)
    ecc_errors: int = Field(ge=0)
