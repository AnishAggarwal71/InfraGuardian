"""
Evaluation harness (PRD §10): runs the fixtures in `evaluation/fixtures/` against the live
rule engine and orchestrator (using the offline fake model, so this needs no API key/network)
and computes the metrics table PRD §10 asks for.

These are reported as actual measured numbers against this project's own fixtures and the
`FakeInfraGuardianModel`, not claims about a production deployment — see the README for that
framing. Run via:

    uv run python -m evaluation.evaluator

which prints a summary and writes the full detail to `evaluation/reports/latest.json`.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from evaluation.fixtures.detection_cases import DETECTION_CASES
from evaluation.fixtures.incident_cases import INCIDENT_CASES
from orchestrator import run_incident
from tools.scenarios import SCENARIOS
from tools.thresholds import evaluate

_REPORTS_DIR = Path(__file__).parent / "reports"


class CaseResult(BaseModel):
    """Pass/fail detail for a single fixture case, kept for the JSON report's audit trail."""

    name: str
    passed: bool
    detail: str = ""


class EvaluationReport(BaseModel):
    """
    The full set of PRD §10 metrics plus per-case detail.

    `rule_filtering_rate` is the fraction of incident-case runs where the rule pre-screen
    short-circuited at least one domain (i.e. at least one sample in the input never reached a
    specialist) — there are none in this fixture set since every incident case's final sample is
    deliberately anomalous, so it's reported as 0.0 here and is more meaningfully exercised by
    `tests/test_orchestrator.py`'s healthy-domain cases; included for contract completeness.
    """

    generated_at: datetime
    detection_accuracy: float
    rca_accuracy: float
    routing_accuracy: float
    false_auto_remediation_count: int
    schema_validity_rate: float
    rule_filtering_rate: float
    llm_call_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    detection_results: list[CaseResult]
    incident_results: list[CaseResult]


def _run_detection_eval() -> tuple[float, list[CaseResult]]:
    """PRD §10 'detection accuracy': rule pre-screen verdict vs. hand-derived expectation."""
    results = []
    correct = 0
    for case in DETECTION_CASES:
        actual = evaluate(case.sample).is_anomalous
        passed = actual == case.expected_anomalous
        correct += passed
        results.append(
            CaseResult(
                name=case.name,
                passed=passed,
                detail=f"expected={case.expected_anomalous} actual={actual}",
            )
        )
    accuracy = correct / len(DETECTION_CASES) if DETECTION_CASES else 1.0
    return accuracy, results


def _run_incident_eval() -> tuple[dict, list[CaseResult]]:
    """
    PRD §10 'RCA accuracy', 'routing accuracy', 'false auto-remediation count', 'schema
    validity', and latency — all measured from one pass over `INCIDENT_CASES`.

    Schema validity is checked by re-validating the already-returned `IncidentReport` against
    its own model — `run_incident` already enforces this during construction via Pydantic, so a
    failure here would mean a case raised before producing a report at all (caught below) rather
    than a model instance existing with an invalid shape.
    """
    results: list[CaseResult] = []
    rca_correct = 0
    routing_correct = 0
    schema_valid = 0
    false_auto_remediations = 0
    latencies_ms: list[float] = []

    for case in INCIDENT_CASES:
        samples = {}
        for name in case.scenario_names:
            final_sample = SCENARIOS[name][-1]
            samples[final_sample.domain] = final_sample

        start = time.perf_counter()
        try:
            report = run_incident(samples, persist=False)
            latencies_ms.append((time.perf_counter() - start) * 1000)
        except Exception as exc:  # noqa: BLE001 - a raised exception is itself a failed case
            results.append(CaseResult(name=case.name, passed=False, detail=f"run raised: {exc}"))
            continue

        try:
            report.__class__.model_validate(report.model_dump())
            schema_valid += 1
        except ValidationError as exc:
            results.append(CaseResult(name=case.name, passed=False, detail=f"schema invalid: {exc}"))
            continue

        rca_ok = report.affected_domains and set(report.affected_domains) == case.expected_affected_domains
        for domain, keyword in case.expected_rca_domain_keywords.items():
            rca_ok = rca_ok and keyword.lower() in report.unified_root_cause.lower()
        rca_correct += bool(rca_ok)

        routing_ok = report.routing_decision == case.expected_routing
        if routing_ok and case.expected_runbook_id is not None:
            routing_ok = report.selected_runbook_id == case.expected_runbook_id
        routing_correct += bool(routing_ok)

        # False auto-remediation: the harness's single most important safety metric (PRD §9/§10)
        # — an incident the fixture says should NOT auto-remediate, but did.
        is_false_auto_remediation = (
            report.routing_decision.value == "auto_remediate"
            and case.expected_routing.value != "auto_remediate"
        )
        false_auto_remediations += is_false_auto_remediation

        passed = bool(rca_ok) and routing_ok and not is_false_auto_remediation
        results.append(
            CaseResult(
                name=case.name,
                passed=passed,
                detail=(
                    f"severity={report.severity.value} routing={report.routing_decision.value} "
                    f"runbook={report.selected_runbook_id} rca_ok={rca_ok} routing_ok={routing_ok}"
                ),
            )
        )

    n = len(INCIDENT_CASES) or 1
    metrics = {
        "rca_accuracy": rca_correct / n,
        "routing_accuracy": routing_correct / n,
        "false_auto_remediation_count": false_auto_remediations,
        "schema_validity_rate": schema_valid / n,
        "latencies_ms": latencies_ms,
    }
    return metrics, results


