"""
Tests for the model-agnostic chat model factory (`tools/llm.py`).

Covers: zero-config default is the offline fake model and produces schema-valid output; an
invalid provider string surfaces a clear error rather than silently falling back; and
`invoke_structured`'s cost-tracking plumbing (fake model reports real $0, a known model prices
correctly, an unknown model string prices as free rather than raising).
"""

import json
import time
from typing import Any

import pytest

from agents.schemas import SpecialistFinding
from tools.llm import (
    CostBudgetExceededError,
    FakeInfraGuardianModel,
    LLMCallTimeoutError,
    StructuredOutputError,
    _price_usage,
    get_chat_model,
    get_cumulative_cost_usd,
    get_last_cost_usd,
    get_last_pricing_unknown,
    invoke_structured,
    start_cost_budget,
)


def test_get_chat_model_defaults_to_fake_model_when_env_unset(monkeypatch):
    monkeypatch.delenv("INFRAGUARDIAN_MODEL", raising=False)
    model = get_chat_model()
    assert isinstance(model, FakeInfraGuardianModel)


def test_fake_model_produces_schema_valid_specialist_finding():
    model = FakeInfraGuardianModel().with_structured_output(SpecialistFinding)
    result = model.invoke("Breached rules:\ncpu_pct=98.0 (max 90.0)")
    assert isinstance(result, SpecialistFinding)
    assert result.is_anomalous is True


def test_get_chat_model_with_invalid_provider_string_raises(monkeypatch):
    monkeypatch.setenv("INFRAGUARDIAN_MODEL", "not_a_real_provider:some-model")
    with pytest.raises(Exception):
        get_chat_model()


def test_fake_model_call_count_increments_on_invoke():
    fake = FakeInfraGuardianModel()
    model = fake.with_structured_output(SpecialistFinding)
    assert fake.call_count == 0
    model.invoke("cpu_pct=98.0 (max 90.0)")
    assert fake.call_count == 1


def test_invoke_structured_against_fake_model_returns_parsed_result_and_zero_cost():
    fake = FakeInfraGuardianModel()
    result = invoke_structured(fake, SpecialistFinding, "cpu_pct=98.0 (max 90.0)")
    assert isinstance(result, SpecialistFinding)
    assert get_last_cost_usd() == pytest.approx(0.0)


def test_price_usage_known_model_computes_expected_cost():
    # claude-sonnet-4-5-20250929: $3/M input, $15/M output.
    cost, pricing_unknown = _price_usage(
        "claude-sonnet-4-5-20250929", {"input_tokens": 1000, "output_tokens": 1000}
    )
    assert cost == pytest.approx((1000 * 3.0 + 1000 * 15.0) / 1_000_000)
    assert pricing_unknown is False


def test_price_usage_unknown_model_with_real_usage_reports_unknown_pricing():
    """A nonzero-usage call against an unlisted model must not silently report $0.00."""
    cost, pricing_unknown = _price_usage(
        "some-unlisted-model", {"input_tokens": 1000, "output_tokens": 1000}
    )
    assert cost is None
    assert pricing_unknown is True


def test_price_usage_zero_usage_is_real_free_not_unknown():
    """The fake model performs no real inference, so zero usage is a known, real $0.00."""
    cost, pricing_unknown = _price_usage(
        "some-unlisted-model", {"input_tokens": 0, "output_tokens": 0}
    )
    assert cost == 0.0
    assert pricing_unknown is False


def test_unparsed_provider_response_is_traced_and_raised(monkeypatch, tmp_path):
    """A provider's ``parsed=None`` response must fail closed and leave a reviewable artifact."""

    class UnparsedRunnable:
        def invoke(self, _prompt):
            return {
                "raw": {"content": "not valid structured output"},
                "parsed": None,
                "parsing_error": ValueError("tool arguments were invalid"),
            }

    class UnparsedModel:
        model = "test-model"

        def with_structured_output(self, _schema, include_raw=False):
            assert include_raw is True
            return UnparsedRunnable()

    monkeypatch.setenv("INFRAGUARDIAN_TRACE_DIR", str(tmp_path))

    with pytest.raises(StructuredOutputError, match="SpecialistFinding structured output was not parsed"):
        invoke_structured(UnparsedModel(), SpecialistFinding, "test prompt", trace_id="trace-123")

    event = json.loads((tmp_path / "events.jsonl").read_text().strip())
    assert event["trace_id"] == "trace-123"
    assert event["success"] is False
    assert event["parsed_response"] is None
    assert event["parsing_error"]["type"] == "ValueError"


