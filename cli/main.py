"""
CLI for InfraGuardian (PRD FR-17): run predefined scenarios and inspect structured output.

Distinct from `scripts/demo_incident.py` (an internal manual-verification script) in that this
is the user-facing interface the PRD's FR-17 asks for — supports listing available scenarios
and selecting JSON vs. human-readable output, which the demo script doesn't need.

Usage:
    uv run python -m cli.main list
    uv run python -m cli.main analyze db_connection_pool_exhaustion [more_scenarios ...]
    uv run python -m cli.main analyze db_connection_pool_exhaustion --json
    uv run python -m cli.main live
    uv run python -m cli.main watch --interval 15
"""

from __future__ import annotations

import argparse
import sys
import time

from agents.schemas import Severity
from orchestrator import run_incident
from storage import incident_store
from tools.collectors import PsutilCollector
from tools.observability import RECORDER
from tools.scenarios import SCENARIOS

# `StrEnum` members compare by string value, not severity order, so escalation detection in
# `_should_alert` needs an explicit rank table (same pattern as `tools/llm.py::_SEVERITY_RANK`).
_SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


def _cmd_list(_args: argparse.Namespace) -> None:
    """Print every predefined scenario name, one per line, sorted."""
    for name in sorted(SCENARIOS):
        print(name)


def _print_report(report) -> None:
    """Human-readable summary shared by `analyze` and `live`."""
    print(f"Incident {report.incident_id}")
    print(f"  Severity:          {report.severity.value}")
    print(f"  Confidence:        {report.confidence:.2f}")
    print(f"  Affected domains:  {', '.join(d.value for d in report.affected_domains) or '(none)'}")
    print(f"  Routing decision:  {report.routing_decision.value}")
    print(f"  Selected runbook:  {report.selected_runbook_id or '(none)'}")
    print(f"  Root cause:        {report.unified_root_cause}")


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

    _print_report(report)


def _cmd_live(args: argparse.Namespace) -> None:
    """
    Collect one real CPU/mem/swap/load sample from this host via `PsutilCollector` and run it
    through the full pipeline — proof the collector abstraction (FR-03) works end to end against
    real metrics, not just replayed fixtures.
    """
    sample = PsutilCollector().collect()
    report = run_incident({sample.domain: sample}, persist=not args.no_persist)

    if args.json:
        print(report.model_dump_json(indent=2))
        return

    print(
        f"Live sample: cpu_pct={sample.cpu_pct:.1f} mem_pct={sample.mem_pct:.1f} "
        f"swap_pct={sample.swap_pct:.1f} load_avg_1m={sample.load_avg_1m:.2f}\n"
    )
    _print_report(report)


def _notify(title: str, message: str) -> None:
    """
    Best-effort desktop notification (Windows toast / macOS notification center via `plyer`).

    Imported lazily, inside the function, so this module stays importable (and the test suite
    stays green) on a machine that hasn't run `uv sync` since `plyer` was added — mirrors the
    lazy-provider-import convention in `tools/llm.py`. A failure here (missing backend, headless
    environment, etc.) must never crash the watch loop — it's surfaced as a one-line stderr
    warning instead (fail-soft, not fail-silent, per the PRD's "tool failures are represented
    explicitly, never swallowed" invariant) since a desktop notification is a convenience, not a
    correctness-critical signal — the alert is already printed to stdout regardless.
    """
    try:
        from plyer import notification

        notification.notify(title=title, message=message, timeout=10)
    except Exception as exc:  # noqa: BLE001 - any backend failure degrades to a warning, never a crash
        print(f"[watch] desktop notification failed ({exc}); alert was still printed above.", file=sys.stderr)


def _should_alert(
    is_anomalous: bool,
    severity: Severity | None,
    prev_anomalous: bool,
    prev_alerted_severity: Severity | None,
) -> bool:
    """
    Edge-triggered alerting rule for `watch`: fire on the healthy->anomalous transition, and
    again if severity escalates while still anomalous. Never re-alerts on an unchanged ongoing
    anomaly or on recovery — a real on-call tool that pages on every tick of a flapping metric
    trains people to ignore it, which is the opposite of the point.
    """
    if not is_anomalous:
        return False
    if not prev_anomalous:
        return True
    if prev_alerted_severity is None:
        return True
    return _SEVERITY_ORDER[severity] > _SEVERITY_ORDER[prev_alerted_severity]


