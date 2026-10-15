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

`invoke_structured()` is the one call site every agent/orchestrator LLM call goes through. It
requests the raw provider message alongside the parsed structured output (`include_raw=True`)
so real token usage can be read from `raw.usage_metadata`, prices it against
`_PRICING_PER_MILLION_TOKENS`, and stashes the result in a thread-local — readable via
`get_last_cost_usd()` immediately after, on the same thread. A thread-local (not a changed
return type) is deliberate: `diagnose()` on every specialist agent returns a bare
`SpecialistFinding` that three test files and the orchestrator pattern-match on
(`isinstance(finding, SpecialistFinding)`); threading a `(finding, cost)` tuple through that
boundary for an internal telemetry detail isn't worth breaking it. This is safe because each
specialist/synthesis LLM call runs to completion within a single LangGraph node's own thread
before any other node can read `get_last_cost_usd()` — the same concurrency assumption
`tools.observability._MetricsRecorder`'s lock already documents.

The same call is wrapped in a timeout and bounded exponential-backoff retry
(`_call_provider_with_retries`) — a transient provider hiccup previously went straight to the
orchestrator's fail-closed sentinel; it now gets `_MAX_RETRIES` chances first. A per-run cost
budget (`start_cost_budget()`/`get_cumulative_cost_usd()`) is opt-in: the orchestrator calls
`start_cost_budget()` once per `run_incident()`, and `invoke_structured()` refuses to make
another call once the run's cumulative known cost reaches the limit, raising
`CostBudgetExceededError` — which the orchestrator's existing broad exception handling around
each specialist/synthesis call already treats as a fail-closed signal, same as any other LLM
failure (PRD §9). The budget is shared across every call in one run via a `ContextVar`
(`_CostBudget`), not a thread-local, because LangGraph's parallel specialist branches (PRD FR-08)
run on separate worker threads a thread-local tracker would never see. An unlisted model's real
(nonzero) token usage now reports `cost_usd=None` plus `pricing_unknown=True` instead of a
silent `$0.00` (`REAL_MODEL_VALIDATION.md` P1) — zero usage (the fake model) still reports a
real, known `$0.00`.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import os
import re
import threading
import time
from typing import Any

from langchain_core.messages import AIMessage

