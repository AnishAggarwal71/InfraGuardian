"""
Load generator for the Postgres integration demo.

Opens many held connections to the demo Postgres container, pushing connection pool utilization
past the 85% threshold that triggers InfraGuardian's `connection_pool_utilization_pct` rule.
With `max_connections=50` in docker-compose, holding ~43 connections crosses that line.

Usage:
    docker compose up -d
    uv run python scripts/demo_postgres_load.py          # holds connections until Ctrl-C
    # in another terminal:
    uv run python scripts/demo_postgres.py               # collect real metrics → diagnose
"""

from __future__ import annotations

import os
import sys
import threading
import time

DSN = os.environ.get(
    "POSTGRES_DSN", "postgresql://infraguardian:localdev@localhost:5432/infraguardian"
)
TARGET_CONNECTIONS = 43


def _hold_connection(dsn: str, index: int, ready: threading.Event) -> None:
    """Open one connection, signal ready, then hold it until the main thread exits."""
    import psycopg2

    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        ready.set()
        # Hold the connection open indefinitely — the main thread's KeyboardInterrupt will
        # terminate the process and close all connections.
        while True:
            time.sleep(60)
    except Exception as exc:
        print(f"  connection {index}: {exc}", file=sys.stderr)
        ready.set()


def main() -> None:
    print(f"Opening {TARGET_CONNECTIONS} held connections to exhaust the pool...")
    print(f"DSN: {DSN}\n")

    threads: list[threading.Thread] = []
    for i in range(TARGET_CONNECTIONS):
        ready = threading.Event()
        t = threading.Thread(target=_hold_connection, args=(DSN, i, ready), daemon=True)
        t.start()
        ready.wait(timeout=5.0)
        threads.append(t)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{TARGET_CONNECTIONS} connections held")

    print(f"\nAll {TARGET_CONNECTIONS} connections held. Pool should be exhausted.")
    print("Press Ctrl-C to release all connections and exit.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nReleasing connections...")


if __name__ == "__main__":
    main()
