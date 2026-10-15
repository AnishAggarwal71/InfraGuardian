"""
Self-observability for InfraGuardian (PRD §11): invocation counts, per-agent latency/failure
rate, end-to-end latency, routing/confidence distributions, and tool/collector failure counts.

This is a module-level, in-memory recorder (not persisted — PRD §11 doesn't ask for historical
observability storage, only that the signals exist and are inspectable) that the orchestrator
calls into as it runs. Kept intentionally separate from `storage/incident_store.py`: that module
is the durable incident audit trail (PRD §13); this one is operational telemetry about
InfraGuardian's own behavior, read via `snapshot()` from the CLI/API, not written to disk.

Token/cost tracking (PRD §11's "cost/incident") is now wired through `tools.llm.invoke_structured`:
every specialist/synthesis call prices its real token usage and passes the result into
`record_agent_call`'s `cost_usd` param. `cost_per_incident_usd` in `snapshot()` is the sum of all
recorded call costs divided by the number of completed incidents — `None` only when zero
incidents have run yet. The fake model reports a real (not faked) `$0.00`, since it performs no
real inference. Coverage is limited to the pricing table in `tools.llm`, which only lists models
this project has actually been run against.

A call against a real but unlisted model has nonzero token usage but no known USD rate —
`tools.llm._price_usage` returns `cost_usd=None` for that case, distinct from the fake model's
genuine `$0.00`. `record_agent_call`'s `pricing_unknown` param carries that distinction through so
`cost_per_incident_usd` doesn't silently average away a pricing gap: `pricing_unknown_calls` in
the snapshot makes the gap visible instead.
"""

from __future__ import annotations

import statistics
import threading
from datetime import datetime, timezone

from pydantic import BaseModel

from agents.schemas import RoutingDecision


class AgentCallRecord(BaseModel):
    """One specialist or synthesis LLM invocation, timed and marked success/failure."""

    agent: str
    latency_ms: float
    success: bool
    recorded_at: datetime
    cost_usd: float | None = None
    pricing_unknown: bool = False


class IncidentRecord(BaseModel):
    """One completed `run_incident()` call's outcome, for end-to-end and routing metrics."""

    duration_ms: float
    routing_decision: RoutingDecision
    confidence: float
    recorded_at: datetime


class ObservabilitySnapshot(BaseModel):
    """Aggregated PRD §11 metrics computed from everything recorded so far."""

    total_agent_calls: int
    agent_call_failures: int
    agent_failure_rate: float
    per_agent_avg_latency_ms: dict[str, float]
    total_incidents: int
    end_to_end_latency_p50_ms: float
    end_to_end_latency_p95_ms: float
    routing_decision_counts: dict[str, int]
    confidence_mean: float
    confidence_min: float
    confidence_max: float
    collector_failures: int
    cost_per_incident_usd: float | None = None
    pricing_unknown_calls: int = 0


class _MetricsRecorder:
    """
    Thread-safety note: a lock guards every mutation/read because the orchestrator's specialist
    nodes execute as concurrent LangGraph branches (PRD FR-08) — without it, concurrent
    `record_agent_call` calls from different domains could interleave list appends unsafely.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent_calls: list[AgentCallRecord] = []
        self._incidents: list[IncidentRecord] = []
        self._collector_failures = 0

    def record_agent_call(
        self,
        agent: str,
        latency_ms: float,
        success: bool,
        cost_usd: float | None = None,
        pricing_unknown: bool = False,
    ) -> None:
        """Record one specialist/synthesis LLM invocation's outcome, latency, and USD cost.

        `pricing_unknown=True` marks a call with real (nonzero) token usage against a model not
        in `tools.llm`'s pricing table — distinct from `cost_usd=0.0`, which means a real,
        known-zero cost (the fake model). Keeping the two distinguishable here is what lets
        `cost_per_incident_usd` avoid silently averaging away a pricing gap.
        """
        with self._lock:
            self._agent_calls.append(
                AgentCallRecord(
                    agent=agent,
                    latency_ms=latency_ms,
                    success=success,
                    recorded_at=datetime.now(timezone.utc),
                    cost_usd=cost_usd,
                    pricing_unknown=pricing_unknown,
                )
            )
            if not success:
                self._collector_failures += 1

    def record_incident(self, duration_ms: float, routing_decision: RoutingDecision, confidence: float) -> None:
        """Record one completed incident's end-to-end latency, routing outcome, and confidence."""
        with self._lock:
            self._incidents.append(
                IncidentRecord(
                    duration_ms=duration_ms,
                    routing_decision=routing_decision,
                    confidence=confidence,
                    recorded_at=datetime.now(timezone.utc),
                )
            )

    def reset(self) -> None:
        """Clear all recorded data — used by tests so each test starts from an empty recorder."""
        with self._lock:
            self._agent_calls.clear()
            self._incidents.clear()
            self._collector_failures = 0

    def snapshot(self) -> ObservabilitySnapshot:
        """Compute the current PRD §11 metrics from everything recorded so far."""
        with self._lock:
            calls = list(self._agent_calls)
            incidents = list(self._incidents)
            collector_failures = self._collector_failures

        total_calls = len(calls)
        failures = sum(1 for c in calls if not c.success)

        per_agent_latencies: dict[str, list[float]] = {}
        for c in calls:
            per_agent_latencies.setdefault(c.agent, []).append(c.latency_ms)
        per_agent_avg = {agent: statistics.mean(lats) for agent, lats in per_agent_latencies.items()}

        durations = [i.duration_ms for i in incidents]
        p50 = statistics.median(durations) if durations else 0.0
        p95 = (
            statistics.quantiles(durations, n=20)[18]
            if len(durations) >= 2
            else (durations[0] if durations else 0.0)
        )

        routing_counts: dict[str, int] = {}
        for i in incidents:
            routing_counts[i.routing_decision.value] = routing_counts.get(i.routing_decision.value, 0) + 1

        confidences = [i.confidence for i in incidents]

        costs = [c.cost_usd for c in calls if c.cost_usd is not None]
        cost_per_incident = (sum(costs) / len(incidents)) if incidents else None
        pricing_unknown_calls = sum(1 for c in calls if c.pricing_unknown)

        return ObservabilitySnapshot(
            total_agent_calls=total_calls,
            agent_call_failures=failures,
            agent_failure_rate=(failures / total_calls) if total_calls else 0.0,
            per_agent_avg_latency_ms=per_agent_avg,
            total_incidents=len(incidents),
            end_to_end_latency_p50_ms=p50,
            end_to_end_latency_p95_ms=p95,
            routing_decision_counts=routing_counts,
            confidence_mean=statistics.mean(confidences) if confidences else 0.0,
            confidence_min=min(confidences) if confidences else 0.0,
            confidence_max=max(confidences) if confidences else 0.0,
            collector_failures=collector_failures,
            cost_per_incident_usd=cost_per_incident,
            pricing_unknown_calls=pricing_unknown_calls,
        )


# Module-level singleton — the orchestrator and any reader (CLI/API) share one recorder per
# process. Not persisted across process restarts; see module docstring for why.
RECORDER = _MetricsRecorder()
