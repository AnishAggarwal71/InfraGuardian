"""
Tests for `PostgresCollector` (`tools/collectors.py`): monkeypatches `psycopg2.connect` to
return a fake cursor with canned query results, so the test is deterministic and doesn't
require a running Postgres instance — same pattern as `test_collectors.py` uses for psutil.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from tools.metrics import DBMetrics, Domain


class _FakeCursor:
    """Returns canned results for the queries PostgresCollector issues, in call order."""

    def __init__(self):
        self._results = iter(
            [
                (12,),           # connections_active (state != 'idle')
                (50,),           # connections_max
                (10000, 50),     # xact_commit, xact_rollback from pg_stat_database
                (4.25,),         # mean_exec_time from pg_stat_statements
            ]
        )

    def execute(self, query):
        pass

    def fetchone(self):
        return next(self._results)

    def close(self):
        pass


class _FakeConnection:
    closed = False

    def cursor(self):
        return _FakeCursor()


@pytest.fixture()
def _patch_psycopg2(monkeypatch):
    """Inject a fake psycopg2 module so PostgresCollector can import and use it."""
    fake_module = types.ModuleType("psycopg2")
    fake_module.connect = MagicMock(return_value=_FakeConnection())
    monkeypatch.setitem(sys.modules, "psycopg2", fake_module)


@pytest.mark.usefixtures("_patch_psycopg2")
def test_postgres_collector_returns_schema_valid_db_sample():
    from tools.collectors import PostgresCollector

    sample = PostgresCollector(dsn="postgresql://test:test@localhost/test").collect()

    assert isinstance(sample, DBMetrics)
    assert sample.domain == Domain.DB
    assert sample.source == "collector:postgres"
    assert sample.connections_active == 12
    assert sample.connections_max == 50
    assert sample.replication_lag_sec == 0.0
    assert sample.query_latency_p95_ms == 4.25
    assert 0 <= sample.error_rate_pct <= 100
    expected_error_rate = 50 / (10000 + 50) * 100
    assert abs(sample.error_rate_pct - expected_error_rate) < 0.01


@pytest.mark.usefixtures("_patch_psycopg2")
def test_postgres_collector_reuses_connection():
    from tools.collectors import PostgresCollector

    collector = PostgresCollector(dsn="postgresql://test:test@localhost/test")
    collector.collect()

    import psycopg2
    assert psycopg2.connect.call_count == 1

    # Second collect should reuse the existing connection, not create a new one.
    # Reset the fake cursor results for the second call.
    collector._conn = _FakeConnection()
    collector.collect()
    assert psycopg2.connect.call_count == 1


@pytest.mark.usefixtures("_patch_psycopg2")
def test_postgres_collector_reads_dsn_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://env:env@envhost/envdb")

    from tools.collectors import PostgresCollector

    collector = PostgresCollector()
    assert collector._dsn == "postgresql://env:env@envhost/envdb"
