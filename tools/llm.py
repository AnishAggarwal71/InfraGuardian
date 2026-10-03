"""
Model-agnostic chat model factory for InfraGuardian.

This is the ONLY module that is allowed to import a provider-specific LangChain package
(`langchain_anthropic`, `langchain_google_genai`, etc.) — and it does so lazily, inside
`init_chat_model()` from LangChain core, which dispatches on the `provider:model` string itself.
Agents and the orchestrator only ever call `get_chat_model()`; swapping Anthropic for Gemini or
an Ollama-hosted open-source model is purely an environment-variable change (`INFRAGUARDIAN_MODEL`),
never a code change. This is the concrete mechanism behind the user's "keep it model-agnostic,
might move to Gemini/open-source for cost" requirement.

When `INFRAGUARDIAN_MODEL` is unset, `get_chat_model()` returns `FakeInfraGuardianModel`: an
offline, zero-cost stand-in that returns coherent (not random) structured findings derived from
the breached-rule names present in the prompt, so the whole pipeline is runnable and demoable
before any API key is configured. It deliberately does not try to simulate real reasoning — it's
a lookup table, documented as such in DECISIONS.md.
"""

from __future__ import annotations

import os
import re
from typing import Any

from agents.schemas import (
    AutomationSafety,
    CrossDomainCorrelation,
    Severity,
    SpecialistFinding,
    SynthesisOutput,
)
from tools.metrics import Domain

_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

# Canned finding content per breached-rule name. These are judgment calls made to produce a
# coherent demo, not tuned-from-incident-data root causes — see DECISIONS.md M2 entry.
_RULE_INFO: dict[str, dict[str, Any]] = {
    "query_latency_p95_ms": {
        "domain": Domain.DB,
        "root_cause_hypothesis": "Query latency has climbed past normal bounds, consistent with a slow-query or missing-index pattern.",
        "severity": Severity.MEDIUM,
        "automation_safety": AutomationSafety.UNSAFE,
        "automation_safety_reason": "Killing or restarting queries without knowing which one is responsible risks dropping in-flight work.",
    },
    "connection_pool_utilization_pct": {
        "domain": Domain.DB,
        "root_cause_hypothesis": "Connection pool utilization is approaching exhaustion, consistent with a connection leak or a traffic spike outpacing pool size.",
        "severity": Severity.HIGH,
        "automation_safety": AutomationSafety.SAFE,
        "automation_safety_reason": "Recycling idle/leaked connections is a narrow, reversible action with no data-loss risk.",
    },
    "replication_lag_sec": {
        "domain": Domain.DB,
        "root_cause_hypothesis": "Replication lag is growing, consistent with replica resource contention or a burst of write volume on the primary.",
        "severity": Severity.HIGH,
        "automation_safety": AutomationSafety.UNSAFE,
        "automation_safety_reason": "Replication topology changes are high-blast-radius; needs a human to confirm before acting.",
    },
    "error_rate_pct": {
        "domain": Domain.DB,
        "root_cause_hypothesis": "Error rate is elevated, consistent with downstream dependency failures or query timeouts.",
        "severity": Severity.MEDIUM,
        "automation_safety": AutomationSafety.UNKNOWN,
        "automation_safety_reason": "The root cause of the errors is not yet distinguished from this signal alone.",
    },
    "cpu_pct": {
        "domain": Domain.CPU_MEM,
        "root_cause_hypothesis": "CPU utilization is saturated, consistent with a runaway process or an undersized instance for current load.",
        "severity": Severity.MEDIUM,
        "automation_safety": AutomationSafety.SAFE,
        "automation_safety_reason": "Restarting a specific runaway worker process is narrow and reversible.",
    },
    "mem_pct": {
        "domain": Domain.CPU_MEM,
        "root_cause_hypothesis": "Memory pressure is high, consistent with a slow leak or an oversized working set for available RAM.",
        "severity": Severity.MEDIUM,
        "automation_safety": AutomationSafety.UNKNOWN,
        "automation_safety_reason": "Whether this is a leak or legitimate load determines whether a restart is safe.",
    },
    "swap_pct": {
        "domain": Domain.CPU_MEM,
        "root_cause_hypothesis": "The system has started swapping, consistent with memory exhaustion severe enough to threaten overall host stability.",
        "severity": Severity.HIGH,
        "automation_safety": AutomationSafety.UNKNOWN,
        "automation_safety_reason": "Swapping this severe may indicate a host-wide issue beyond a single safe runbook.",
    },
    "load_avg_1m": {
        "domain": Domain.CPU_MEM,
        "root_cause_hypothesis": "Load average is spiking well past core count, consistent with a burst of queued work or a blocked resource backing up CPU-bound tasks.",
        "severity": Severity.HIGH,
        "automation_safety": AutomationSafety.UNKNOWN,
        "automation_safety_reason": "Load spikes can stem from many causes; needs more context before acting automatically.",
    },
    "gpu_temp_c": {
        "domain": Domain.GPU,
        "root_cause_hypothesis": "GPU temperature is approaching the thermal throttle point, consistent with inadequate cooling or sustained high utilization.",
        "severity": Severity.HIGH,
        "automation_safety": AutomationSafety.UNSAFE,
        "automation_safety_reason": "Thermal issues risk hardware damage; needs human judgment, not an automated action.",
    },
    "gpu_mem_pct": {
        "domain": Domain.GPU,
        "root_cause_hypothesis": "GPU memory utilization is approaching capacity, consistent with a memory leak across batches or a batch size too large for the device.",
        "severity": Severity.MEDIUM,
        "automation_safety": AutomationSafety.SAFE,
        "automation_safety_reason": "Clearing a bounded GPU memory cache for a specific process is narrow and reversible.",
    },
    "ecc_errors": {
        "domain": Domain.GPU,
        "root_cause_hypothesis": "ECC memory errors are present, consistent with a developing hardware fault on this device.",
        "severity": Severity.CRITICAL,
        "automation_safety": AutomationSafety.UNSAFE,
        "automation_safety_reason": "ECC errors indicate a possible hardware fault; critical-severity findings never auto-remediate (PRD safety gate).",
    },
}

