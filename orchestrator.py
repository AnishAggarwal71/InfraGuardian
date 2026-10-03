"""
LangGraph orchestrator (PRD FR-09, FR-10, FR-11).

Wires the M1 rule pre-screen, the three domain specialists, and a cross-domain synthesis step
into a single stateful graph:

    screen -> (conditional fan-out to only anomalous domains' specialists) -> synthesize

`screen` enforces FR-06 structurally: specialists for healthy domains are never added as nodes
to invoke for a given run, not just skipped by convention. `synthesize` enforces FR-10 by
asking the model to explicitly state cross-domain relationships and their supporting evidence,
and short-circuits (no LLM call) when zero specialists fired, keeping the "healthy domains don't
reach the LLM" property intact even at the synthesis step.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from agents import cpu_mem_agent, db_agent, gpu_agent
from agents.schemas import IncidentReport, Severity, SynthesisOutput
from tools.llm import get_chat_model
from tools.metrics import Domain, MetricSample
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


def _make_specialist_node(domain: Domain):
    def _node(state: IncidentState) -> IncidentState:
        sample = state["samples"][domain]
        detection = state["detections"][domain]
        finding = _SPECIALISTS[domain](sample, detection)
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
            automation_safety_reason="No incident — nothing requires action.",
        )
        return {"report": report}

    synthesis_model = get_chat_model().with_structured_output(SynthesisOutput)
    synthesis: SynthesisOutput = synthesis_model.invoke(_build_synthesis_prompt(findings))

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
        confidence=synthesis.confidence,
        predictive_risk=synthesis.predictive_risk,
        priority_ordered_actions=synthesis.priority_ordered_actions,
        automation_safety_reason=synthesis.automation_safety_reason,
    )
    return {"report": report}


def _build_graph() -> Any:
    graph = StateGraph(IncidentState)
    graph.add_node("screen", _screen)
    for domain in Domain:
        graph.add_node(domain.value, _make_specialist_node(domain))
        graph.add_edge(domain.value, "synthesize")
    graph.add_node("synthesize", _synthesize)

    graph.set_entry_point("screen")
    graph.add_conditional_edges(
        "screen",
        _route_anomalous_domains,
        {domain.value: domain.value for domain in Domain} | {"synthesize": "synthesize"},
    )
    graph.add_edge("synthesize", END)
    return graph.compile()


_GRAPH = _build_graph()


def run_incident(samples: dict[Domain, MetricSample]) -> IncidentReport:
    """
    Entry point: run the full screen -> specialists -> synthesize pipeline for one set of
    per-domain samples (one sample per domain being analyzed in this incident) and return the
    unified incident report.
    """
    final_state = _GRAPH.invoke({"samples": samples})
    return final_state["report"]
