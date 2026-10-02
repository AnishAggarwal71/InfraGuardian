"""
Tests for the rule pre-screen (PRD FR-05, FR-06).

The core assertion, run across every scenario: the final sample of a healthy scenario must
evaluate as not-anomalous, and the final sample of a degraded scenario must evaluate as
anomalous with at least one breached rule. This is the automated version of the M1 demo
criterion ("run a scenario, see a correct healthy/degraded classification").
"""

import pytest

from tools.scenarios import DEGRADED_SCENARIOS, HEALTHY_SCENARIOS, SCENARIOS
from tools.thresholds import evaluate


@pytest.mark.parametrize("name", HEALTHY_SCENARIOS)
def test_healthy_scenario_final_sample_is_not_anomalous(name):
    final_sample = SCENARIOS[name][-1]
    result = evaluate(final_sample)
    assert result.is_anomalous is False
    assert result.breached_rules == []


@pytest.mark.parametrize("name", DEGRADED_SCENARIOS)
def test_degraded_scenario_final_sample_is_anomalous(name):
    final_sample = SCENARIOS[name][-1]
    result = evaluate(final_sample)
    assert result.is_anomalous is True
    assert len(result.breached_rules) >= 1


@pytest.mark.parametrize("name", HEALTHY_SCENARIOS)
def test_healthy_scenario_first_sample_is_not_anomalous(name):
    first_sample = SCENARIOS[name][0]
    result = evaluate(first_sample)
    assert result.is_anomalous is False


@pytest.mark.parametrize("name", DEGRADED_SCENARIOS)
def test_degraded_scenario_starts_healthy(name):
    """Degraded scenarios should start from a healthy baseline, not already-anomalous."""
    first_sample = SCENARIOS[name][0]
    result = evaluate(first_sample)
    assert result.is_anomalous is False
