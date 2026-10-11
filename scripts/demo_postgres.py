"""
Real Postgres integration demo: collect live DB metrics and run the full InfraGuardian pipeline.

Connects to a running Postgres instance (default: the docker-compose demo container), collects
one sample via `PostgresCollector`, and passes it through the LangGraph orchestrator. The
pipeline detects anomalies (if any), invokes the DB specialist agent, synthesizes a report, and
routes it through the safety gates — all against real, not simulated, metric data.

Usage:
    docker compose up -d                                          # start Postgres
    uv run python scripts/demo_postgres_load.py &                 # create connection pressure
    uv run python scripts/demo_postgres.py                        # this script

    # or, without the load generator (healthy baseline):
    uv run python scripts/demo_postgres.py

Set INFRAGUARDIAN_MODEL and the provider's API key to use a real model; otherwise runs against
the offline FakeInfraGuardianModel (same as `demo_incident.py`).
"""

from __future__ import annotations

import json
import os
import sys

from orchestrator import run_incident
from tools.collectors import PostgresCollector
from tools.metrics import Domain


def main() -> None:
    dsn = os.environ.get(
        "POSTGRES_DSN", "postgresql://infraguardian:localdev@localhost:5432/infraguardian"
    )
    print(f"Collecting live DB metrics from Postgres...")
    print(f"DSN: {dsn}\n")

    try:
        collector = PostgresCollector(dsn=dsn)
        sample = collector.collect()
    except Exception as exc:
        print(f"Failed to collect metrics: {exc}", file=sys.stderr)
        print("Is the Postgres container running? Try: docker compose up -d", file=sys.stderr)
        raise SystemExit(1)

    print("Collected sample:")
    print(f"  connections_active: {sample.connections_active}")
    print(f"  connections_max:    {sample.connections_max}")
    utilization = 100 * sample.connections_active / sample.connections_max
    print(f"  utilization:        {utilization:.1f}%")
    print(f"  query_latency_p95:  {sample.query_latency_p95_ms:.2f} ms")
    print(f"  replication_lag:    {sample.replication_lag_sec:.1f} s")
    print(f"  error_rate:         {sample.error_rate_pct:.2f}%")
    print()

    print("Running InfraGuardian pipeline...\n")
    report = run_incident({Domain.DB: sample}, persist=True)

    print(json.dumps(json.loads(report.model_dump_json()), indent=2))
    print(f"\nSeverity:         {report.severity.value}")
    print(f"Confidence:       {report.confidence:.2f}")
    print(f"Routing decision: {report.routing_decision.value}")
    print(f"Selected runbook: {report.selected_runbook_id or '(none)'}")

    if report.affected_domains:
        print(f"\nAnomaly detected in: {', '.join(d.value for d in report.affected_domains)}")
    else:
        print("\nNo anomalies detected — Postgres is healthy.")


if __name__ == "__main__":
    main()