_BREACHED_RULE_RE = re.compile(r"(\w+)=([\d.]+) \(max ([\d.]+)\)")
_SYNTHESIS_BLOCK_FIELD_RE = re.compile(r"^(\w+): (.*)$", re.MULTILINE)


def _stringify(prompt_input: Any) -> str:
    """Flatten whatever `with_structured_output(...).invoke(...)` was given into plain text."""
    if isinstance(prompt_input, str):
        return prompt_input
    if isinstance(prompt_input, list):
        return "\n".join(
            getattr(m, "content", str(m)) if not isinstance(m, str) else m for m in prompt_input
        )
    return str(prompt_input)


def _build_specialist_finding(text: str) -> SpecialistFinding:
    matches = _BREACHED_RULE_RE.findall(text)
    matched_rules = [name for name, _, _ in matches if name in _RULE_INFO]
    evidence = [f"{name}={value} (max {threshold})" for name, value, threshold in matches]

    if not matched_rules:
        return SpecialistFinding(
            domain=Domain.CPU_MEM,
            is_anomalous=True,
            root_cause_hypothesis="No specific breached rule could be identified from the prompt.",
            severity=Severity.MEDIUM,
            predictive_risk="Unknown — insufficient signal to project forward.",
            evidence=evidence,
            automation_safety=AutomationSafety.UNKNOWN,
            automation_safety_reason="Fake model could not match a known rule in the prompt text.",
        )

    primary = max(matched_rules, key=lambda name: _SEVERITY_RANK[_RULE_INFO[name]["severity"]])
    info = _RULE_INFO[primary]
    return SpecialistFinding(
        domain=info["domain"],
        is_anomalous=True,
        root_cause_hypothesis=info["root_cause_hypothesis"],
        severity=info["severity"],
        predictive_risk=f"If unaddressed, expect {info['domain'].value} degradation to continue worsening along its current trend.",
        evidence=evidence,
        automation_safety=info["automation_safety"],
        automation_safety_reason=info["automation_safety_reason"],
    )


