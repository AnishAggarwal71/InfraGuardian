"""
Self-observability for InfraGuardian (PRD §11): invocation counts, per-agent latency/failure
rate, end-to-end latency, routing/confidence distributions, and tool/collector failure counts.

This is a module-level, in-memory recorder (not persisted — PRD §11 doesn't ask for historical
observability storage, only that the signals exist and are inspectable) that the orchestrator
calls into as it runs. Kept intentionally separate from `storage/incident_store.py`: that module
is the durable incident audit trail (PRD §13); this one is operational telemetry about
InfraGuardian's own behavior, read via `snapshot()` from the CLI/API, not written to disk.

Token/cost tracking (PRD §11's "cost/incident") is NOT implemented: `with_structured_output()`
returns a parsed Pydantic instance, not the raw `AIMessage` a provider attaches token-usage
metadata to, so getting real token counts would require `include_raw=True` and reshaping every
specialist/synthesis call site. Documented here as a known gap rather than faked with a made-up
number — `cost_per_incident_usd` is always `None` in the snapshot until that's implemented.
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

    def record_agent_call(self, agent: str, latency_ms: float, success: bool) -> None:
        """Record one specialist/synthesis LLM invocation's outcome and latency."""
        with self._lock:
            self._agent_calls.append(
                AgentCallRecord(
                    agent=agent,
                    latency_ms=latency_ms,
                    success=success,
                    recorded_at=datetime.now(timezone.utc),
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
            cost_per_incident_usd=None,
        )


# Module-level singleton — the orchestrator and any reader (CLI/API) share one recorder per
# process. Not persisted across process restarts; see module docstring for why.
RECORDER = _MetricsRecorder()
