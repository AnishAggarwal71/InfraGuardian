"""
Live-model evaluation harness (`REAL_MODEL_VALIDATION.md` P1): runs `INCIDENT_CASES` against a
real provider/model, repeated N times per case to measure run-to-run variance, and writes an
immutable run manifest plus a human-readable summary.

This file makes zero LLM calls on its own. It is a harness only — point it at your own API key
and model and run it yourself:

    ANTHROPIC_API_KEY=... uv run python -m evaluation.live_evaluator \\
        --model anthropic:claude-sonnet-4-5-20250929 --repeats 5

Omitting `--model` runs against the offline `FakeInfraGuardianModel` instead (free, deterministic)
— that's how this harness's own test coverage (`tests/test_live_evaluator.py`) exercises it
without spending real API budget.

Deliberately separate from `evaluation/evaluator.py` (fake-model, free, CI-gated on every push)
for the same reason given in `REAL_MODEL_VALIDATION.md`: real-model numbers are a dated snapshot
against one named model/run, not a continuously-enforced accuracy claim, so they must never block
a merge or require a provider key in CI.

Each (case, repeat) run gets its own `INFRAGUARDIAN_TRACE_DIR`, so every prompt/raw-response/
usage-metadata event for that single run lives in its own `events.jsonl` — this satisfies
`REAL_MODEL_VALIDATION.md`'s acceptance test for "per-call trace IDs" via directory structure
instead of a bare id, and keeps one case's trace from ever mixing into another's.
"""

from __future__ import annotations

import argparse
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from evaluation.fixtures.incident_cases import INCIDENT_CASES, IncidentCase
from orchestrator import run_incident
from tools.llm import get_cumulative_cost_usd
from tools.observability import RECORDER
from tools.scenarios import SCENARIOS

_RUNS_DIR = Path(__file__).parent / "real-runs"
_TRACE_DIR_ENV = "INFRAGUARDIAN_TRACE_DIR"
_MODEL_ENV = "INFRAGUARDIAN_MODEL"

# PRD §9 / project CLAUDE.md's stated, non-negotiable auto-remediation gate — mirrored here (not
# imported from `tools.routing`'s private constants) so this harness checks the gate as an
# external observer of `IncidentReport` fields, the same relationship a real auditor would have.
_AUTO_REMEDIATE_CONFIDENCE_MIN = 0.8


class LiveCaseResult(BaseModel):
    """One (case, repeat) run's actual outcome vs. expectation, plus cost/latency/trace pointer."""

    case_name: str
    repeat_index: int
    trace_dir: str
    error: str | None = None
    actual_severity: str | None = None
    actual_routing: str | None = None
    actual_runbook_id: str | None = None
    actual_rca: str | None = None
    severity_match: bool = False
    routing_match: bool = False
    rca_match: bool = False
    false_auto_remediation: bool = False
    critical_auto_remediated: bool = False
    underconfident_auto_remediated: bool = False
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    agent_calls: int = 0
    agent_call_failures: int = 0
    pricing_unknown_calls: int = 0


class LiveCaseSummary(BaseModel):
    """Per-case aggregate across its repeats — this is the run-to-run variance measurement."""

    case_name: str
    repeats: int
    severity_accuracy: float
    routing_accuracy: float
    rca_accuracy: float
    distinct_severities_seen: int
    distinct_routing_decisions_seen: int
    safety_invariant_violations: int
    avg_latency_ms: float
    avg_cost_usd: float
    error_count: int


class LiveRunManifest(BaseModel):
    """The complete, immutable record of one `live_evaluator` invocation — written once, read many."""

    run_id: str
    model: str
    started_at: datetime
    finished_at: datetime
    repeats_per_case: int
    case_count: int
    results: list[LiveCaseResult]
    case_summaries: list[LiveCaseSummary]
    overall_severity_accuracy: float
    overall_routing_accuracy: float
    overall_rca_accuracy: float
    total_safety_invariant_violations: int
    latency_p50_ms: float
    latency_p95_ms: float
    total_cost_usd: float
    pricing_unknown_calls: int
    trace_root_dir: str


