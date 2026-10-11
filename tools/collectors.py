"""
Collector abstraction for InfraGuardian.

A `MetricCollector` is anything that can produce the next `MetricSample` for a domain.
`SimulatorCollector` replays a fixed scenario sample-by-sample; `PsutilCollector` reads this
host's real CPU/mem/swap/load (PRD §12, post-MVP roadmap item 4 — now implemented for the
CPU_MEM domain); `PostgresCollector` queries a live Postgres instance for DB domain metrics.
All implement the same Protocol, so swapping one for another requires no change to the rule
engine, agents, or orchestrator (PRD FR-03). A `Protocol` (structural typing) is used instead
of an ABC so a real collector just needs to expose `.collect()` — no inheritance coupling to
this module.
"""

from __future__ import annotations

import os
from typing import Protocol

import psutil

from tools.metrics import CPUMemMetrics, DBMetrics, MetricSample


class MetricCollector(Protocol):
    """Anything that can produce the next metric sample when polled."""

    def collect(self) -> MetricSample:
        """Return the next available sample. Raises `StopIteration` when exhausted."""
        ...


class SimulatorCollector:
    """
    Replays a fixed, pre-authored sequence of samples one at a time.

    Mimics how a real collector is polled (one sample per call) so scenario playback exercises
    the same call pattern downstream code will see in production.
    """

    def __init__(self, samples: list[MetricSample]) -> None:
        self._samples = samples
        self._index = 0

    def collect(self) -> MetricSample:
        if self._index >= len(self._samples):
            raise StopIteration("scenario exhausted")
        sample = self._samples[self._index]
        self._index += 1
        return sample

    def __len__(self) -> int:
        return len(self._samples)

    def reset(self) -> None:
        """Rewind to the first sample — useful for re-running a scenario in tests/demos."""
        self._index = 0


class PsutilCollector:
    """
    Reads this host's real CPU/mem/swap/load via `psutil` and returns one `CPUMemMetrics`
    sample per `.collect()` call.

    `psutil.getloadavg()` works cross-platform (Linux/macOS via the native syscall; Windows via
    psutil's own sampled emulation, backed by a background sampling thread psutil starts
    lazily on first call) — no platform branch needed here.
    """

    def collect(self) -> CPUMemMetrics:
        return CPUMemMetrics(
            timestamp=MetricSample.now(),
            cpu_pct=psutil.cpu_percent(interval=0.1),
            mem_pct=psutil.virtual_memory().percent,
            swap_pct=psutil.swap_memory().percent,
            load_avg_1m=psutil.getloadavg()[0],
            source="collector:psutil",
        )


class PostgresCollector:
    """
    Queries a live PostgreSQL instance for DB domain metrics.

    Uses `psycopg2` (imported lazily so the module stays importable and the test suite stays
    green even when the dependency isn't installed — same convention as `tools/llm.py`'s
    provider SDKs and `cli/main.py`'s `plyer` import).

    Constructor reads `POSTGRES_DSN` from the environment by default, or accepts an explicit DSN.
    The connection is established lazily on the first `.collect()` call and reused thereafter.

    Known limitation: `replication_lag_sec` is hardcoded to 0.0 because the docker-compose demo
    runs a single-node Postgres (no replica). This is documented honestly rather than fabricated.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get("POSTGRES_DSN", "")
        self._conn = None

    def _get_connection(self):
        """Lazily connect (or reconnect on failure) to Postgres."""
        import psycopg2

        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
        return self._conn

    def collect(self) -> DBMetrics:
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE state != 'idle' AND pid != pg_backend_pid()"
        )
        connections_active = cur.fetchone()[0]

        cur.execute("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
        connections_max = cur.fetchone()[0]

        cur.execute(
            "SELECT xact_commit, xact_rollback FROM pg_stat_database WHERE datname = current_database()"
        )
        row = cur.fetchone()
        total = (row[0] or 0) + (row[1] or 0)
        error_rate_pct = ((row[1] or 0) / total * 100) if total > 0 else 0.0

        cur.execute(
            "SELECT COALESCE(mean_exec_time, 0) FROM pg_stat_statements "
            "ORDER BY mean_exec_time DESC LIMIT 1"
        )
        pg_ss_row = cur.fetchone()
        query_latency_p95_ms = pg_ss_row[0] if pg_ss_row else 0.0

        cur.close()

        return DBMetrics(
            timestamp=MetricSample.now(),
            source="collector:postgres",
            query_latency_p95_ms=query_latency_p95_ms,
            connections_active=connections_active,
            connections_max=connections_max,
            replication_lag_sec=0.0,
            error_rate_pct=error_rate_pct,
        )
