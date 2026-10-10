"""
DB specialist agent (PRD FR-07).

Only ever invoked by the orchestrator when the M1 rule pre-screen (`tools.thresholds.evaluate`)
has already flagged the DB domain as anomalous — this module has no opinion on when it runs,
it just does the diagnosis once asked.
"""

from __future__ import annotations

from agents.schemas import SpecialistFinding
from tools.llm import get_chat_model, invoke_structured
from tools.metrics import DBMetrics
from tools.thresholds import DetectionResult


def _build_prompt(sample: DBMetrics, detection: DetectionResult) -> str:
    breached_lines = "\n".join(
        f"{b.rule_name}={b.observed_value:.1f} (max {b.threshold:.1f})"
        for b in detection.breached_rules
    )
    return (
        "You are a database reliability specialist. A deterministic threshold check has "
        "flagged the following DB metrics sample as anomalous. Diagnose the likely root cause.\n\n"
        f"Sample at {sample.timestamp}:\n"
        f"  query_latency_p95_ms={sample.query_latency_p95_ms}\n"
        f"  connections_active={sample.connections_active}/{sample.connections_max}\n"
        f"  replication_lag_sec={sample.replication_lag_sec}\n"
        f"  error_rate_pct={sample.error_rate_pct}\n\n"
        f"Breached rules:\n{breached_lines}\n\n"
        "Return a structured finding: root cause hypothesis, severity, predictive risk, "
        "evidence, and whether automated remediation would be safe."
    )


def diagnose(sample: DBMetrics, detection: DetectionResult) -> SpecialistFinding:
    """Produce a structured RCA finding for an anomalous DB sample."""
    return invoke_structured(get_chat_model(), SpecialistFinding, _build_prompt(sample, detection))