def _run_watch_loop(
    collector: PsutilCollector,
    interval: float,
    persist: bool = True,
    max_ticks: int | None = None,
    notify=_notify,
    sleep=time.sleep,
) -> None:
    """
    Poll `collector` every `interval` seconds and run each sample through the full pipeline.

    Healthy ticks print a quiet one-line heartbeat and are never persisted — `run_incident` is
    always called with `persist=False` here, because unlike `analyze`/`live` (one-shot, so
    unconditional persistence is fine) a long-running watcher would otherwise flood the
    `incidents` table with thousands of identical healthy rows. An anomalous tick is persisted
    explicitly via `incident_store.save_incident` (unless `persist=False` was passed) and, per
    `_should_alert`, may trigger a bannered alert + desktop notification. `max_ticks=None` runs
    forever (the real CLI usage); a finite value makes this deterministic for tests/demos.
    """
    prev_anomalous = False
    prev_alerted_severity: Severity | None = None
    tick = 0

    while max_ticks is None or tick < max_ticks:
        sample = collector.collect()
        report = run_incident({sample.domain: sample}, persist=False)
        is_anomalous = bool(report.affected_domains)

        timestamp = report.timestamp.isoformat(timespec="seconds")
        print(
            f"[{timestamp}] cpu={sample.cpu_pct:.1f} mem={sample.mem_pct:.1f} "
            f"swap={sample.swap_pct:.1f} load={sample.load_avg_1m:.2f} "
            f"severity={report.severity.value}"
        )

        if is_anomalous:
            if persist:
                incident_store.save_incident(report)
            if _should_alert(is_anomalous, report.severity, prev_anomalous, prev_alerted_severity):
                print(f"\n{'=' * 60}\nALERT — {report.severity.value.upper()} severity incident\n{'=' * 60}")
                _print_report(report)
                print()
                notify(
                    f"InfraGuardian: {report.severity.value.upper()} incident",
                    report.unified_root_cause,
                )
                prev_alerted_severity = report.severity
        else:
            prev_alerted_severity = None

        prev_anomalous = is_anomalous
        tick += 1
        if max_ticks is None or tick < max_ticks:
            sleep(interval)


def _cmd_watch(args: argparse.Namespace) -> None:
    """
    Continuously poll this host's real CPU/mem/swap/load and surface anomalies as they happen.

    Unlike `live` (one sample, one report, exit), `watch` is the actual "agentic infrastructure
    incident-response system" pitch running against a live machine: quiet while healthy, loud
    (bannered report + desktop toast) the moment something trips a threshold or escalates.
    `KeyboardInterrupt` is expected (Ctrl+C is how a human stops this) — exit cleanly, no
    traceback.
    """
    try:
        _run_watch_loop(
            PsutilCollector(),
            interval=args.interval,
            persist=not args.no_persist,
            max_ticks=args.max_ticks,
        )
    except KeyboardInterrupt:
        print("\nStopped.")


def _cmd_metrics(_args: argparse.Namespace) -> None:
    """Print the current process's self-observability snapshot (PRD §11) as JSON."""
    print(RECORDER.snapshot().model_dump_json(indent=2))


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

    live_parser = subparsers.add_parser(
        "live", help="Collect one real CPU/mem/swap/load sample from this host and analyze it."
    )
    live_parser.add_argument(
        "--json", action="store_true", help="Print the full IncidentReport as JSON."
    )
    live_parser.add_argument(
        "--no-persist", action="store_true", help="Don't write the result to the SQLite incident store."
    )
    live_parser.set_defaults(func=_cmd_live)

    watch_parser = subparsers.add_parser(
        "watch", help="Continuously poll this host's real CPU/mem/swap/load and alert on anomalies."
    )
    watch_parser.add_argument(
        "--interval", type=float, default=15.0, help="Seconds between samples (default: 15)."
    )
    watch_parser.add_argument(
        "--max-ticks", type=int, default=None, help="Stop after N samples (default: run forever)."
    )
    watch_parser.add_argument(
        "--no-persist", action="store_true", help="Don't write anomalous incidents to the SQLite incident store."
    )
    watch_parser.set_defaults(func=_cmd_watch)

    metrics_parser = subparsers.add_parser(
        "metrics", help="Print this process's self-observability snapshot (PRD §11)."
    )
    metrics_parser.set_defaults(func=_cmd_metrics)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
