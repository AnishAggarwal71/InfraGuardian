"""
Manual demo for M1: play a scenario sample-by-sample through a SimulatorCollector and print the
rule pre-screen's classification at each step, to visually show the healthy-to-anomalous
transition (this is the thing the M1 milestone demo is: a human watching this run).

Usage: uv run python -m scripts.demo [scenario_name]
"""

from __future__ import annotations

import sys

from tools.collectors import SimulatorCollector
from tools.scenarios import SCENARIOS
from tools.thresholds import evaluate

DEFAULT_SCENARIO = "db_connection_pool_exhaustion"


def main() -> None:
    scenario_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    if scenario_name not in SCENARIOS:
        print(f"Unknown scenario '{scenario_name}'. Available: {', '.join(sorted(SCENARIOS))}")
        raise SystemExit(1)

    collector = SimulatorCollector(SCENARIOS[scenario_name])
    print(f"Playing scenario: {scenario_name} ({len(collector)} samples)\n")

    while True:
        try:
            sample = collector.collect()
        except StopIteration:
            break
        result = evaluate(sample)
        status = "ANOMALOUS" if result.is_anomalous else "healthy"
        breaches = ", ".join(
            f"{b.rule_name}={b.observed_value:.1f} (max {b.threshold:.1f})"
            for b in result.breached_rules
        )
        print(f"[{sample.timestamp.time()}] {status:10s} {breaches}")


if __name__ == "__main__":
    main()