from agents.schemas import (
    AutomationSafety,
    CrossDomainCorrelation,
    Severity,
    SpecialistFinding,
    SynthesisOutput,
)
from tools.metrics import Domain
from tools.tracing import record_trace_event

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

    def __init__(self, schema: type, model: "FakeInfraGuardianModel", include_raw: bool = False) -> None:
        self._schema = schema
        self._model = model
        self._include_raw = include_raw

    def _parse(self, text: str) -> Any:
        if self._schema is SpecialistFinding:
            return _build_specialist_finding(text)
        if self._schema is SynthesisOutput:
            return _build_synthesis_output(text)
        raise NotImplementedError(
            f"FakeInfraGuardianModel has no canned behavior for schema {self._schema!r}"
        )

    def invoke(self, prompt_input: Any, config: Any = None, **kwargs: Any) -> Any:
        self._model.call_count += 1
        text = _stringify(prompt_input)
        parsed = self._parse(text)
        if not self._include_raw:
            return parsed
        # Zero usage, zero cost — honest, not a made-up number: the fake model does no real
        # inference, so there are no real tokens to report.
        raw = AIMessage(content="", usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        return {"raw": raw, "parsed": parsed}

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

    def with_structured_output(
        self, schema: type, include_raw: bool = False, **kwargs: Any
    ) -> _FakeStructuredRunnable:
        return _FakeStructuredRunnable(schema, model=self, include_raw=include_raw)


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


# USD per 1M tokens, (input, output). Covers the models this project has actually been run
# against — not an exhaustive provider price list. An unlisted model string with real token
# usage prices as unknown (`cost_usd=None`, `pricing_unknown=True`, see `_price_usage` below),
# never as a silent `$0.00` — cost tracking still never blocks a real LLM call, it just reports
# the gap honestly instead of masking it.
_PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-opus-4-5-20251101": (5.0, 25.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

_cost_local = threading.local()


class _CostBudget:
    """
    Shared, lock-protected per-run cost accumulator.

    `tools.observability._MetricsRecorder` already documents why specialist nodes run as
    concurrent LangGraph branches on separate worker threads (PRD FR-08) — a `threading.local()`
    budget tracker would silently never see any specialist's spend from `run_incident()`'s own
    thread, so a configured budget would never actually bind. LangGraph propagates the calling
    thread's `contextvars.Context` into each node's worker thread (confirmed: a `ContextVar` set
    before `run_incident()` is visible inside every specialist call), so a single shared object
    referenced via a `ContextVar` — not a thread-local — is what makes the budget apply across
    every call in one run, regardless of which thread makes it.
    """

    def __init__(self, max_cost_usd: float | None) -> None:
        self.max_cost_usd = max_cost_usd
        self._spent_usd = 0.0
        self._lock = threading.Lock()

    def add(self, cost_usd: float) -> None:
        with self._lock:
            self._spent_usd += cost_usd

    def spent(self) -> float:
        with self._lock:
            return self._spent_usd


_cost_budget_var: contextvars.ContextVar["_CostBudget | None"] = contextvars.ContextVar(
    "infraguardian_cost_budget", default=None
)

# Timeout/retry/backoff constants for real provider calls (`REAL_MODEL_VALIDATION.md` P1 /
# external-review gap #1: "no timeouts, retries, or backoff"). Deliberately plain module
# constants rather than env-driven — tests monkeypatch them directly, same pattern as every
# other tuned constant in this project (see `tools/thresholds.py`).
_DEFAULT_TIMEOUT_SEC = 20.0
_MAX_RETRIES = 2
_BACKOFF_BASE_SEC = 0.5

_MAX_COST_USD_PER_RUN_ENV = "INFRAGUARDIAN_MAX_COST_USD_PER_RUN"


class StructuredOutputError(RuntimeError):
    """Raised when a provider response cannot be converted to the requested output schema."""


class LLMCallTimeoutError(RuntimeError):
    """Raised when a provider call exceeds `_DEFAULT_TIMEOUT_SEC`, after retries are exhausted."""


class CostBudgetExceededError(RuntimeError):
    """
    Raised when a run's cumulative LLM cost has already reached its configured per-run budget,
    before making another call. Caught by the orchestrator's existing broad `except Exception`
    around each specialist/synthesis call, so a budget breach fails closed to the same
    sentinel-finding/ESCALATE path as any other LLM failure (PRD §9) rather than needing its own
    special-cased handling.
    """


def _price_usage(model_name: str, usage: dict[str, int]) -> tuple[float | None, bool]:
    """
    Price one call's token usage against `_PRICING_PER_MILLION_TOKENS`.

    Returns `(cost_usd, pricing_unknown)`. Zero usage (the fake model, which performs no real
    inference) always prices as a real, known `$0.00` — not an unknown-pricing gap. Nonzero
    usage against a model string absent from the pricing table returns `(None, True)` rather
    than silently reporting `$0.00`, per the P1 fix in `REAL_MODEL_VALIDATION.md`: a `$0.00`
    that just means "we don't have this model's price" must not look identical to a genuinely
    free call.
    """
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    if input_tokens == 0 and output_tokens == 0:
        return 0.0, False
    pricing = _PRICING_PER_MILLION_TOKENS.get(model_name)
    if pricing is None:
        return None, True
    input_price, output_price = pricing
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000, False


def get_last_cost_usd() -> float | None:
    """
    Return the USD cost computed by the most recent `invoke_structured()` call on this thread,
    or `None` if none has run yet on this thread, or if that call's model string has no entry in
    `_PRICING_PER_MILLION_TOKENS` (see `get_last_pricing_unknown()` to distinguish the two). Must
    be read immediately after the matching `invoke_structured()` call — see module docstring for
    the thread-local safety rationale.
    """
    return getattr(_cost_local, "last_cost_usd", None)


def get_last_pricing_unknown() -> bool:
    """
    Return whether the most recent `invoke_structured()` call on this thread used real token
    usage priced against a model string absent from `_PRICING_PER_MILLION_TOKENS`. `True` means
    `get_last_cost_usd()` is `None` because the price is genuinely unknown, not because the call
    was actually free.
    """
    return getattr(_cost_local, "last_pricing_unknown", False)


def start_cost_budget(max_cost_usd: float | None) -> None:
    """
    Start a new cumulative-cost tracker for one `run_incident()` call.

    `max_cost_usd=None` disables budget enforcement entirely (the default) — every call still
    accumulates `get_cumulative_cost_usd()`, it's just never compared against a limit. Must be
    called from the thread that then calls `run_incident()`/the orchestrator entrypoint — the
    tracker is propagated to specialist/synthesis calls via a `ContextVar`, which LangGraph
    carries into each node's worker thread automatically (see `_CostBudget`'s docstring).
    """
    _cost_budget_var.set(_CostBudget(max_cost_usd))


def get_cumulative_cost_usd() -> float:
    """Return the current run's cumulative known LLM cost since the last `start_cost_budget()`."""
    budget = _cost_budget_var.get()
    return budget.spent() if budget is not None else 0.0


def _call_provider_with_retries(
    model: Any, schema: type, prompt: str, trace_id: str | None, model_name: str
) -> Any:
    """
    Call `model.with_structured_output(schema, include_raw=True).invoke(prompt)` under a timeout,
    retrying transient failures (timeouts, network/provider errors) up to `_MAX_RETRIES` times
    with exponential backoff before giving up.

    Deliberately does NOT retry on a successful call that returns `parsed=None`/a parsing error
    — that's a deterministic schema-validity failure the provider's own response caused, not a
    transient fault a retry would fix, and `invoke_structured` already treats it as fail-closed
    on its own. Only exceptions raised by the call itself (including this function's own
    `LLMCallTimeoutError`) are retried.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            runnable = model.with_structured_output(schema, include_raw=True)
            future = executor.submit(runnable.invoke, prompt)
            try:
                return future.result(timeout=_DEFAULT_TIMEOUT_SEC)
            except concurrent.futures.TimeoutError as exc:
                raise LLMCallTimeoutError(
                    f"{schema.__name__} call exceeded {_DEFAULT_TIMEOUT_SEC}s timeout "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES + 1})."
                ) from exc
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any call failure may be transient
            last_exc = exc
            if attempt < _MAX_RETRIES:
                record_trace_event(
                    trace_id,
                    "llm_call_retry",
                    attempt=attempt + 1,
                    max_retries=_MAX_RETRIES,
                    model_name=model_name,
                    schema=schema.__name__,
                    error=exc,
                )
                time.sleep(_BACKOFF_BASE_SEC * (2**attempt))
                continue
            raise
        finally:
            # Don't block here waiting for a timed-out call's background thread to finish —
            # that would defeat the point of the timeout. The thread is left to finish on its
            # own; its result is simply never read.
            executor.shutdown(wait=False)
    raise last_exc  # pragma: no cover - loop always returns or raises above


def invoke_structured(model: Any, schema: type, prompt: str, trace_id: str | None = None) -> Any:
    """
    The one call site every specialist/synthesis LLM call goes through: requests `schema`-typed
    structured output from `model`, retrying transient failures with backoff under a timeout
    (`_call_provider_with_retries`), prices the real token usage (when the provider attaches
    `usage_metadata` to the raw message) via `_PRICING_PER_MILLION_TOKENS`, and stores the
    computed cost for `get_last_cost_usd()`/`get_last_pricing_unknown()` to read right after.
    Returns just the parsed structured object — callers are unaffected by the `include_raw`
    plumbing.

    Raises `CostBudgetExceededError` *before* calling the provider if the current run's
    cumulative cost (set up via `start_cost_budget()`, shared across threads via a `ContextVar`)
    has already reached its configured limit — a budget check gates the next call, it doesn't
    abort one already in flight.
    """
    model_name = getattr(model, "model", None) or getattr(model, "model_name", None) or ""
    start = time.perf_counter()
    _cost_local.last_cost_usd = None
    _cost_local.last_pricing_unknown = False

    budget = _cost_budget_var.get()
    if budget is not None and budget.max_cost_usd is not None and budget.spent() >= budget.max_cost_usd:
        error = CostBudgetExceededError(
            f"Per-run LLM cost budget (${budget.max_cost_usd:.4f}) already reached "
            f"(${budget.spent():.4f} spent) — refusing to make another {schema.__name__} call."
        )
        record_trace_event(
            trace_id,
            "llm_call",
            success=False,
            model_name=model_name,
            schema=schema.__name__,
            prompt=prompt,
            latency_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )
        raise error

    try:
        result = _call_provider_with_retries(model, schema, prompt, trace_id, model_name)
    except Exception as exc:
        record_trace_event(
            trace_id,
            "llm_call",
            success=False,
            model_name=model_name,
            schema=schema.__name__,
            prompt=prompt,
            latency_ms=(time.perf_counter() - start) * 1000,
            error=exc,
        )
        raise

    raw = result.get("raw") if isinstance(result, dict) else None
    parsed = result.get("parsed") if isinstance(result, dict) else result
    parsing_error = result.get("parsing_error") if isinstance(result, dict) else None
    usage = getattr(raw, "usage_metadata", None) or {}
    cost_usd, pricing_unknown = _price_usage(model_name, usage)
    _cost_local.last_cost_usd = cost_usd
    _cost_local.last_pricing_unknown = pricing_unknown
    if cost_usd is not None and budget is not None:
        budget.add(cost_usd)

    if parsing_error is not None or parsed is None:
        error = StructuredOutputError(
            f"{schema.__name__} structured output was not parsed"
            + (f": {parsing_error}" if parsing_error is not None else ".")
        )
        record_trace_event(
            trace_id,
            "llm_call",
            success=False,
            model_name=model_name,
            schema=schema.__name__,
            prompt=prompt,
            raw_response=raw,
            parsed_response=parsed,
            parsing_error=parsing_error,
            usage_metadata=usage,
            cost_usd=cost_usd,
            pricing_unknown=pricing_unknown,
            latency_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )
        raise error

    if not isinstance(parsed, schema):
        try:
            parsed = schema.model_validate(parsed)
        except Exception as exc:
            error = StructuredOutputError(f"{schema.__name__} failed schema validation: {exc}")
            record_trace_event(
                trace_id,
                "llm_call",
                success=False,
                model_name=model_name,
                schema=schema.__name__,
                prompt=prompt,
                raw_response=raw,
                parsed_response=parsed,
                parsing_error=parsing_error,
                usage_metadata=usage,
                cost_usd=cost_usd,
                pricing_unknown=pricing_unknown,
                latency_ms=(time.perf_counter() - start) * 1000,
                error=error,
            )
            raise error from exc

    record_trace_event(
        trace_id,
        "llm_call",
        success=True,
        model_name=model_name,
        schema=schema.__name__,
        prompt=prompt,
        raw_response=raw,
        parsed_response=parsed,
        usage_metadata=usage,
        cost_usd=cost_usd,
        pricing_unknown=pricing_unknown,
        latency_ms=(time.perf_counter() - start) * 1000,
    )
    return parsed