# --- Timeout, retry/backoff, cost budget, pricing-gap fix (external review item #4) -----------


class _FlakyThenOkRunnable:
    """Raises on its first N calls, then succeeds — used to test retry/backoff."""

    def __init__(self, fail_times: int, result: Any):
        self.fail_times = fail_times
        self.result = result
        self.calls = 0

    def invoke(self, _prompt):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("simulated transient provider error")
        return self.result


class _FlakyModel:
    model = "test-model"

    def __init__(self, runnable):
        self._runnable = runnable

    def with_structured_output(self, _schema, include_raw=False):
        assert include_raw is True
        return self._runnable


def _finding_result():
    finding = SpecialistFinding(
        domain="cpu_mem",
        is_anomalous=True,
        root_cause_hypothesis="x",
        severity="medium",
        predictive_risk="x",
        evidence=["x"],
        automation_safety="safe",
        automation_safety_reason="x",
    )
    from langchain_core.messages import AIMessage

    raw = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})
    return {"raw": raw, "parsed": finding}


def test_invoke_structured_retries_transient_failure_and_succeeds(monkeypatch):
    monkeypatch.setattr("tools.llm._BACKOFF_BASE_SEC", 0.0)
    runnable = _FlakyThenOkRunnable(fail_times=1, result=_finding_result())
    result = invoke_structured(_FlakyModel(runnable), SpecialistFinding, "prompt")
    assert isinstance(result, SpecialistFinding)
    assert runnable.calls == 2


def test_invoke_structured_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("tools.llm._BACKOFF_BASE_SEC", 0.0)
    runnable = _FlakyThenOkRunnable(fail_times=99, result=_finding_result())
    with pytest.raises(ConnectionError):
        invoke_structured(_FlakyModel(runnable), SpecialistFinding, "prompt")
    assert runnable.calls == 3  # 1 initial + 2 retries, per _MAX_RETRIES


def test_invoke_structured_raises_timeout_error_on_slow_call(monkeypatch):
    monkeypatch.setattr("tools.llm._DEFAULT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("tools.llm._BACKOFF_BASE_SEC", 0.0)

    class _SlowRunnable:
        def invoke(self, _prompt):
            time.sleep(1.0)
            return _finding_result()

    with pytest.raises(LLMCallTimeoutError):
        invoke_structured(_FlakyModel(_SlowRunnable()), SpecialistFinding, "prompt")


def test_cost_budget_blocks_call_once_reached():
    fake = FakeInfraGuardianModel()
    # Fake model always reports real $0.00 (zero usage), so simulate having already spent by
    # setting the budget to 0 directly.
    start_cost_budget(max_cost_usd=0.0)
    with pytest.raises(CostBudgetExceededError):
        invoke_structured(fake, SpecialistFinding, "cpu_pct=98.0 (max 90.0)")


def test_cost_budget_none_disables_enforcement():
    fake = FakeInfraGuardianModel()
    start_cost_budget(max_cost_usd=None)
    result = invoke_structured(fake, SpecialistFinding, "cpu_pct=98.0 (max 90.0)")
    assert isinstance(result, SpecialistFinding)


def test_cost_budget_accumulates_across_calls(monkeypatch):
    monkeypatch.setattr(
        "tools.llm._PRICING_PER_MILLION_TOKENS", {"test-model": (1_000_000.0, 0.0)}
    )
    runnable = _FlakyThenOkRunnable(fail_times=0, result=_finding_result())
    start_cost_budget(max_cost_usd=100.0)
    invoke_structured(_FlakyModel(runnable), SpecialistFinding, "prompt")
    # 10 input tokens * $1,000,000/M = $10.00
    assert get_cumulative_cost_usd() == pytest.approx(10.0)


def test_unlisted_model_with_real_usage_reports_pricing_unknown_not_free():
    runnable = _FlakyThenOkRunnable(fail_times=0, result=_finding_result())
    start_cost_budget(max_cost_usd=None)
    invoke_structured(_FlakyModel(runnable), SpecialistFinding, "prompt")
    assert get_last_cost_usd() is None
    assert get_last_pricing_unknown() is True
