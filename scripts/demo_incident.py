"""
Manual demo for M2/M3: run a full scenario through the LangGraph orchestrator
(screen -> specialists -> synthesize -> route_and_remediate) and print the resulting
IncidentReport, including its routing decision and (if auto-remediated) the runbook result.

Persists to `infraguardian.db` by default (PRD §13) — inspect with e.g.:
    sqlite3 infraguardian.db "select incident_id, severity, routing_decision from incidents;"

Uses the offline fake model by default (no API key needed). Set INFRAGUARDIAN_MODEL (and the
provider's API key env var) to run against a real model instead — e.g.:

    INFRAGUARDIAN_MODEL=anthropic:claude-sonnet-4-5-20250929 ANTHROPIC_API_KEY=... \
        uv run python -m scripts.demo_incident db_connection_pool_exhaustion

Usage: uv run python -m scripts.demo_incident [scenario_name ...]
"""

from __future__ import annotations

import sys

from orchestrator import run_incident
from tools.scenarios import SCENARIOS

DEFAULT_SCENARIOS = ["db_connection_pool_exhaustion"]


def main() -> None:
    scenario_names = sys.argv[1:] or DEFAULT_SCENARIOS
    samples = {}
    for name in scenario_names:
        if name not in SCENARIOS:
            print(f"Unknown scenario '{name}'. Available: {', '.join(sorted(SCENARIOS))}")
            raise SystemExit(1)
        final_sample = SCENARIOS[name][-1]
        samples[final_sample.domain] = final_sample

    print(f"Analyzing: {', '.join(scenario_names)}\n")
    report = run_incident(samples, persist=True)
    print(report.model_dump_json(indent=2))
    print(
        f"\nRouting decision: {report.routing_decision.value}"
        f"\nSelected runbook: {report.selected_runbook_id or '(none)'}"
    )


if __name__ == "__main__":
    main()