def _build_synthesis_output(text: str) -> SynthesisOutput:
    blocks = text.split("---")
    parsed: list[dict[str, str]] = []
    for block in blocks:
        fields = dict(_SYNTHESIS_BLOCK_FIELD_RE.findall(block))
        if "domain" in fields:
            parsed.append(fields)

    if not parsed:
        return SynthesisOutput(
            unified_root_cause="No specialist findings were present to synthesize.",
            correlations=[],
            severity=Severity.LOW,
            confidence=0.5,
            predictive_risk="Unknown.",
            priority_ordered_actions=[],
            automation_safety_reason="No findings to assess.",
        )

    domains = []
    for f in parsed:
        try:
            domains.append(Domain(f["domain"]))
        except ValueError:
            continue

    _valid_severities = {s.value for s in Severity}
    severities = [Severity(f["severity"]) for f in parsed if f.get("severity") in _valid_severities]
    worst_severity = max(severities, key=lambda s: _SEVERITY_RANK[s]) if severities else Severity.MEDIUM

    correlations: list[CrossDomainCorrelation] = []
    unique_domains = sorted(set(domains), key=lambda d: d.value)
    if len(unique_domains) > 1:
        correlations.append(
            CrossDomainCorrelation(
                domains=unique_domains,
                relationship=(
                    "Multiple domains show concurrent anomalies within the same analysis "
                    "window, suggesting a shared or cascading underlying cause."
                ),
                is_causal_hypothesis=True,
                supporting_evidence=[f.get("evidence", "") for f in parsed if f.get("evidence")],
            )
        )

    return SynthesisOutput(
        unified_root_cause="; ".join(f["root_cause"] for f in parsed if "root_cause" in f),
        correlations=correlations,
        severity=worst_severity,
        # Single-domain findings get a high canned confidence so the fake model can exercise
        # the auto-remediate gate end-to-end in tests/demos without a real API key (PRD FR-12);
        # multi-domain findings stay below the threshold since M3 routing always sends
        # cross-domain incidents to RECOMMEND regardless of confidence (see tools/routing.py).
        confidence=0.6 if len(unique_domains) > 1 else 0.9,
        predictive_risk="; ".join(f.get("predictive_risk", "") for f in parsed if f.get("predictive_risk")),
        priority_ordered_actions=[
            f"Review {d.value} specialist finding and apply its recommended runbook" for d in unique_domains
        ],
        automation_safety_reason="; ".join(
            f.get("automation_safety_reason", "") for f in parsed if f.get("automation_safety_reason")
        ),
    )


class _FakeStructuredRunnable:
    """Returned by `FakeInfraGuardianModel.with_structured_output(schema)`."""

    def __init__(self, schema: type, model: "FakeInfraGuardianModel") -> None:
        self._schema = schema
        self._model = model

    def invoke(self, prompt_input: Any, config: Any = None, **kwargs: Any) -> Any:
        self._model.call_count += 1
        text = _stringify(prompt_input)
        if self._schema is SpecialistFinding:
            return _build_specialist_finding(text)
        if self._schema is SynthesisOutput:
            return _build_synthesis_output(text)
        raise NotImplementedError(
            f"FakeInfraGuardianModel has no canned behavior for schema {self._schema!r}"
        )

    async def ainvoke(self, prompt_input: Any, config: Any = None, **kwargs: Any) -> Any:
        return self.invoke(prompt_input, config, **kwargs)


class FakeInfraGuardianModel:
    """
    Offline, zero-cost default chat model used when `INFRAGUARDIAN_MODEL` is unset.

    `call_count` lets tests assert how many times the model was actually invoked — e.g. to
    confirm a healthy domain never triggers a specialist call (PRD FR-06).
    """

    def __init__(self) -> None:
        self.call_count = 0

    def with_structured_output(self, schema: type, **kwargs: Any) -> _FakeStructuredRunnable:
        return _FakeStructuredRunnable(schema, model=self)


def get_chat_model() -> Any:
    """
    Return the chat model InfraGuardian should use, selected by the `INFRAGUARDIAN_MODEL`
    environment variable (e.g. `anthropic:claude-sonnet-4-5-20250929`,
    `google_genai:gemini-2.0-flash`). Falls back to the offline `FakeInfraGuardianModel` when
    unset. Provider API keys (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, ...) are read natively by
    `init_chat_model`/the provider SDKs — not handled here.

    Deliberately does not catch errors from `init_chat_model`: a bad provider string or a
    missing API key must surface as a clear error, never silently fall back to the fake model.
    """
    model_string = os.environ.get("INFRAGUARDIAN_MODEL")
    if not model_string:
        return FakeInfraGuardianModel()

    from langchain.chat_models import init_chat_model

    return init_chat_model(model_string)
