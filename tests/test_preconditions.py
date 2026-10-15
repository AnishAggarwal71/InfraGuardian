"""
Unit tests for `tools/preconditions.py` (`REAL_MODEL_VALIDATION.md` P1): deterministic runbook
preconditions, independent of routing/confidence/severity — see `tests/test_routing.py` for the
end-to-end gate tests that exercise this through `route()`.
"""

from datetime import datetime, timezone

import pytest

from tools.metrics import Domain
from tools.preconditions import check_preconditions
from tools.thresholds import BreachedRule, DetectionResult


def _detection(domain: Domain, rule_names: list[str]) -> DetectionResult:
    return DetectionResult(
        domain=domain,
        is_anomalous=len(rule_names) > 0,
        breached_rules=[
            BreachedRule(rule_name=name, observed_value=99.0, threshold=1.0) for name in rule_names
        ],
        evaluated_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "runbook_id,domain,rule_name",
    [
        ("db.recycle_connection_pool", Domain.DB, "connection_pool_utilization_pct"),
        ("cpu_mem.restart_worker_process", Domain.CPU_MEM, "cpu_pct"),
        ("gpu.clear_memory_cache", Domain.GPU, "gpu_mem_pct"),
    ],
)
def test_isolated_required_breach_is_met(runbook_id, domain, rule_name):
    detections = {domain: _detection(domain, [rule_name])}
    result = check_preconditions(runbook_id, domain, detections)
    assert result.met is True


def test_concurrent_breach_in_same_domain_is_not_met():
    detections = {
        Domain.DB: _detection(Domain.DB, ["connection_pool_utilization_pct", "error_rate_pct"])
    }
    result = check_preconditions("db.recycle_connection_pool", Domain.DB, detections)
    assert result.met is False


def test_required_rule_not_breached_is_not_met():
    detections = {Domain.DB: _detection(Domain.DB, ["replication_lag_sec"])}
    result = check_preconditions("db.recycle_connection_pool", Domain.DB, detections)
    assert result.met is False


def test_missing_detection_for_domain_is_not_met():
    result = check_preconditions("db.recycle_connection_pool", Domain.DB, {})
    assert result.met is False


def test_unregistered_runbook_is_not_met():
    detections = {Domain.DB: _detection(Domain.DB, ["connection_pool_utilization_pct"])}
    result = check_preconditions("db.some_future_runbook", Domain.DB, detections)
    assert result.met is False


def test_healthy_detection_with_no_breaches_is_not_met():
    detections = {Domain.DB: _detection(Domain.DB, [])}
    result = check_preconditions("db.recycle_connection_pool", Domain.DB, detections)
    assert result.met is False
