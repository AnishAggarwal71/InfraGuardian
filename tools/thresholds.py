"""
Deterministic rule pre-screen for InfraGuardian (PRD FR-05, FR-06).

Threshold checks here run before any LLM call and decide whether a domain is anomalous. The
orchestrator (M2) uses `DetectionResult.is_anomalous` to decide whether to invoke that domain's
specialist agent at all — healthy domains must never reach the LLM. Keeping this module free of
any model/agent import is deliberate: it is the enforcement point for FR-05/FR-06, not just a
convention.

Threshold constants are named and centralized here (rather than inlined in `evaluate()`) because
an operator tuning this system in practice needs one place to find and justify each number.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from tools.metrics import CPUMemMetrics, DBMetrics, Domain, GPUMetrics, MetricSample

# --- DB thresholds ---------------------------------------------------------------------------
# p95 latency threshold set well above the simulator's healthy baseline (~45-50ms) but below
# where the degradation scenario crosses into clearly-slow territory (PRD acceptance criterion
# is "a correct classification", not a tuned-for-production number — see DECISIONS.md).
DB_LATENCY_P95_MS_MAX = 200.0
DB_CONNECTION_UTILIZATION_PCT_MAX = 85.0
DB_REPLICATION_LAG_SEC_MAX = 5.0
DB_ERROR_RATE_PCT_MAX = 2.0

# --- CPU/memory thresholds --------------------------------------------------------------------
CPU_PCT_MAX = 90.0
MEM_PCT_MAX = 90.0
SWAP_PCT_MAX = 10.0  # any meaningful swapping indicates memory pressure, not just "high usage"
LOAD_AVG_1M_MAX = 8.0

# --- GPU thresholds ----------------------------------------------------------------------------
GPU_TEMP_C_MAX = 85.0  # typical consumer/datacenter GPU thermal throttle point
GPU_MEM_PCT_MAX = 95.0
GPU_ECC_ERRORS_MAX = 0  # any ECC error is treated as anomalous — these indicate hardware faults


class BreachedRule(BaseModel):
    """One threshold that was exceeded, with enough context to explain the breach."""

    rule_name: str
    observed_value: float
    threshold: float


class DetectionResult(BaseModel):
    """
    Outcome of running the rule pre-screen against a single sample.

    `is_anomalous=False` means the orchestrator must not invoke a specialist for this domain on
    this sample (FR-06). `breached_rules` is empty iff `is_anomalous` is False.
    """

    domain: Domain
    is_anomalous: bool
    breached_rules: list[BreachedRule]
    evaluated_at: datetime


def _evaluate_db(sample: DBMetrics) -> list[BreachedRule]:
    breached: list[BreachedRule] = []
    if sample.query_latency_p95_ms > DB_LATENCY_P95_MS_MAX:
        breached.append(
            BreachedRule(
                rule_name="query_latency_p95_ms",
                observed_value=sample.query_latency_p95_ms,
                threshold=DB_LATENCY_P95_MS_MAX,
            )
        )
    utilization_pct = 100 * sample.connections_active / sample.connections_max
    if utilization_pct > DB_CONNECTION_UTILIZATION_PCT_MAX:
        breached.append(
            BreachedRule(
                rule_name="connection_pool_utilization_pct",
                observed_value=utilization_pct,
                threshold=DB_CONNECTION_UTILIZATION_PCT_MAX,
            )
        )
    if sample.replication_lag_sec > DB_REPLICATION_LAG_SEC_MAX:
        breached.append(
            BreachedRule(
                rule_name="replication_lag_sec",
                observed_value=sample.replication_lag_sec,
                threshold=DB_REPLICATION_LAG_SEC_MAX,
            )
        )
    if sample.error_rate_pct > DB_ERROR_RATE_PCT_MAX:
        breached.append(
            BreachedRule(
                rule_name="error_rate_pct",
                observed_value=sample.error_rate_pct,
                threshold=DB_ERROR_RATE_PCT_MAX,
            )
        )
    return breached


def _evaluate_cpu_mem(sample: CPUMemMetrics) -> list[BreachedRule]:
    breached: list[BreachedRule] = []
    if sample.cpu_pct > CPU_PCT_MAX:
        breached.append(
            BreachedRule(rule_name="cpu_pct", observed_value=sample.cpu_pct, threshold=CPU_PCT_MAX)
        )
    if sample.mem_pct > MEM_PCT_MAX:
        breached.append(
            BreachedRule(rule_name="mem_pct", observed_value=sample.mem_pct, threshold=MEM_PCT_MAX)
        )
    if sample.swap_pct > SWAP_PCT_MAX:
        breached.append(
            BreachedRule(
                rule_name="swap_pct", observed_value=sample.swap_pct, threshold=SWAP_PCT_MAX
            )
        )
    if sample.load_avg_1m > LOAD_AVG_1M_MAX:
        breached.append(
            BreachedRule(
                rule_name="load_avg_1m",
                observed_value=sample.load_avg_1m,
                threshold=LOAD_AVG_1M_MAX,
            )
        )
    return breached


def _evaluate_gpu(sample: GPUMetrics) -> list[BreachedRule]:
    breached: list[BreachedRule] = []
    if sample.gpu_temp_c > GPU_TEMP_C_MAX:
        breached.append(
            BreachedRule(
                rule_name="gpu_temp_c", observed_value=sample.gpu_temp_c, threshold=GPU_TEMP_C_MAX
            )
        )
    if sample.gpu_mem_pct > GPU_MEM_PCT_MAX:
        breached.append(
            BreachedRule(
                rule_name="gpu_mem_pct",
                observed_value=sample.gpu_mem_pct,
                threshold=GPU_MEM_PCT_MAX,
            )
        )
    if sample.ecc_errors > GPU_ECC_ERRORS_MAX:
        breached.append(
            BreachedRule(
                rule_name="ecc_errors",
                observed_value=float(sample.ecc_errors),
                threshold=float(GPU_ECC_ERRORS_MAX),
            )
        )
    return breached


_EVALUATORS = {
    Domain.DB: _evaluate_db,
    Domain.CPU_MEM: _evaluate_cpu_mem,
    Domain.GPU: _evaluate_gpu,
}


def evaluate(sample: MetricSample) -> DetectionResult:
    """
    Run the deterministic threshold pre-screen for `sample`'s domain.

    This is the sole gate FR-06 relies on: no LLM is invoked here, and the orchestrator must
    treat `is_anomalous=False` as "do not call a specialist for this domain."
    """
    breached = _EVALUATORS[sample.domain](sample)
    return DetectionResult(
        domain=sample.domain,
        is_anomalous=len(breached) > 0,
        breached_rules=breached,
        evaluated_at=datetime.now(timezone.utc),
    )
