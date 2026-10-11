"""
Tests for the model-agnostic chat model factory (`tools/llm.py`).

Covers: zero-config default is the offline fake model and produces schema-valid output; an
invalid provider string surfaces a clear error rather than silently falling back; and
`invoke_structured`'s cost-tracking plumbing (fake model reports real $0, a known model prices
correctly, an unknown model string prices as free rather than raising).
"""

import json

import pytest

from agents.schemas import SpecialistFinding
from tools.llm import (
    FakeInfraGuardianModel,
    StructuredOutputError,
    _price_usage,
    get_chat_model,
    get_last_cost_usd,
    invoke_structured,
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
    cost = _price_usage(
        "claude-sonnet-4-5-20250929", {"input_tokens": 1000, "output_tokens": 1000}
    )
    assert cost == pytest.approx((1000 * 3.0 + 1000 * 15.0) / 1_000_000)


def test_price_usage_unknown_model_prices_as_free():
    cost = _price_usage("some-unlisted-model", {"input_tokens": 1000, "output_tokens": 1000})
    assert cost == 0.0


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
