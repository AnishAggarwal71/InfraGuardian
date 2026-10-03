"""
Allowlisted remediation runbooks (PRD FR-15).

`RUNBOOKS` is a static registry — the only mechanism by which an incident can trigger an
executable action. The LLM/routing layer only ever selects a runbook *id* from this dict; it
can never construct or supply arbitrary executable text. Execution in this MVP is simulated
(no real DB/host/GPU to act on in a portfolio project) — each runbook logs what it would do and
returns a canned success result. That tradeoff is intentional and stated here rather than
glossed over: the mechanism being demonstrated is the safety gate and audit trail, not real
infrastructure remediation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel

from tools.metrics import Domain


class RunbookResult(BaseModel):
    """Outcome of executing one runbook once, for one incident (PRD FR-16 audit fields)."""

    runbook_id: str
    success: bool
    message: str
    executed_at: datetime


@dataclass(frozen=True)
class Runbook:
    """
    One allowlisted remediation action.

    `execute` never crosses an LLM structured-output boundary, so a plain dataclass (rather than
    a Pydantic model) is sufficient here — it only ever holds a Python callable the orchestrator
    invokes directly.
    """

    id: str
    domain: Domain
    description: str
    execute: Callable[[str], RunbookResult]


def _recycle_db_connection_pool(idempotency_key: str) -> RunbookResult:
    """Simulate recycling idle/leaked DB connections — narrow, reversible, no data-loss risk."""
    return RunbookResult(
        runbook_id="db.recycle_connection_pool",
        success=True,
        message=(
            f"Simulated: recycled idle/leaked connections in the DB pool for incident "
            f"{idempotency_key}."
        ),
        executed_at=datetime.now(timezone.utc),
    )


def _restart_cpu_mem_worker_process(idempotency_key: str) -> RunbookResult:
    """Simulate restarting the specific runaway/stuck worker process identified by the finding."""
    return RunbookResult(
        runbook_id="cpu_mem.restart_worker_process",
        success=True,
        message=(
            f"Simulated: restarted the flagged worker process for incident {idempotency_key}."
        ),
        executed_at=datetime.now(timezone.utc),
    )


def _clear_gpu_memory_cache(idempotency_key: str) -> RunbookResult:
    """Simulate clearing a bounded GPU memory cache for the flagged process."""
    return RunbookResult(
        runbook_id="gpu.clear_memory_cache",
        success=True,
        message=f"Simulated: cleared GPU memory cache for incident {idempotency_key}.",
        executed_at=datetime.now(timezone.utc),
    )


RUNBOOKS: dict[str, Runbook] = {
    "db.recycle_connection_pool": Runbook(
        id="db.recycle_connection_pool",
        domain=Domain.DB,
        description="Recycle idle/leaked connections in the DB connection pool.",
        execute=_recycle_db_connection_pool,
    ),
    "cpu_mem.restart_worker_process": Runbook(
        id="cpu_mem.restart_worker_process",
        domain=Domain.CPU_MEM,
        description="Restart the specific stuck/runaway worker process.",
        execute=_restart_cpu_mem_worker_process,
    ),
    "gpu.clear_memory_cache": Runbook(
        id="gpu.clear_memory_cache",
        domain=Domain.GPU,
        description="Clear a bounded GPU memory cache for the flagged process.",
        execute=_clear_gpu_memory_cache,
    ),
}

_RUNBOOK_BY_DOMAIN: dict[Domain, Runbook] = {rb.domain: rb for rb in RUNBOOKS.values()}


def get_runbook_for_domain(domain: Domain) -> Runbook | None:
    """Return the one allowlisted runbook for `domain`, or None if no runbook exists for it."""
    return _RUNBOOK_BY_DOMAIN.get(domain)
