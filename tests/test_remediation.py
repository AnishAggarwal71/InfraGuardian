"""Tests for the allowlisted remediation runbooks (PRD FR-15)."""

import pytest

from tools.metrics import Domain
from tools.remediation import RUNBOOKS, get_runbook_for_domain


@pytest.mark.parametrize("runbook_id", list(RUNBOOKS))
def test_runbook_executes_successfully(runbook_id):
    runbook = RUNBOOKS[runbook_id]
    result = runbook.execute("test-incident-id")
    assert result.success is True
    assert result.runbook_id == runbook_id
    assert "test-incident-id" in result.message


@pytest.mark.parametrize("domain", list(Domain))
def test_get_runbook_for_domain_returns_a_runbook_for_every_mvp_domain(domain):
    assert get_runbook_for_domain(domain) is not None
    assert get_runbook_for_domain(domain).domain == domain


def test_registry_has_at_least_three_runbooks():
    assert len(RUNBOOKS) >= 3