def _samples_for(case: IncidentCase) -> dict:
    """Resolve a case's scenario names to the final (most-anomalous) sample per domain."""
    samples = {}
    for name in case.scenario_names:
        final_sample = SCENARIOS[name][-1]
        samples[final_sample.domain] = final_sample
    return samples


def _run_once(case: IncidentCase, repeat_index: int, trace_root: Path) -> LiveCaseResult:
    """
    Run one case once against whatever `tools.llm.get_chat_model()` currently resolves to.

    Points `INFRAGUARDIAN_TRACE_DIR` at a run-specific subdirectory for the duration of the call
    only, restoring whatever was set before — this harness must not leak tracing state into
    whatever runs after it in the same process (e.g. a later case, or a caller's own code).
    """
    trace_dir = trace_root / case.name / f"repeat_{repeat_index}"
    previous_trace_dir = os.environ.get(_TRACE_DIR_ENV)
    os.environ[_TRACE_DIR_ENV] = str(trace_dir)

    before = RECORDER.snapshot()
    start = time.perf_counter()
    try:
        try:
            report = run_incident(_samples_for(case), persist=False)
        except Exception as exc:  # noqa: BLE001 - a live-provider failure is itself a result, not a crash
            after = RECORDER.snapshot()
            return LiveCaseResult(
                case_name=case.name,
                repeat_index=repeat_index,
                trace_dir=str(trace_dir),
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - start) * 1000,
                agent_calls=after.total_agent_calls - before.total_agent_calls,
                agent_call_failures=after.agent_call_failures - before.agent_call_failures,
                pricing_unknown_calls=after.pricing_unknown_calls - before.pricing_unknown_calls,
            )
    finally:
        if previous_trace_dir is None:
            os.environ.pop(_TRACE_DIR_ENV, None)
        else:
            os.environ[_TRACE_DIR_ENV] = previous_trace_dir

    after = RECORDER.snapshot()
    rca_match = (
        bool(report.affected_domains)
        and set(report.affected_domains) == case.expected_affected_domains
        and all(
            keyword.lower() in report.unified_root_cause.lower()
            for keyword in case.expected_rca_domain_keywords.values()
        )
    )
    severity_match = report.severity == case.expected_severity
    routing_match = report.routing_decision == case.expected_routing
    if routing_match and case.expected_runbook_id is not None:
        routing_match = report.selected_runbook_id == case.expected_runbook_id

    false_auto_remediation = (
        report.routing_decision.value == "auto_remediate"
        and case.expected_routing.value != "auto_remediate"
    )
    # Live-observer checks of PRD §9's non-negotiable invariants — not expected to ever fire
    # (routing.py already enforces both deterministically), but checking the live report's own
    # fields is exactly how the original ECC-severity P0 was first caught against a real model.
    critical_auto_remediated = (
        report.severity.value == "critical" and report.routing_decision.value == "auto_remediate"
    )
    underconfident_auto_remediated = (
        report.routing_decision.value == "auto_remediate"
        and report.confidence < _AUTO_REMEDIATE_CONFIDENCE_MIN
    )

    return LiveCaseResult(
        case_name=case.name,
        repeat_index=repeat_index,
        trace_dir=str(trace_dir),
        actual_severity=report.severity.value,
        actual_routing=report.routing_decision.value,
        actual_runbook_id=report.selected_runbook_id,
        actual_rca=report.unified_root_cause,
        severity_match=severity_match,
        routing_match=routing_match,
        rca_match=rca_match,
        false_auto_remediation=false_auto_remediation,
        critical_auto_remediated=critical_auto_remediated,
        underconfident_auto_remediated=underconfident_auto_remediated,
        latency_ms=(time.perf_counter() - start) * 1000,
        cost_usd=get_cumulative_cost_usd(),
        agent_calls=after.total_agent_calls - before.total_agent_calls,
        agent_call_failures=after.agent_call_failures - before.agent_call_failures,
        pricing_unknown_calls=after.pricing_unknown_calls - before.pricing_unknown_calls,
    )


