"""
Shared structured-output contracts for InfraGuardian's specialist agents and orchestrator
(PRD FR-07, FR-10, FR-11, §8).

Every LLM call in this project (specialist diagnosis, cross-domain synthesis) is bound to one
of these Pydantic models via `with_structured_output()` — this is what makes "schema-valid"
model output an enforceable property rather than a hope. A model that can't produce a valid
instance of these raises during `.invoke()`, which the orchestrator (M3) is expected to treat
as a fail-closed signal per PRD §9, not a crash to swallow silently.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from tools.metrics import Domain


class AutomationSafety(StrEnum):
    """Whether a specialist judges its finding safe for unattended remediation."""

    SAFE = "safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    """Incident severity. CRITICAL must never auto-remediate (PRD §9) — enforced in M3."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SpecialistFinding(BaseModel):
    """
    One domain specialist's structured root-cause analysis for a single anomalous sample.

    Only produced for domains the M1 rule pre-screen flagged as anomalous (PRD FR-06) — a
    healthy domain never has a `SpecialistFinding` because its specialist is never invoked.
    """

    domain: Domain
    is_anomalous: bool
    root_cause_hypothesis: str = Field(
        description="Specialist's best-guess explanation for the anomaly, in plain language."
    )
    severity: Severity
    predictive_risk: str = Field(
        description="What is likely to happen next if this anomaly is left unaddressed."
    )
    evidence: list[str] = Field(
        description="Concrete observed values/breached rules supporting the hypothesis."
    )
    automation_safety: AutomationSafety
    automation_safety_reason: str = Field(
        description="Why this finding is/isn't safe to act on without a human (PRD FR-16)."
    )


class CrossDomainCorrelation(BaseModel):
    """
    A relationship the synthesis step identified between two or more domains' findings.

    `is_causal_hypothesis` defaults to True because PRD §9 requires causal claims to be labeled
    as hypotheses unless backed by directly measured evidence (e.g., a traced query, not just
    "these two things happened around the same time") — the synthesis prompt is responsible for
    only setting this False when that bar is actually met.
    """

    domains: list[Domain]
    relationship: str = Field(description="Plain-language description of the correlation.")
    is_causal_hypothesis: bool = True
    supporting_evidence: list[str]


class IncidentReport(BaseModel):
    """
    The canonical incident report (PRD §8). This is the single artifact every downstream
    consumer (routing in M3, the API/CLI in M4, the evaluation harness in M5) is built against.

    `routing_decision` and `recommended_or_executed_runbook` are placeholders in M2
    ("pending_m3") — the auto-remediate/recommend/escalate gating logic that actually populates
    them is M3's responsibility (PRD FR-12-16), but the field exists now so the schema is
    stable going forward and M3 doesn't need a breaking change.
    """

    incident_id: str
    timestamp: datetime
    duration_sec: float = Field(ge=0)
    affected_domains: list[Domain]
    observed_anomalies: list[str]
    evidence: list[str]
    unified_root_cause: str
    correlations: list[CrossDomainCorrelation]
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    predictive_risk: str
    priority_ordered_actions: list[str]
    recommended_or_executed_runbook: str | None = None
    routing_decision: str = "pending_m3"
    automation_safety_reason: str


class SynthesisOutput(BaseModel):
    """
    What the synthesis LLM call actually produces — a subset of `IncidentReport`'s fields.

    Fields the orchestrator can compute deterministically in code (`incident_id`, `timestamp`,
    `duration_sec`, `affected_domains`, `observed_anomalies`, `evidence`,
    `routing_decision`) are deliberately excluded here: asking the model to restate values it
    didn't need to reason about would just be another place for it to introduce drift.
    """

    unified_root_cause: str
    correlations: list[CrossDomainCorrelation]
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    predictive_risk: str
    priority_ordered_actions: list[str]
    automation_safety_reason: str
