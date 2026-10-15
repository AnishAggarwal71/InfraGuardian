"""
Deterministic runbook preconditions (`REAL_MODEL_VALIDATION.md` P1): separates "a specialist
diagnosed this finding as SAFE" from "automation is authorized to act on it".

Mirrors `tools/severity.py`'s bridge-module pattern: lives outside `tools/thresholds.py` (which
deliberately stays free of model/agent coupling, per its own module docstring) but is a pure
function over `DetectionResult`, so the gate it protects gains no new LLM dependency.

Motivated by the P1 finding in `evaluation/REAL_MODEL_VALIDATION.md`: the fake model's
`_RULE_INFO` table marks a lone connection-pool breach `SAFE` unconditionally, but a real model
(Claude Haiku) correctly pointed out that a pool recycle could drop in-flight work without more
evidence than a single sample provides. A specialist's `SAFE` verdict is a single-signal
judgment about the one rule it was asked to diagnose; it says nothing about whether *other*
rules in the same domain were breached at the same time. Requiring the triggering rule to be the
ONLY breached rule in its domain's detection is a concrete, checkable stand-in for "the evidence
is narrow enough to trust an automated response to it":

  - `db.recycle_connection_pool`: a lone `connection_pool_utilization_pct` breach is at least
    consistent with idle/leaked connections; a concurrent latency or error-rate breach suggests
    load genuinely acting on the DB, which a connection recycle will not fix and could worsen.
  - `cpu_mem.restart_worker_process`: a lone `cpu_pct` breach is consistent with one runaway
    process; concurrent memory/swap/load breaches suggest host-wide pressure a single process
    restart will not fix.
  - `gpu.clear_memory_cache`: a lone `gpu_mem_pct` breach is consistent with a leak or an
    oversized batch in one process; a concurrent ECC or thermal breach means a hardware fault may
    be in play, which clearing a cache cannot address and must not mask.

This is a judgment call, not a value derived from real incident data — the same honest framing
`tools/confidence.py` and `tools/severity.py` already use for their own thresholds.
"""

from __future__ import annotations

from pydantic import BaseModel

from tools.metrics import Domain
from tools.thresholds import DetectionResult

# The one breached-rule name each allowlisted runbook is authorized to respond to. A runbook id
# with no entry here fails closed — see `check_preconditions`.
_RUNBOOK_REQUIRED_RULE: dict[str, str] = {
    "db.recycle_connection_pool": "connection_pool_utilization_pct",
    "cpu_mem.restart_worker_process": "cpu_pct",
    "gpu.clear_memory_cache": "gpu_mem_pct",
}


class PreconditionResult(BaseModel):
    """Whether a runbook is authorized to execute for one domain's detection, and why."""

    met: bool
    reason: str


def check_preconditions(
    runbook_id: str,
    domain: Domain,
    detections: dict[Domain, DetectionResult],
) -> PreconditionResult:
    """
    Decide whether `runbook_id` is authorized to act on `domain`'s current detection.

    Fails closed (met=False) whenever the runbook has no registered precondition, the domain
    has no detection result, the runbook's required rule wasn't actually breached, or any other
    rule in the same domain was breached concurrently — only an isolated, single-rule breach of
    exactly the rule the runbook addresses is authorized.
    """
    required_rule = _RUNBOOK_REQUIRED_RULE.get(runbook_id)
    if required_rule is None:
        return PreconditionResult(
            met=False,
            reason=f"No precondition is registered for runbook '{runbook_id}' — fails closed.",
        )

    detection = detections.get(domain)
    if detection is None:
        return PreconditionResult(
            met=False,
            reason=f"No detection result is available for domain '{domain.value}' — fails closed.",
        )

    breached_names = [b.rule_name for b in detection.breached_rules]
    if required_rule not in breached_names:
        return PreconditionResult(
            met=False,
            reason=(
                f"Runbook '{runbook_id}' requires rule '{required_rule}' to be breached, but it "
                f"was not among the breached rules {breached_names} for domain "
                f"'{domain.value}'."
            ),
        )

    other_breaches = [name for name in breached_names if name != required_rule]
    if other_breaches:
        return PreconditionResult(
            met=False,
            reason=(
                f"Runbook '{runbook_id}' requires an isolated '{required_rule}' breach, but "
                f"domain '{domain.value}' also breached {other_breaches} at the same time — "
                "automation is not authorized without a human confirming the broader picture."
            ),
        )

    return PreconditionResult(
        met=True,
        reason=f"Isolated '{required_rule}' breach for domain '{domain.value}' — precondition satisfied.",
    )
