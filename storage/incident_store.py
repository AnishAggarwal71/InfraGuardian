"""
Persistent incident store (PRD §13 MVP scope, §15: "SQLite in MVP, Postgres post-MVP").

A thin, direct `sqlite3` wrapper — no ORM. One table (`incidents`) holds the full
`IncidentReport` as JSON plus indexed columns for simple querying; a second table
(`remediation_actions`) is the FR-16 audit log of every *executed* automated action, kept
separate from `incidents` because it has its own independent fields (rationale, confidence,
result) that could in principle exist without a fresh incident row (e.g. a future
human-approved action logged the same way).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from agents.schemas import IncidentReport
from tools.remediation import RunbookResult

DEFAULT_DB_PATH = "infraguardian.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    routing_decision TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remediation_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    runbook_id TEXT NOT NULL,
    rationale TEXT NOT NULL,
    confidence REAL NOT NULL,
    success INTEGER NOT NULL,
    message TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    UNIQUE (incident_id, runbook_id)
);
"""


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create the `incidents` and `remediation_actions` tables if they don't already exist."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def save_incident(report: IncidentReport, db_path: str = DEFAULT_DB_PATH) -> None:
    """Upsert the full `IncidentReport` (as JSON) plus its indexed columns."""
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO incidents (incident_id, timestamp, severity, routing_decision, report_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(incident_id) DO UPDATE SET
                timestamp=excluded.timestamp,
                severity=excluded.severity,
                routing_decision=excluded.routing_decision,
                report_json=excluded.report_json
            """,
            (
                report.incident_id,
                report.timestamp.isoformat(),
                report.severity.value,
                report.routing_decision.value,
                report.model_dump_json(),
            ),
        )
        conn.commit()


def get_incident(incident_id: str, db_path: str = DEFAULT_DB_PATH) -> IncidentReport | None:
    """Read path for M4's API/CLI and M5's eval harness. Returns None if not found."""
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT report_json FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
    if row is None:
        return None
    return IncidentReport.model_validate_json(row[0])


def was_already_executed(
    incident_id: str, runbook_id: str, db_path: str = DEFAULT_DB_PATH
) -> bool:
    """
    Idempotency check (PRD §9) consulted before a runbook's `execute()` is called — prevents
    double-executing the same incident_id+runbook_id pair across retries or duplicate requests.
    """
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM remediation_actions WHERE incident_id = ? AND runbook_id = ?",
            (incident_id, runbook_id),
        ).fetchone()
    return row is not None


def record_remediation_action(
    incident_id: str,
    runbook_id: str,
    rationale: str,
    confidence: float,
    result: RunbookResult,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Append the FR-16 audit row for one executed automated action."""
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO remediation_actions
                (incident_id, runbook_id, rationale, confidence, success, message, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                runbook_id,
                rationale,
                confidence,
                int(result.success),
                result.message,
                result.executed_at.isoformat(),
            ),
        )
        conn.commit()
