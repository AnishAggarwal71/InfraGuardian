"""
Tests for the per-domain specialist agents (PRD FR-07): schema-valid structured findings for
an anomalous sample in each domain, using the offline fake model (no network/API key needed).
"""

import pytest

from agents import cpu_mem_agent, db_agent, gpu_agent
from agents.schemas import SpecialistFinding
from tools.scenarios import SCENARIOS
from tools.thresholds import evaluate

CASES = [
    ("db_connection_pool_exhaustion", db_agent),
    ("cpu_mem_cpu_saturation", cpu_mem_agent),
    ("gpu_thermal_pressure", gpu_agent),
]


@pytest.mark.parametrize("scenario_name,agent_module", CASES)
def test_specialist_diagnose_returns_schema_valid_anomalous_finding(scenario_name, agent_module):
    sample = SCENARIOS[scenario_name][-1]
    detection = evaluate(sample)
    assert detection.is_anomalous  # sanity check on the fixture itself

    finding = agent_module.diagnose(sample, detection)

    assert isinstance(finding, SpecialistFinding)
    assert finding.is_anomalous is True
    assert finding.root_cause_hypothesis
    assert finding.evidence
