"""
CPU/memory specialist agent (PRD FR-07).

Only ever invoked by the orchestrator when the M1 rule pre-screen has already flagged the
CPU/memory domain as anomalous.
"""

from __future__ import annotations

from agents.schemas import SpecialistFinding
from tools.llm import get_chat_model, invoke_structured
from tools.metrics import CPUMemMetrics
from tools.thresholds import DetectionResult


def _build_prompt(sample: CPUMemMetrics, detection: DetectionResult) -> str:
    breached_lines = "\n".join(
        f"{b.rule_name}={b.observed_value:.1f} (max {b.threshold:.1f})"
        for b in detection.breached_rules
    )
    return (
        "You are a host compute reliability specialist. A deterministic threshold check has "
        "flagged the following CPU/memory metrics sample as anomalous. Diagnose the likely "
        "root cause.\n\n"
        f"Sample at {sample.timestamp}:\n"
        f"  cpu_pct={sample.cpu_pct}\n"
        f"  mem_pct={sample.mem_pct}\n"
        f"  swap_pct={sample.swap_pct}\n"
        f"  load_avg_1m={sample.load_avg_1m}\n\n"
        f"Breached rules:\n{breached_lines}\n\n"
        "Return a structured finding: root cause hypothesis, severity, predictive risk, "
        "evidence, and whether automated remediation would be safe."
    )


def diagnose(sample: CPUMemMetrics, detection: DetectionResult) -> SpecialistFinding:
    """Produce a structured RCA finding for an anomalous CPU/memory sample."""
    return invoke_structured(get_chat_model(), SpecialistFinding, _build_prompt(sample, detection))
