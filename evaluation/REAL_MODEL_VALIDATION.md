# Real-model validation findings

This document records manual validation against Claude Haiku on 2026-10-11. It is deliberately
separate from `evaluation/evaluator.py`, which forces the offline `FakeInfraGuardianModel` so
that CI remains deterministic and free. The results below are a small exploratory baseline, not
aggregate accuracy claims about a production model.

## Evidence collected

Three predefined scenarios were run through the CLI with a real Anthropic API key. The runs
contained one final metric sample per domain, so the model did not receive a time series. The
first three runs predated the local trace recorder; their final JSON reports were saved from the
terminal. A subsequent `gpu_ecc_errors` rerun used the recorder and captured its complete
LLM-call trace locally under the gitignored `evaluation/real-runs/` directory.

| Scenario | Fake-model expected result | Claude Haiku result | Assessment |
|---|---|---|---|
| `gpu_ecc_errors` | `critical` / `escalate` | `high` / `recommend` | **Fail: a critical hardware signal was downgraded by the model before routing.** |
| `db_replication_lag` | `high` / `recommend` | `medium` / `recommend` | Routing was safe, but severity was under-classified. |
| `db_connection_pool_exhaustion` | `high` / `auto_remediate` | `high` / `recommend` | Safe divergence: the model considered automation unsafe with only one sample and no session-level evidence. |

All three reports were schema-valid and contained plausible domain reasoning. The observed RCA
themes matched the inputs: GPU hardware degradation, replica-apply/contention causes, and DB
connection saturation. The experiment produced zero false auto-remediations, but only one of
three cases matched the fake-model routing expectation.

## Issues found

### P0 — critical routing depends on model severity — IMPLEMENTED

`ecc_errors > 0` is a deterministic zero-tolerance breach, but a real model returned `high`.
Because `tools.routing.route()` receives model-produced report severity, it returned
`recommend` rather than `escalate`. A real model must not be able to downgrade a safety-critical
signal.

**Fix:** `tools/severity.py` adds a deterministic severity floor (`severity_floor()`) applied in
`orchestrator._synthesize()` right next to the existing confidence calibration. The
`_RULE_MIN_SEVERITY` mapping forces `ecc_errors` to `CRITICAL` regardless of what the model
returns. 11 regression tests in `tests/test_severity_floor.py` cover every variant, including an
end-to-end test through `run_incident()` proving ECC breach → CRITICAL → ESCALATE.

### P1 — agents see only a point-in-time sample

All live responses reasonably noted that no trend was available. The CLI resolves a scenario to
its final sample, and the live collector passes only one poll. This weakens RCA confidence and
makes auto-remediation unsafe by default.

**Recommended change:** introduce a bounded per-domain metric window (for example, the last
five to ten samples plus rate-of-change features) into the collector contract and specialist
prompt. Preserve the current single-sample path as an explicitly lower-confidence fallback.

**Acceptance test:** a rising replication-lag trajectory is distinguishable from a recovering or
one-off spike; reports cite the observed trend rather than claiming one is unavailable.

### P1 — auto-remediation expectation is not supported by live evidence

The fake model marks connection-pool exhaustion `SAFE`, but Claude Haiku correctly pointed out
that pool restarts or session termination could drop in-flight work. The current prompt does not
provide the narrow allowlisted action or the preconditions needed to prove it is safe.

**Recommended change:** model diagnosis and automation authorization should be separate. Define
deterministic runbook preconditions, such as verified idle/leaked connections and no active
transaction impact, and require them before executing `db.recycle_connection_pool`. If the
collector cannot provide that evidence, the correct route is `RECOMMEND`.

**Acceptance test:** auto-remediation is impossible without the required preconditions and is
possible only when each precondition is present and the deterministic safety gate passes.

### P1 — live results need a durable evaluation ledger

The original test that returned `parsed=None` crashed before output could be inspected. The
project now records opt-in JSONL traces through `INFRAGUARDIAN_TRACE_DIR`; each event includes
the prompt, raw/parsed response, parser error, usage metadata, latency, and final report. The
artifacts are gitignored because they may contain operational data.

**Recommended change:** build `evaluation/live_evaluator.py` to run a labelled fixture set
against a named provider/model, write immutable run manifests, compare deterministic expectations
field by field, and summarize regressions separately from the fake-model baseline. Repeat each
case to quantify output variability; use human review for semantic RCA quality rather than
letting the same model grade itself.

**Acceptance test:** a live run produces a manifest, per-call trace IDs, field-level expected vs
actual results, safety-invariant failures, token usage, latency, and cost.

### P1 — current Haiku cost reporting is inaccurate

The live trace identified the configured model as `claude-haiku-5-5`, while the pricing table
does not contain that exact identifier. The recorder therefore reports `$0.00`; this is an
internal accounting gap, not evidence that the provider call was free.

**Recommended change:** update the model-pricing registry from the provider's current published
prices whenever a model is enabled, and emit `cost_usd: null` plus an explicit `pricing_unknown`
flag for unlisted models rather than silently returning zero.

## Recommended implementation order

1. ~~**Blocker:** enforce rule-level criticality for ECC errors and add the routing regression
   tests.~~ **Done** — `tools/severity.py`, applied in `orchestrator._synthesize()`, 11 tests.
2. Add the live evaluation ledger on top of the new trace recorder.
3. Upgrade collectors/prompts from a final sample to a bounded metric window.
4. Replace model-only automation safety with runbook-specific deterministic preconditions; revise
   fake fixtures where they assume safety without the evidence a real system would need.
5. Correct price handling and add timeout, retry, concurrency, and per-run cost budgets.
6. Demonstrate one non-simulated integration: for example, Dockerized Postgres, a controlled
   connection leak/load generator, real collector metrics, and an audited recommendation.

## Portfolio framing

The valuable portfolio story is not “the fake-model evaluator got 100%.” It is:

> I separated deterministic safeguards from probabilistic reasoning, instrumented the first
> live-model run, found that the model could downgrade a critical ECC fault, and turned that
> result into a deterministic routing invariant and a regression test.

Keep the fake-model numbers labelled as pipeline-regression results. Publish real-model results
only with the provider/model name, run date, fixture count, repeated-run methodology, and any
observed safety failures.