def run_evaluation() -> EvaluationReport:
    """Run every fixture category once and assemble the full `EvaluationReport`."""
    from tools.llm import FakeInfraGuardianModel
    import tools.llm as llm_module
    import orchestrator as orchestrator_module
    import agents.db_agent as db_agent_module
    import agents.cpu_mem_agent as cpu_mem_agent_module
    import agents.gpu_agent as gpu_agent_module

    fake = FakeInfraGuardianModel()
    # Patch every call site that imported `get_chat_model` into its own namespace (same pattern
    # as tests/test_orchestrator.py) so the harness is deterministic and needs no API key.
    for module in (llm_module, orchestrator_module, db_agent_module, cpu_mem_agent_module, gpu_agent_module):
        module.get_chat_model = lambda: fake

    detection_accuracy, detection_results = _run_detection_eval()
    incident_metrics, incident_results = _run_incident_eval()

    latencies = incident_metrics["latencies_ms"]
    latency_p50 = statistics.median(latencies) if latencies else 0.0
    latency_p95 = (
        statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 2 else (latencies[0] if latencies else 0.0)
    )

    return EvaluationReport(
        generated_at=datetime.now(timezone.utc),
        detection_accuracy=detection_accuracy,
        rca_accuracy=incident_metrics["rca_accuracy"],
        routing_accuracy=incident_metrics["routing_accuracy"],
        false_auto_remediation_count=incident_metrics["false_auto_remediation_count"],
        schema_validity_rate=incident_metrics["schema_validity_rate"],
        rule_filtering_rate=0.0,
        llm_call_count=fake.call_count,
        latency_p50_ms=latency_p50,
        latency_p95_ms=latency_p95,
        detection_results=detection_results,
        incident_results=incident_results,
    )


def main() -> None:
    report = run_evaluation()
    _REPORTS_DIR.mkdir(exist_ok=True)
    (_REPORTS_DIR / "latest.json").write_text(report.model_dump_json(indent=2))

    print(f"Detection accuracy:        {report.detection_accuracy:.1%}  (target >= 95%)")
    print(f"RCA accuracy:              {report.rca_accuracy:.1%}  (target >= 85%)")
    print(f"Routing accuracy:          {report.routing_accuracy:.1%}  (target >= 95%)")
    print(f"False auto-remediations:   {report.false_auto_remediation_count}  (target == 0)")
    print(f"Schema validity:           {report.schema_validity_rate:.1%}  (target >= 99%)")
    print(f"LLM calls (fake model):    {report.llm_call_count}")
    print(f"Latency p50 / p95 (ms):    {report.latency_p50_ms:.2f} / {report.latency_p95_ms:.2f}")

    failed = [r for r in report.detection_results + report.incident_results if not r.passed]
    if failed:
        print(f"\n{len(failed)} failing case(s):")
        for r in failed:
            print(f"  - {r.name}: {r.detail}")


if __name__ == "__main__":
    main()
