"""
Manual demo for M2: run a full scenario through the LangGraph orchestrator
(screen -> specialists -> synthesize) and print the resulting IncidentReport.

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
    report = run_incident(samples)
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
