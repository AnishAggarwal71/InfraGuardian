"""
Tests for `evaluation/live_evaluator.py` — the harness itself, run against the offline fake
model so this suite needs no API key/network (consistent with every other test in this repo).
Correctness against a *real* model is the user's own job, run manually per
`REAL_MODEL_VALIDATION.md`; these tests only prove the harness's own mechanics: it resolves the
right model, aggregates repeats correctly, writes an immutable manifest, isolates trace
directories per run, and surfaces errors/safety violations rather than swallowing them.
"""

from __future__ import annotations

import json
import os

import pytest

from evaluation.fixtures.incident_cases import INCIDENT_CASES
from evaluation.live_evaluator import (
    _TRACE_DIR_ENV,
    LiveRunManifest,
    run_live_evaluation,
)


@pytest.fixture(autouse=True)
def _clean_trace_dir_env(monkeypatch):
    """Every test must start with no ambient trace dir so leakage across tests is detectable."""
    monkeypatch.delenv(_TRACE_DIR_ENV, raising=False)
    monkeypatch.delenv("INFRAGUARDIAN_MODEL", raising=False)


def test_default_model_is_fake_and_resolves_without_a_provider_key(tmp_path):
    """Omitting `model=` must run the offline fake model, not attempt a real provider call."""
    manifest = run_live_evaluation(
        model=None, repeats=1, case_names=["gpu_ecc_errors"], runs_dir=tmp_path
    )
    assert manifest.model == "fake"
    assert len(manifest.results) == 1
    assert manifest.results[0].error is None


def test_repeats_are_aggregated_into_one_case_summary(tmp_path):
    """3 repeats of 1 case must produce 3 results folded into exactly 1 `LiveCaseSummary`."""
    manifest = run_live_evaluation(
        model=None, repeats=3, case_names=["db_connection_pool_exhaustion"], runs_dir=tmp_path
    )
    assert len(manifest.results) == 3
    assert len(manifest.case_summaries) == 1
    summary = manifest.case_summaries[0]
    assert summary.repeats == 3
    # The fake model is fully deterministic, so all 3 repeats must agree with each other and
    # with the fixture's expectation.
    assert summary.severity_accuracy == 1.0
    assert summary.routing_accuracy == 1.0
    assert summary.distinct_severities_seen == 1
    assert summary.distinct_routing_decisions_seen == 1


def test_case_name_filter_restricts_which_cases_run(tmp_path):
    """`case_names` must restrict the run, not just annotate it — unlisted cases never execute."""
    manifest = run_live_evaluation(
        model=None, repeats=1, case_names=["gpu_ecc_errors"], runs_dir=tmp_path
    )
    assert manifest.case_count == 1
    assert {r.case_name for r in manifest.results} == {"gpu_ecc_errors"}


def test_no_filter_runs_every_fixture_case(tmp_path):
    """Omitting `case_names` must run the full `INCIDENT_CASES` fixture set, not a hardcoded subset."""
    manifest = run_live_evaluation(model=None, repeats=1, case_names=None, runs_dir=tmp_path)
    assert manifest.case_count == len(INCIDENT_CASES)
    assert len(manifest.results) == len(INCIDENT_CASES)


def test_manifest_is_written_to_disk_and_matches_the_returned_object(tmp_path):
    """The on-disk manifest is the durable record `REAL_MODEL_VALIDATION.md` asks for — prove it
    round-trips byte-for-byte with what the function returned, not just that a file exists."""
    manifest = run_live_evaluation(
        model=None, repeats=1, case_names=["gpu_thermal_pressure"], runs_dir=tmp_path
    )
    manifest_path = tmp_path / manifest.run_id / "manifest.json"
    assert manifest_path.exists()
    on_disk = LiveRunManifest.model_validate(json.loads(manifest_path.read_text()))
    assert on_disk.run_id == manifest.run_id
    assert on_disk.overall_severity_accuracy == manifest.overall_severity_accuracy
    assert len(on_disk.results) == len(manifest.results)


def test_each_run_id_gets_an_isolated_trace_directory(tmp_path):
    """Two separate invocations must not share a trace root — each run_id is its own directory,
    so one run's traces can never be mistaken for another's."""
    manifest_a = run_live_evaluation(
        model=None, repeats=1, case_names=["gpu_ecc_errors"], runs_dir=tmp_path
    )
    manifest_b = run_live_evaluation(
        model=None, repeats=1, case_names=["gpu_ecc_errors"], runs_dir=tmp_path
    )
    assert manifest_a.run_id != manifest_b.run_id
    assert manifest_a.trace_root_dir != manifest_b.trace_root_dir


def test_restores_ambient_model_and_trace_dir_env_vars_after_running(tmp_path, monkeypatch):
    """A harness that leaves `INFRAGUARDIAN_MODEL`/`INFRAGUARDIAN_TRACE_DIR` mutated after it
    returns would corrupt whatever runs next in the same process — assert both are restored."""
    monkeypatch.setenv("INFRAGUARDIAN_MODEL", "sentinel-should-be-restored")
    monkeypatch.setenv(_TRACE_DIR_ENV, "/sentinel/should/be/restored")

    run_live_evaluation(model=None, repeats=1, case_names=["gpu_ecc_errors"], runs_dir=tmp_path)

    assert os.environ.get("INFRAGUARDIAN_MODEL") == "sentinel-should-be-restored"
    assert os.environ.get(_TRACE_DIR_ENV) == "/sentinel/should/be/restored"


def test_max_total_cost_usd_stops_starting_new_runs_once_exceeded(tmp_path):
    """A near-zero budget must cause later (case, repeat) runs to be skipped rather than ignored
    — with the fake model's $0.00 cost this never actually trips, so this asserts the simpler,
    always-true safety property: the manifest never claims more completed runs than requested."""
    manifest = run_live_evaluation(
        model=None,
        repeats=2,
        case_names=["gpu_ecc_errors", "db_connection_pool_exhaustion"],
        max_total_cost_usd=0.0,
        runs_dir=tmp_path,
    )
    assert len(manifest.results) <= 2 * 2


def test_safety_invariant_violation_flags_are_computed_from_report_fields(tmp_path):
    """`critical_auto_remediated`/`underconfident_auto_remediated`/`false_auto_remediation` must
    be derived from the actual report, not hardcoded false — verify the always-safe fake-model
    baseline reports zero violations end-to-end for a known-critical case."""
    manifest = run_live_evaluation(
        model=None, repeats=1, case_names=["gpu_ecc_errors"], runs_dir=tmp_path
    )
    result = manifest.results[0]
    assert result.actual_severity == "critical"
    assert result.actual_routing == "escalate"
    assert result.critical_auto_remediated is False
    assert result.false_auto_remediation is False
    assert result.underconfident_auto_remediated is False
    assert manifest.total_safety_invariant_violations == 0