def _summarize_case(case_name: str, results: list[LiveCaseResult]) -> LiveCaseSummary:
    """Aggregate one case's repeats into accuracy rates and a run-to-run variance signal."""
    n = len(results)
    severities = {r.actual_severity for r in results if r.actual_severity is not None}
    routings = {r.actual_routing for r in results if r.actual_routing is not None}
    violations = sum(
        r.false_auto_remediation or r.critical_auto_remediated or r.underconfident_auto_remediated
        for r in results
    )
    return LiveCaseSummary(
        case_name=case_name,
        repeats=n,
        severity_accuracy=sum(r.severity_match for r in results) / n,
        routing_accuracy=sum(r.routing_match for r in results) / n,
        rca_accuracy=sum(r.rca_match for r in results) / n,
        distinct_severities_seen=len(severities),
        distinct_routing_decisions_seen=len(routings),
        safety_invariant_violations=violations,
        avg_latency_ms=sum(r.latency_ms for r in results) / n,
        avg_cost_usd=sum(r.cost_usd for r in results) / n,
        error_count=sum(r.error is not None for r in results),
    )


def run_live_evaluation(
    model: str | None,
    repeats: int,
    case_names: list[str] | None = None,
    max_total_cost_usd: float | None = None,
    runs_dir: Path | None = None,
) -> LiveRunManifest:
    """
    Run every (filtered) `IncidentCase` `repeats` times against `model`, writing a per-call JSONL
    trace under `<runs_dir>/<run_id>/` and returning the full manifest (also written to
    `<run_id>/manifest.json` for an immutable record independent of this call's return value).

    `model` is written to `INFRAGUARDIAN_MODEL` for the duration of this call (then restored) so
    `tools.llm.get_chat_model()` resolves to it; leaving it `None` runs against the offline fake
    model instead. `max_total_cost_usd` is this harness's own cumulative safety net across the
    whole run — on top of whatever `INFRAGUARDIAN_MAX_COST_USD_PER_RUN` already caps per single
    incident — once the running total would exceed it, remaining (case, repeat) combinations are
    skipped rather than started; the manifest still reports the completed ones honestly rather
    than silently truncating without a record of why.
    """
    previous_model = os.environ.get(_MODEL_ENV)
    if model is not None:
        os.environ[_MODEL_ENV] = model
    try:
        resolved_model = os.environ.get(_MODEL_ENV, "fake") or "fake"

        cases = INCIDENT_CASES
        if case_names:
            wanted = set(case_names)
            cases = [c for c in cases if c.name in wanted]

        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        trace_root = (runs_dir or _RUNS_DIR) / run_id
        started_at = datetime.now(timezone.utc)

        results: list[LiveCaseResult] = []
        running_total_cost = 0.0
        budget_exhausted = False
        for case in cases:
            for repeat_index in range(repeats):
                if budget_exhausted:
                    break
                result = _run_once(case, repeat_index, trace_root)
                results.append(result)
                running_total_cost += result.cost_usd
                if max_total_cost_usd is not None and running_total_cost >= max_total_cost_usd:
                    budget_exhausted = True

        finished_at = datetime.now(timezone.utc)

        by_case: dict[str, list[LiveCaseResult]] = {}
        for r in results:
            by_case.setdefault(r.case_name, []).append(r)
        case_summaries = [_summarize_case(name, rs) for name, rs in by_case.items()]

        n = len(results) or 1
        latencies = sorted(r.latency_ms for r in results)
        manifest = LiveRunManifest(
            run_id=run_id,
            model=resolved_model,
            started_at=started_at,
            finished_at=finished_at,
            repeats_per_case=repeats,
            case_count=len(cases),
            results=results,
            case_summaries=case_summaries,
            overall_severity_accuracy=sum(r.severity_match for r in results) / n,
            overall_routing_accuracy=sum(r.routing_match for r in results) / n,
            overall_rca_accuracy=sum(r.rca_match for r in results) / n,
            total_safety_invariant_violations=sum(
                s.safety_invariant_violations for s in case_summaries
            ),
            latency_p50_ms=latencies[len(latencies) // 2] if latencies else 0.0,
            latency_p95_ms=latencies[int(len(latencies) * 0.95)] if latencies else 0.0,
            total_cost_usd=sum(r.cost_usd for r in results),
            pricing_unknown_calls=sum(r.pricing_unknown_calls for r in results),
            trace_root_dir=str(trace_root),
        )

        trace_root.mkdir(parents=True, exist_ok=True)
        (trace_root / "manifest.json").write_text(manifest.model_dump_json(indent=2))
        return manifest
    finally:
        if previous_model is None:
            os.environ.pop(_MODEL_ENV, None)
        else:
            os.environ[_MODEL_ENV] = previous_model


def _print_summary(manifest: LiveRunManifest) -> None:
    """Human-readable console summary — the full detail always lives in the written manifest."""
    print(f"Run {manifest.run_id} — model={manifest.model}")
    print(
        f"Cases: {manifest.case_count}  Repeats/case: {manifest.repeats_per_case}  "
        f"Total runs: {len(manifest.results)}"
    )
    print(f"Severity accuracy:  {manifest.overall_severity_accuracy:.1%}")
    print(f"Routing accuracy:   {manifest.overall_routing_accuracy:.1%}")
    print(f"RCA accuracy:       {manifest.overall_rca_accuracy:.1%}")
    print(f"Safety-invariant violations: {manifest.total_safety_invariant_violations}")
    print(f"Latency p50 / p95 (ms): {manifest.latency_p50_ms:.1f} / {manifest.latency_p95_ms:.1f}")
    print(
        f"Total cost (USD): {manifest.total_cost_usd:.4f}  "
        f"(pricing-unknown calls: {manifest.pricing_unknown_calls})"
    )
    print(f"Manifest: {manifest.trace_root_dir}/manifest.json")
    print("\nPer-case:")
    for s in manifest.case_summaries:
        print(
            f"  {s.case_name}: severity={s.severity_accuracy:.0%} routing={s.routing_accuracy:.0%} "
            f"rca={s.rca_accuracy:.0%} distinct_severities={s.distinct_severities_seen} "
            f"distinct_routing={s.distinct_routing_decisions_seen} "
            f"violations={s.safety_invariant_violations} avg_latency_ms={s.avg_latency_ms:.0f} "
            f"avg_cost_usd={s.avg_cost_usd:.4f} errors={s.error_count}"
        )
    if manifest.total_safety_invariant_violations:
        print(
            f"\n*** {manifest.total_safety_invariant_violations} SAFETY-INVARIANT VIOLATION(S) "
            "— see manifest for detail. ***"
        )


def main() -> None:
    """CLI entrypoint: `uv run python -m evaluation.live_evaluator [options]`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=None,
        help="e.g. anthropic:claude-sonnet-4-5-20250929 (sets INFRAGUARDIAN_MODEL); "
        "omit to use the offline fake model",
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--cases", default=None, help="comma-separated subset of case names; omit to run all"
    )
    parser.add_argument(
        "--max-total-cost-usd",
        type=float,
        default=None,
        help="stop starting new (case, repeat) runs once the running total would exceed this",
    )
    args = parser.parse_args()

    case_names = args.cases.split(",") if args.cases else None
    manifest = run_live_evaluation(args.model, args.repeats, case_names, args.max_total_cost_usd)
    _print_summary(manifest)


if __name__ == "__main__":
    main()
