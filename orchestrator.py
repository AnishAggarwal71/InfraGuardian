"""
LangGraph orchestrator (PRD FR-09, FR-10, FR-11, FR-12-16).

Wires the M1 rule pre-screen, the three domain specialists, cross-domain synthesis, and the M3
routing/remediation gate into a single stateful graph:

    screen -> (conditional fan-out to only anomalous domains' specialists) -> synthesize
        -> route_and_remediate

`screen` enforces FR-06 structurally: specialists for healthy domains are never added as nodes
to invoke for a given run, not just skipped by convention. `synthesize` enforces FR-10 by
asking the model to explicitly state cross-domain relationships and their supporting evidence,
and short-circuits (no LLM call) when zero specialists fired, keeping the "healthy domains don't
reach the LLM" property intact even at the synthesis step. `route_and_remediate` applies the
deterministic FR-12-14 gates and, only for AUTO_REMEDIATE, executes the selected allowlisted
runbook and records the FR-16 audit row.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from agents import cpu_mem_agent, db_agent, gpu_agent
from agents.schemas import (
    AutomationSafety,
    IncidentReport,
    RoutingDecision,
    Severity,
    SpecialistFinding,
    SynthesisOutput,
)
from storage import incident_store
from tools.confidence import calibrate
from tools.llm import get_chat_model, get_last_cost_usd, invoke_structured
from tools.metrics import Domain, MetricSample
from tools.observability import RECORDER
from tools.remediation import RUNBOOKS
from tools.routing import route
from tools.thresholds import DetectionResult, evaluate

_SPECIALISTS = {
    Domain.DB: db_agent.diagnose,
    Domain.CPU_MEM: cpu_mem_agent.diagnose,
    Domain.GPU: gpu_agent.diagnose,
}


def _merge_findings(left: dict, right: dict) -> dict:
    """
    Reducer for the `findings` state key.

    Specialist nodes run as parallel LangGraph branches (FR-08) and each writes only its own
    domain's entry, but LangGraph requires an explicit merge function for any state key more
    than one node can write to in the same step — without this, two specialists firing
    concurrently raises `InvalidUpdateError`.
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class IncidentState(TypedDict, total=False):
    """Mutable state threaded through the graph for a single incident analysis run."""

    samples: dict[Domain, MetricSample]
    detections: dict[Domain, DetectionResult]
    findings: Annotated[dict[Domain, Any], _merge_findings]  # Domain -> SpecialistFinding
    report: IncidentReport


def _screen(state: IncidentState) -> IncidentState:
    """Run the deterministic rule pre-screen for every domain present in the input samples."""
    detections = {domain: evaluate(sample) for domain, sample in state["samples"].items()}
    return {"detections": detections}


def _route_anomalous_domains(state: IncidentState) -> list[str]:
    """
    Conditional edge: only domains the rule pre-screen flagged get routed to a specialist node.

    This is the structural enforcement of FR-06 — a healthy domain's node simply isn't in the
    returned list, so LangGraph never invokes it.
    """
    anomalous = [
        domain.value for domain, result in state["detections"].items() if result.is_anomalous
    ]
    return anomalous if anomalous else ["synthesize"]


def _sentinel_finding(domain: Domain, error: Exception) -> SpecialistFinding:
    """
    Fail-closed stand-in for a specialist finding when the LLM call raises or returns
    schema-invalid output (PRD §9: "tool/API failures must be represented explicitly" and
    "malformed output fails closed"). Severity is forced to `CRITICAL` — not because the
    underlying infrastructure issue is necessarily critical, but because an unassessed failure
    must trip the "critical never auto-remediates" gate unconditionally and escalate to a
    human, rather than risk a confidence/safety combination that happens to clear the
    auto-remediate bar despite nothing having actually been diagnosed.
    """
    return SpecialistFinding(
        domain=domain,
        is_anomalous=True,
        root_cause_hypothesis=f"{domain.value} specialist failed to produce a finding: {error}",
        severity=Severity.CRITICAL,
        predictive_risk="Unknown — specialist diagnosis failed, risk cannot be assessed.",
        evidence=[f"specialist_error: {error}"],
        automation_safety=AutomationSafety.UNKNOWN,
        automation_safety_reason="Specialist call failed; safety cannot be assessed, so this fails closed.",
    )


