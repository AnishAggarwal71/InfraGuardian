"""
Deterministic routing & safety gates (PRD FR-12-14, §9).

`route()` is the single place these gates are evaluated and is intentionally NOT an LLM call —
it is a pure function over already-structured data (calibrated confidence, severity, and the
automation-safety verdicts of every contributing specialist finding). Routing runs after
structured parsing has already either succeeded or been replaced by a fail-closed sentinel
upstream in the orchestrator, so there is nothing here for malformed LLM output to corrupt.
"""

from __future__ import annotations

from pydantic import BaseModel

from agents.schemas import AutomationSafety, IncidentReport, RoutingDecision, SpecialistFinding
from tools.metrics import Domain
from tools.preconditions import check_preconditions
from tools.remediation import get_runbook_for_domain
from tools.thresholds import DetectionResult

_AUTO_REMEDIATE_CONFIDENCE_MIN = 0.8
_ESCALATE_CONFIDENCE_MAX = 0.5


class RoutingResult(BaseModel):
    """What `route()` decided, and why — the `reason` is surfaced in the incident report."""

    decision: RoutingDecision
    reason: str
    runbook_id: str | None = None


def route(
    report: IncidentReport,
    findings: dict[Domain, SpecialistFinding],
    detections: dict[Domain, DetectionResult],
) -> RoutingResult:
    """
    Apply PRD FR-12-14's gates, in priority order, to decide auto-remediate/recommend/escalate.

    Order matters: critical severity and low confidence are checked first and unconditionally
    override everything else (PRD §9 — "critical never auto-remediates", "low-confidence
    findings fail closed"). Only after those gates pass does automation-safety, domain count,
    and `tools.preconditions.check_preconditions` (separating "the model diagnosed it SAFE" from
    "automation is actually authorized" — `REAL_MODEL_VALIDATION.md` P1) determine whether the
    incident is safe to auto-remediate at all. `detections` is the deterministic rule pre-screen
    result per domain, needed because preconditions are checked against breached rules, not
    model output.
    """
    if report.severity.value == "critical":
        return RoutingResult(
            decision=RoutingDecision.ESCALATE,
            reason="Critical-severity incidents never auto-remediate (PRD §9).",
        )

    if report.confidence < _ESCALATE_CONFIDENCE_MAX:
        return RoutingResult(
            decision=RoutingDecision.ESCALATE,
            reason=(
                f"Confidence {report.confidence:.2f} is below the escalation floor "
                f"{_ESCALATE_CONFIDENCE_MAX} — low-confidence findings fail closed to "
                "escalation, not auto-remediation (PRD §9)."
            ),
        )

    all_safe = all(f.automation_safety == AutomationSafety.SAFE for f in findings.values())

    if (
        report.confidence >= _AUTO_REMEDIATE_CONFIDENCE_MIN
        and len(findings) == 1
        and all_safe
    ):
        (only_domain,) = findings.keys()
        runbook = get_runbook_for_domain(only_domain)
        if runbook is not None:
            precondition = check_preconditions(runbook.id, only_domain, detections)
            if precondition.met:
                return RoutingResult(
                    decision=RoutingDecision.AUTO_REMEDIATE,
                    reason=(
                        f"Confidence {report.confidence:.2f} >= {_AUTO_REMEDIATE_CONFIDENCE_MIN}, "
                        f"single affected domain ({only_domain.value}) is SAFE, an allowlisted "
                        f"runbook exists for it, and its deterministic preconditions are met "
                        f"(PRD FR-12): {precondition.reason}"
                    ),
                    runbook_id=runbook.id,
                )
            return RoutingResult(
                decision=RoutingDecision.RECOMMEND,
                reason=(
                    f"Model diagnosis would otherwise clear the auto-remediate bar, but "
                    f"runbook '{runbook.id}' preconditions are not met — automation is not "
                    f"authorized without them: {precondition.reason}"
                ),
                runbook_id=runbook.id,
            )

    if not all_safe:
        return RoutingResult(
            decision=RoutingDecision.RECOMMEND,
            reason=(
                "At least one contributing finding is UNSAFE/UNKNOWN for automation — SAFE "
                "across all contributing findings is a hard requirement for auto-remediation, "
                "not just a tiebreaker."
            ),
        )

    if len(findings) >= 2:
        return RoutingResult(
            decision=RoutingDecision.RECOMMEND,
            reason=(
                "Multiple domains are affected; a single runbook only ever targets one domain, "
                "so cross-domain incidents route to recommendation at minimum in this MVP even "
                "at high confidence/safety."
            ),
        )

    return RoutingResult(
        decision=RoutingDecision.RECOMMEND,
        reason=(
            f"Confidence {report.confidence:.2f} is below the auto-remediate threshold "
            f"{_AUTO_REMEDIATE_CONFIDENCE_MIN} — medium-confidence findings route to "
            "human-reviewed recommendation (PRD FR-13)."
        ),
    )
