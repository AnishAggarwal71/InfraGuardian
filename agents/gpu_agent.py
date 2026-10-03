"""
GPU specialist agent (PRD FR-07).

Only ever invoked by the orchestrator when the M1 rule pre-screen has already flagged the GPU
domain as anomalous.
"""

from __future__ import annotations

from agents.schemas import SpecialistFinding
from tools.llm import get_chat_model
from tools.metrics import GPUMetrics
from tools.thresholds import DetectionResult


def _build_prompt(sample: GPUMetrics, detection: DetectionResult) -> str:
    breached_lines = "\n".join(
        f"{b.rule_name}={b.observed_value:.1f} (max {b.threshold:.1f})"
        for b in detection.breached_rules
    )
    return (
        "You are a GPU infrastructure specialist. A deterministic threshold check has flagged "
        "the following GPU metrics sample as anomalous. Diagnose the likely root cause.\n\n"
        f"Sample at {sample.timestamp}:\n"
        f"  gpu_util_pct={sample.gpu_util_pct}\n"
        f"  gpu_mem_pct={sample.gpu_mem_pct}\n"
        f"  gpu_temp_c={sample.gpu_temp_c}\n"
        f"  ecc_errors={sample.ecc_errors}\n\n"
        f"Breached rules:\n{breached_lines}\n\n"
        "Return a structured finding: root cause hypothesis, severity, predictive risk, "
        "evidence, and whether automated remediation would be safe."
    )


def diagnose(sample: GPUMetrics, detection: DetectionResult) -> SpecialistFinding:
    """Produce a structured RCA finding for an anomalous GPU sample."""
    model = get_chat_model().with_structured_output(SpecialistFinding)
    return model.invoke(_build_prompt(sample, detection))
