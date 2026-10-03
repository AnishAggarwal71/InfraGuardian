"""
CLI for InfraGuardian (PRD FR-17): run predefined scenarios and inspect structured output.

Distinct from `scripts/demo_incident.py` (an internal manual-verification script) in that this
is the user-facing interface the PRD's FR-17 asks for — supports listing available scenarios
and selecting JSON vs. human-readable output, which the demo script doesn't need.

Usage:
    uv run python -m cli.main list
    uv run python -m cli.main analyze db_connection_pool_exhaustion [more_scenarios ...]
    uv run python -m cli.main analyze db_connection_pool_exhaustion --json
"""

from __future__ import annotations

import argparse
import sys

from orchestrator import run_incident
from tools.scenarios import SCENARIOS


def _cmd_list(_args: argparse.Namespace) -> None:
    """Print every predefined scenario name, one per line, sorted."""
    for name in sorted(SCENARIOS):
        print(name)


def _cmd_analyze(args: argparse.Namespace) -> None:
    """Resolve the requested scenario names to samples, run the pipeline, and print the report."""
    samples = {}
    for name in args.scenarios:
        if name not in SCENARIOS:
            print(f"Unknown scenario '{name}'. Run 'list' to see available scenarios.", file=sys.stderr)
            raise SystemExit(1)
        final_sample = SCENARIOS[name][-1]
        samples[final_sample.domain] = final_sample

    report = run_incident(samples, persist=not args.no_persist)

    if args.json:
        print(report.model_dump_json(indent=2))
        return

    print(f"Incident {report.incident_id}")
    print(f"  Severity:          {report.severity.value}")
    print(f"  Confidence:        {report.confidence:.2f}")
    print(f"  Affected domains:  {', '.join(d.value for d in report.affected_domains) or '(none)'}")
    print(f"  Routing decision:  {report.routing_decision.value}")
    print(f"  Selected runbook:  {report.selected_runbook_id or '(none)'}")
    print(f"  Root cause:        {report.unified_root_cause}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="infraguardian", description="InfraGuardian CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List available predefined scenarios.")
    list_parser.set_defaults(func=_cmd_list)

    analyze_parser = subparsers.add_parser(
        "analyze", help="Run one or more predefined scenarios through the full pipeline."
    )
    analyze_parser.add_argument("scenarios", nargs="+", help="Scenario name(s) from 'list'.")
    analyze_parser.add_argument(
        "--json", action="store_true", help="Print the full IncidentReport as JSON."
    )
    analyze_parser.add_argument(
        "--no-persist", action="store_true", help="Don't write the result to the SQLite incident store."
    )
    analyze_parser.set_defaults(func=_cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
