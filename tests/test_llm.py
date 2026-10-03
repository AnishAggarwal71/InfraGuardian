"""
Tests for the model-agnostic chat model factory (`tools/llm.py`).

Covers: zero-config default is the offline fake model and produces schema-valid output; an
invalid provider string surfaces a clear error rather than silently falling back.
"""

import pytest

from agents.schemas import SpecialistFinding
from tools.llm import FakeInfraGuardianModel, get_chat_model


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
