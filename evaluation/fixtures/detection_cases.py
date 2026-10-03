"""
Detection-accuracy fixtures for the M5 evaluation harness (PRD §10).

Expected anomaly flags are derived independently from the threshold constants in
`tools/thresholds.py` and the literal value trajectories in `tools/scenarios.py` — by hand-tracing
which values cross which threshold at which sample index — rather than by calling
`tools.thresholds.evaluate()` on each sample and recording whatever it returns. Mirroring the
code under test would make "detection accuracy" tautologically 100%; these fixtures exist so a
future change to the rule engine that produces a different, wrong answer actually gets caught.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from tools.metrics import MetricSample
from tools.scenarios import SCENARIOS


class DetectionCase(BaseModel):
    """One (sample, expected rule-engine verdict) pair."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    sample: MetricSample
    expected_anomalous: bool


# Per-scenario, the 0-based index at which samples first become anomalous, hand-derived by
# comparing each scenario's literal trajectory (tools/scenarios.py) against the threshold
# constants (tools/thresholds.py). `None` means every sample in that scenario stays healthy.
#
#   db_slow_query_degradation:    latency [50,70,110,180,260,...] crosses 200ms at index 4 (260)
#   db_connection_pool_exhaustion: utilization [20,35,50,68,82,93,...]% crosses 85% at index 5 (93)
#   db_replication_lag:           lag [0.2,1.0,4.0,9.0,...]s crosses 5s at index 3 (9.0)
#   cpu_mem_cpu_saturation:       cpu [40,55,68,78,86,92,...]% crosses 90% at index 5 (92)
#   cpu_mem_memory_pressure:      swap [0,0,2,8,18,...]% crosses 10% at index 4 (18); mem never
#                                 exceeds 90 before that (83 at index 3)
#   cpu_mem_load_spike:           load [1.0,2.5,5.0,9.0,...] crosses 8.0 at index 3 (9.0)
#   gpu_thermal_pressure:         temp [55,62,70,77,83,88,...]C crosses 85C at index 5 (88)
#   gpu_memory_saturation:        mem [40,55,68,79,88,94,97,...]% crosses 95% at index 6 (97)
#   gpu_ecc_errors:               ecc [0,1,2,...] — any nonzero count is anomalous, first at index 1
_FIRST_ANOMALOUS_INDEX: dict[str, int | None] = {
    "db_healthy": None,
    "db_slow_query_degradation": 4,
    "db_connection_pool_exhaustion": 5,
    "db_replication_lag": 3,
    "cpu_mem_healthy": None,
    "cpu_mem_cpu_saturation": 5,
    "cpu_mem_memory_pressure": 4,
    "cpu_mem_load_spike": 3,
    "gpu_healthy": None,
    "gpu_thermal_pressure": 5,
    "gpu_memory_saturation": 6,
    "gpu_ecc_errors": 1,
}


def _build_detection_cases() -> list[DetectionCase]:
    """Expand every scenario's sample list into one labeled `DetectionCase` per sample."""
    cases: list[DetectionCase] = []
    for name, samples in SCENARIOS.items():
        first_anomalous = _FIRST_ANOMALOUS_INDEX[name]
        for i, sample in enumerate(samples):
            expected = first_anomalous is not None and i >= first_anomalous
            cases.append(
                DetectionCase(name=f"{name}[{i}]", sample=sample, expected_anomalous=expected)
            )
    return cases


DETECTION_CASES: list[DetectionCase] = _build_detection_cases()