def _make_specialist_node(domain: Domain):
    def _node(state: IncidentState) -> IncidentState:
        sample = state["samples"][domain]
        detection = state["detections"][domain]
        start = time.perf_counter()
        try:
            finding = _SPECIALISTS[domain](sample, detection)
            RECORDER.record_agent_call(
                domain.value,
                (time.perf_counter() - start) * 1000,
                success=True,
                cost_usd=get_last_cost_usd(),
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure fails closed
            RECORDER.record_agent_call(domain.value, (time.perf_counter() - start) * 1000, success=False)
            finding = _sentinel_finding(domain, exc)
        return {"findings": {domain: finding}}

    return _node


def _build_synthesis_prompt(findings: dict[Domain, Any]) -> str:
    """
    Serialize findings into the block format `tools.llm._build_synthesis_output` parses for the
    fake model, and that a real model is instructed to read and reason over. Keeping one real
    LLM's reasoning and the fake model's parsing aligned to the same prompt shape is what lets
    the same orchestrator code paths be exercised in tests without a network call.
    """
    blocks = []
    for domain, finding in findings.items():
        blocks.append(
            "\n".join(
                [
                    f"domain: {domain.value}",
                    f"severity: {finding.severity.value}",
                    f"root_cause: {finding.root_cause_hypothesis}",
                    f"predictive_risk: {finding.predictive_risk}",
                    f"evidence: {'; '.join(finding.evidence)}",
                    f"automation_safety_reason: {finding.automation_safety_reason}",
                ]
            )
        )
    findings_block = "\n---\n".join(blocks)
    return (
        "You are correlating structured findings from independent infrastructure specialists "
        "into a single incident analysis. For each pair of domains present, state whether "
        "there is a relationship between their findings and what evidence supports it. Label "
        "any relationship as a hypothesis unless the evidence directly measures a causal link "
        "(e.g. a traced query), not just co-occurrence in time.\n\n"
        f"Specialist findings:\n{findings_block}\n\n"
        "Return a structured synthesis: unified root cause, correlations (with evidence and "
        "whether each is a causal hypothesis), overall severity, confidence (0-1), predictive "
        "risk, and priority-ordered recommended actions."
    )


def _sentinel_synthesis_output(error: Exception) -> SynthesisOutput:
    """
    Fail-closed stand-in for the synthesis LLM call (PRD §9, same rationale as
    `_sentinel_finding`). `CRITICAL` severity guarantees `route()` sends this to ESCALATE,
    never AUTO_REMEDIATE, regardless of what the underlying findings looked like.
    """
    return SynthesisOutput(
        unified_root_cause=f"Synthesis failed: {error}",
        correlations=[],
        severity=Severity.CRITICAL,
        confidence=0.0,
        predictive_risk="Unknown — synthesis failed, risk cannot be assessed.",
        priority_ordered_actions=[],
        automation_safety_reason="Synthesis call failed; safety cannot be assessed, so this fails closed.",
    )


def _synthesize(state: IncidentState) -> IncidentState:
    findings = state.get("findings", {})
    incident_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    sample_timestamps = [s.timestamp for s in state["samples"].values()]
    duration_sec = (
        (max(sample_timestamps) - min(sample_timestamps)).total_seconds()
        if sample_timestamps
        else 0.0
    )
    affected_domains = sorted(findings.keys(), key=lambda d: d.value)
    observed_anomalies = [
        f"{domain.value}: {', '.join(b.rule_name for b in state['detections'][domain].breached_rules)}"
        for domain in affected_domains
    ]
    evidence = [e for f in findings.values() for e in f.evidence]

    if not findings:
        report = IncidentReport(
            incident_id=incident_id,
            timestamp=now,
            duration_sec=duration_sec,
            affected_domains=[],
            observed_anomalies=[],
            evidence=[],
            unified_root_cause="No anomalies detected; all monitored domains are healthy.",
            correlations=[],
            severity=Severity.LOW,
            confidence=1.0,
            predictive_risk="None.",
            priority_ordered_actions=[],
            # No incident exists to route — RECOMMEND is the closest fit of the three
            # decisions (there's nothing to auto-remediate or escalate) with zero actions.
            routing_decision=RoutingDecision.RECOMMEND,
            automation_safety_reason="No incident — nothing requires action.",
        )
        return {"report": report}

    _start = time.perf_counter()
    try:
        synthesis: SynthesisOutput = invoke_structured(
            get_chat_model(), SynthesisOutput, _build_synthesis_prompt(findings)
        )
        RECORDER.record_agent_call(
            "synthesis",
            (time.perf_counter() - _start) * 1000,
            success=True,
            cost_usd=get_last_cost_usd(),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure fails closed
        RECORDER.record_agent_call("synthesis", (time.perf_counter() - _start) * 1000, success=False)
        synthesis = _sentinel_synthesis_output(exc)

    calibrated_confidence = calibrate(synthesis.confidence, findings, synthesis.correlations)

    report = IncidentReport(
        incident_id=incident_id,
        timestamp=now,
        duration_sec=duration_sec,
        affected_domains=affected_domains,
        observed_anomalies=observed_anomalies,
        evidence=evidence,
        unified_root_cause=synthesis.unified_root_cause,
        correlations=synthesis.correlations,
        severity=synthesis.severity,
        confidence=calibrated_confidence,
        predictive_risk=synthesis.predictive_risk,
        priority_ordered_actions=synthesis.priority_ordered_actions,
        # Placeholder until `route_and_remediate` runs next; every report leaving the graph has
        # this overwritten with a real decision.
        routing_decision=RoutingDecision.ESCALATE,
        automation_safety_reason=synthesis.automation_safety_reason,
    )
    return {"report": report}


def _route_and_remediate(state: IncidentState) -> IncidentState:
    """
    Apply the FR-12-14 routing gates to the synthesized report and, only for AUTO_REMEDIATE,
    execute the selected allowlisted runbook and record the FR-16 audit row. Idempotency (PRD
    §9) is enforced by checking `was_already_executed` before calling `execute()`.
    """
    report = state["report"]
    findings = state.get("findings", {})

    if not findings:
        # Healthy/no-incident case already has its final routing_decision set in `_synthesize`.
        return {"report": report}

    routing_result = route(report, findings)

    updates: dict[str, Any] = {"routing_decision": routing_result.decision}

    if routing_result.decision == RoutingDecision.AUTO_REMEDIATE and routing_result.runbook_id:
        runbook = RUNBOOKS[routing_result.runbook_id]
        updates["selected_runbook_id"] = runbook.id
        updates["recommended_or_executed_runbook"] = runbook.description

        if not incident_store.was_already_executed(report.incident_id, runbook.id):
            result = runbook.execute(report.incident_id)
            incident_store.record_remediation_action(
                incident_id=report.incident_id,
                runbook_id=runbook.id,
                rationale=routing_result.reason,
                confidence=report.confidence,
                result=result,
            )
    elif routing_result.runbook_id:
        updates["selected_runbook_id"] = routing_result.runbook_id
        updates["recommended_or_executed_runbook"] = RUNBOOKS[routing_result.runbook_id].description

    updated_report = report.model_copy(update=updates)
    return {"report": updated_report}


def _build_graph() -> Any:
    graph = StateGraph(IncidentState)
    graph.add_node("screen", _screen)
    for domain in Domain:
        graph.add_node(domain.value, _make_specialist_node(domain))
        graph.add_edge(domain.value, "synthesize")
    graph.add_node("synthesize", _synthesize)
    graph.add_node("route_and_remediate", _route_and_remediate)

    graph.set_entry_point("screen")
    graph.add_conditional_edges(
        "screen",
        _route_anomalous_domains,
        {domain.value: domain.value for domain in Domain} | {"synthesize": "synthesize"},
    )
    graph.add_edge("synthesize", "route_and_remediate")
    graph.add_edge("route_and_remediate", END)
    return graph.compile()


_GRAPH = _build_graph()


def run_incident(samples: dict[Domain, MetricSample], persist: bool = True) -> IncidentReport:
    """
    Entry point: run the full screen -> specialists -> synthesize -> route_and_remediate
    pipeline for one set of per-domain samples (one sample per domain being analyzed in this
    incident) and return the unified incident report.

    `persist` controls whether the final report is written to the SQLite incident store
    (PRD §13) — it's a side effect of this entrypoint, not a graph node, so tests can pass
    `persist=False` to avoid touching a real DB file.

    Always records end-to-end latency, routing decision, and confidence to
    `tools.observability.RECORDER` (PRD §11) regardless of `persist`, since self-observability
    is not the same concern as incident durability.
    """
    start = time.perf_counter()
    final_state = _GRAPH.invoke({"samples": samples})
    report = final_state["report"]
    RECORDER.record_incident(
        (time.perf_counter() - start) * 1000, report.routing_decision, report.confidence
    )
    if persist:
        incident_store.save_incident(report)
    return report

