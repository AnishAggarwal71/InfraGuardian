"""
FastAPI surface for InfraGuardian (PRD FR-17-19).

`POST /analyze` accepts either predefined scenario names or raw per-domain metric samples and
returns the full `IncidentReport`. Both request and response bodies are Pydantic models, so
malformed input is rejected by FastAPI's validation layer before it ever reaches the
orchestrator (PRD FR-19) — this is the same "validate at the boundary" principle used for
LLM structured output, just applied to the HTTP boundary instead.
"""

from __future__ import annotations

from typing import Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agents.schemas import IncidentReport
from orchestrator import run_incident
from tools.metrics import CPUMemMetrics, DBMetrics, GPUMetrics, Domain
from tools.observability import RECORDER, ObservabilitySnapshot
from tools.scenarios import SCENARIOS

app = FastAPI(
    title="InfraGuardian",
    description="Agentic infrastructure observability and incident-response API.",
    version="0.1.0",
)

# Not a discriminated union: `Domain` is a `StrEnum`-typed field with a non-`Literal` default on
# each metrics model, and Pydantic's discriminated-union support requires a `Literal` type on
# the discriminator field. Plain `Union` with Pydantic v2's "smart mode" matching (picks the
# member whose required fields best match the input) is sufficient here since each domain's
# metrics model has a distinct, non-overlapping set of fields.
MetricSampleUnion = Union[DBMetrics, CPUMemMetrics, GPUMetrics]


class AnalyzeRequest(BaseModel):
    """
    Request body for `POST /analyze`. Exactly one of `scenarios`/`samples` must be non-empty —
    `scenarios` names predefined fixtures (PRD FR-17's "run predefined scenarios"), `samples`
    carries raw per-domain metric readings (PRD FR-18's "metric inputs") for ad hoc analysis.
    """

    scenarios: list[str] = Field(
        default_factory=list,
        description="Names of predefined scenarios from tools.scenarios.SCENARIOS.",
    )
    samples: list[MetricSampleUnion] = Field(
        default_factory=list,
        description="Raw metric samples to analyze directly, one per domain.",
    )


def _resolve_samples(request: AnalyzeRequest) -> dict[Domain, object]:
    """
    Turn an `AnalyzeRequest` into the `{Domain: MetricSample}` mapping `run_incident` expects.

    Scenario names resolve to that scenario's final (most-degraded-or-healthy) sample, matching
    the CLI demo's convention. Raises `HTTPException` (400) for unknown scenario names or when
    neither `scenarios` nor `samples` is provided, so a client gets an actionable error instead
    of a silently empty analysis.
    """
    if not request.scenarios and not request.samples:
        raise HTTPException(
            status_code=400, detail="Provide at least one of 'scenarios' or 'samples'."
        )

    resolved: dict[Domain, object] = {}
    for name in request.scenarios:
        if name not in SCENARIOS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown scenario '{name}'. Available: {', '.join(sorted(SCENARIOS))}",
            )
        final_sample = SCENARIOS[name][-1]
        resolved[final_sample.domain] = final_sample

    for sample in request.samples:
        resolved[sample.domain] = sample

    return resolved


@app.post("/analyze", response_model=IncidentReport)
def analyze(request: AnalyzeRequest) -> IncidentReport:
    """Run the full detect -> diagnose -> synthesize -> route pipeline and return the report."""
    samples = _resolve_samples(request)
    return run_incident(samples)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check — no dependency on the LLM or the incident store."""
    return {"status": "ok"}


@app.get("/metrics", response_model=ObservabilitySnapshot)
def metrics() -> ObservabilitySnapshot:
    """Self-observability snapshot for this process (PRD §11) — invocation counts, per-agent
    latency/failure rate, end-to-end latency, routing/confidence distributions."""
    return RECORDER.snapshot()
