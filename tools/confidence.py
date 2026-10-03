"""
Confidence calibration for the auto-remediate/recommend/escalate gate (PRD FR-12-14, §9).

The synthesis LLM reports its own confidence (0-1) in `SynthesisOutput.confidence`, but PRD §9
requires the auto-remediate gate not trust a model's self-reported number unchecked — a
hallucinated or miscalibrated confidence would otherwise flow straight through to an automated
action. `calibrate()` caps the model's number against a deterministic signal derived from how
many domains corroborate the finding and whether the contributing specialists judged their
findings safe to act on. This is a documented judgment call (see DECISIONS.md), not a value
derived from real incident data.
"""

from __future__ import annotations

from agents.schemas import AutomationSafety, CrossDomainCorrelation, SpecialistFinding
from tools.metrics import Domain

_SINGLE_DOMAIN_UNSAFE_CAP = 0.65
_MULTI_DOMAIN_CORROBORATED_CAP = 0.95
_DEFAULT_CAP = 0.8


def calibrate(
    synthesis_confidence: float,
    findings: dict[Domain, SpecialistFinding],
    correlations: list[CrossDomainCorrelation],
) -> float:
    """
    Return the calibrated confidence to use for routing: `min(synthesis_confidence, cap)`.

    `cap` is 0.65 when only one domain is affected and that specialist reported
    `UNKNOWN`/`UNSAFE` automation safety (an unsupported finding should never be confident
    enough to clear the 0.8 auto-remediate threshold on its own); 0.95 when at least two domains
    corroborate each other (a non-empty `correlations` list) and every contributing specialist
    reported `SAFE`; 0.8 otherwise.
    """
    all_safe = all(f.automation_safety == AutomationSafety.SAFE for f in findings.values())

    if len(findings) == 1:
        (only_finding,) = findings.values()
        if only_finding.automation_safety != AutomationSafety.SAFE:
            cap = _SINGLE_DOMAIN_UNSAFE_CAP
        else:
            cap = _DEFAULT_CAP
    elif len(findings) >= 2 and correlations and all_safe:
        cap = _MULTI_DOMAIN_CORROBORATED_CAP
    else:
        cap = _DEFAULT_CAP

    return min(synthesis_confidence, cap)
