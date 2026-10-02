"""Tests for the scenario catalog (PRD FR-02): every domain has required coverage, samples are well-formed."""

from tools.metrics import Domain
from tools.scenarios import DEGRADED_SCENARIOS, HEALTHY_SCENARIOS, SCENARIOS


def test_every_domain_has_at_least_one_healthy_scenario():
    for domain in Domain:
        matching = [name for name in HEALTHY_SCENARIOS if name.startswith(domain.value)]
        assert len(matching) >= 1, f"{domain} has no healthy scenario"


def test_every_domain_has_at_least_three_degraded_scenarios():
    for domain in Domain:
        matching = [name for name in DEGRADED_SCENARIOS if name.startswith(domain.value)]
        assert len(matching) >= 3, f"{domain} has only {len(matching)} degraded scenarios"


def test_scenario_samples_are_chronologically_ordered():
    for name, samples in SCENARIOS.items():
        timestamps = [s.timestamp for s in samples]
        assert timestamps == sorted(timestamps), f"{name} samples are not in order"


def test_scenario_samples_share_a_single_domain():
    for name, samples in SCENARIOS.items():
        domains = {s.domain for s in samples}
        assert len(domains) == 1, f"{name} mixes domains: {domains}"


def test_scenario_names_match_declared_domain():
    for name, samples in SCENARIOS.items():
        domain = samples[0].domain
        assert name.startswith(domain.value), f"{name} does not match domain {domain}"
