"""
Deterministic severity floor for safety-critical threshold breaches.

Parallels `tools/confidence.py` (which caps model-produced confidence against deterministic
signals): this module floors model-produced severity against rules that carry a non-negotiable
minimum. The orchestrator applies both corrections between the synthesis LLM output and the
routing gate, so neither confidence nor severity can be softened by a model past a deterministic
bound.

Motivated by the P0 finding in `evaluation/REAL_MODEL_VALIDATION.md`: Claude Haiku downgraded
a GPU ECC fault from critical to high, causing `route()` to return RECOMMEND instead of
ESCALATE. ECC errors are a hardware fault with a zero-tolerance threshold — the model must not
be able to soften that.

Lives in `tools/` (not in `tools/thresholds.py`) because `thresholds.py` deliberately avoids
importing from `agents.schemas` to stay free of model/agent coupling (see its module docstring).
This module bridges both sides, same import pattern as `tools/confidence.py`.
"""

from __future__ import annotations

from agents.schemas import Severity
from tools.metrics import Domain
from tools.thresholds import DetectionResult

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

_RULE_MIN_SEVERITY: dict[str, Severity] = {
    "ecc_errors": Severity.CRITICAL,
}


def severity_floor(
    model_severity: Severity,
    detections: dict[Domain, DetectionResult],
) -> Severity:
    """
    Return the higher of `model_severity` and the strongest rule-mandated floor.

    Scans every breached rule across all detection results. If any rule's name appears in
    `_RULE_MIN_SEVERITY` and that floor is above the model's severity, the floor wins. If no
    rule carries a floor (or no rules were breached), returns `model_severity` unchanged.
    """
    highest = model_severity
    for detection in detections.values():
        for rule in detection.breached_rules:
            floor = _RULE_MIN_SEVERITY.get(rule.rule_name)
            if floor is not None and _SEVERITY_RANK[floor] > _SEVERITY_RANK[highest]:
                highest = floor
    return highest
