# InfraGuardian — Project Instructions & Dev Plan

Source of truth: `InfraGuardian_PRD.docx` (v1.0, Oct 2026). This file translates that PRD into
an actionable build plan and the invariants every change in this repo must respect. If this
file and the PRD disagree, the PRD wins for *requirements*; this file wins for *how we build it*.

## What this is

An agentic infrastructure incident-response system: deterministic anomaly detection across
DB / CPU-mem / GPU domains, specialist LLM agents invoked only on flagged anomalies, a
LangGraph orchestrator that correlates cross-domain findings into a unified incident report,
and safety-gated routing to auto-remediation, human recommendation, or escalation. Portfolio/FDE
narrative (PRD §17): sell this as production-oriented AI systems engineering — bounded autonomy,
evaluation rigor, and failure handling — not as a LangGraph tech demo.

Non-goals (PRD §4): not a Datadog/Grafana replacement, no arbitrary shell execution by the LLM,
no fully autonomous remediation of critical incidents, no model training/fine-tuning, no
production-grade PagerDuty/Slack integration in MVP.

## Non-negotiable safety invariants (PRD §9)

These apply to every commit, regardless of which phase is in flight. Treat a change that
violates one of these as a bug, not a style issue.

- No arbitrary code or shell execution from model output — ever.
- Every executable remediation action maps to a predefined allowlist entry. The LLM selects
  from the allowlist; it cannot invent new executable actions.
- Critical-severity incidents never auto-remediate.
- Low-confidence findings fail closed to recommendation/escalation, not auto-remediation.
- Malformed or schema-invalid LLM output fails closed (recommendation/escalation), never
  silently defaults to auto-remediate.
- Tool/collector/API failures are represented explicitly in the incident report, never swallowed.
- Any causal claim in a report is labeled a hypothesis unless backed by measured evidence —
  correlation is not causation in the output contract.
- Remediations are idempotent or guarded against duplicate execution.
- Auto-remediation gate: confidence ≥ 0.8 AND action is on the safe allowlist AND severity <
  critical. All three, every time — don't loosen this for convenience during development.

## Architecture (PRD §12, §15)

```
Metric Sources → Ingestion → Parallel Specialist Agents → LangGraph Orchestrator →
  Auto-Remediate / Recommend / Escalate → Incident Report
```

Target repo layout:

```
infra-guardian/
├── orchestrator.py
├── agents/
│   ├── db_agent.py
│   ├── cpu_mem_agent.py
│   └── gpu_agent.py
├── tools/
│   ├── metrics.py          # simulators now; real collectors (psutil/pynvml/db queries) later
│   └── remediation.py      # allowlisted runbooks only
├── api/
│   └── server.py           # FastAPI, POST /analyze
├── evaluation/
│   ├── fixtures/           # 100+ scenario cases, expected anomaly/RCA/severity/routing/action
│   ├── evaluator.py
│   └── reports/
├── storage/
│   └── incident_store.py   # SQLite in MVP, Postgres post-MVP
├── tests/
├── requirements.txt
└── README.md
```

Stack: LangGraph, LangChain + Anthropic (Claude Sonnet), FastAPI, Pydantic, psutil, pynvml.
Collector abstraction is load-bearing: simulators and real collectors must be swappable without
touching anything downstream of ingestion (FR-03).

## Build plan

Work roughly in this order — each phase should be independently testable before moving on.
Numbers in parens are the PRD functional requirements it satisfies.

1. **Scaffolding** — repo init (git), directory layout above, `requirements.txt`, empty
   module stubs, README skeleton.
2. **Metric contracts & simulators** (FR-01–04) — `DBMetrics`, `CPUMemMetrics`, `GPUMetrics`
   Pydantic models with timestamp + source/domain metadata; deterministic scenario simulator
   producing realistic healthy/degraded cases; collector interface simulators implement so real
   collectors can drop in later without changing agents/orchestrator.
3. **Deterministic rule pre-screen** (FR-05, FR-06) — per-domain threshold checks that run
   before any LLM call; healthy domains must short-circuit without invoking a specialist.
4. **Specialist agents** (FR-07, FR-08) — one per domain, schema-valid structured output
   (root cause, severity, predictive risk, evidence, automation-safety assessment); designed to
   run concurrently (asyncio / LangGraph parallel branches).
5. **LangGraph orchestrator & synthesis** (FR-09–11) — stateful graph wiring specialist
   execution → synthesis; synthesis must explicitly state cross-domain correlations/evidence,
   not just concatenate findings; produces the unified incident report (contract in PRD §8).
6. **Routing & safety gates** (FR-12–16) — implement the three routing paths and the allowlist
   enforcement; every automated action logs rationale, confidence, target, timestamp, result.
7. **Remediation runbooks** — at least 3 concrete allowlisted runbooks, each idempotent/guarded.
8. **Persistent incident store** — SQLite, one row per incident, full decision trail.
9. **API & CLI** (FR-17–19) — FastAPI `POST /analyze`, CLI for running scenarios locally;
   Pydantic validation on both requests and model outputs.
10. **Evaluation harness** (PRD §10) — 100+ fixture cases; compute detection accuracy, RCA
    accuracy, routing accuracy, false-auto-remediation count, schema validity, rule-filtering
    rate, LLM cost/incident, latency p50/p95. These are product targets, not claims — don't
    hand-wave numbers in the README; run the harness and report what it actually says.
11. **Observability of InfraGuardian itself** (PRD §11) — invocation counts/tokens, per-agent
    latency/failure rate, end-to-end latency, cost/incident, routing & confidence distributions,
    collector failures, eval regression tracking.
12. **Tests** — scenario coverage, schema validation, routing-gate unit tests (including the
    "critical never auto-remediates" and "low confidence always escalates" cases explicitly),
    API/CLI smoke tests.
13. **README** — architecture, setup, demo scenarios, evaluation results, limitations,
    simulator→production collector migration path (this doubles as the portfolio writeup,
    PRD §17).

## Open questions (PRD §18) — working defaults

Not yet signed off by the user; treat as provisional and flag if a build decision depends on
one of these in a way that's hard to reverse later.

- **Which runbooks are MVP-safe?** Default to narrowly scoped, reversible actions with no
  blast radius beyond a single resource: e.g. restart a specific stuck service/process, clear a
  bounded cache, scale a non-critical worker pool within a capped range. Avoid anything touching
  data deletion, scaling down critical capacity, or network/security config.
- **Causal vs correlational evidence standard:** default to "correlational unless the evidence
  includes a direct measured link (e.g. a query directly traced to the resource spike)." Report
  labels every relationship as hypothesis unless that bar is met.
- **Confidence: model vs rule-derived vs calibrated?** Default to a calibrated combination —
  rule pre-screen contributes a deterministic floor/ceiling signal, LLM contributes a reasoning-
  based estimate, orchestrator combines them rather than trusting either alone.
- **Incident history retention for future RCA:** default to retaining full incident reports
  (not just summaries) in SQLite indefinitely for MVP; revisit retention policy post-MVP.
- **First real deployment target:** no default — this is a user call when it comes up, since it
  depends on infra they actually have access to.

## Working conventions for this repo

- Per the user's global coding standard: every function/method gets a doc comment covering
  purpose, non-obvious params, and return value; non-obvious logic (especially safety-gate
  logic) gets a WHY comment; new files get whatever header convention gets established in
  phase 1, applied consistently after that.
- Schema-valid structured output from LLM calls is enforced via Pydantic — don't accept a raw
  dict from a specialist agent and hope it's shaped right.
- When adding a new remediation action, it must be added to the allowlist explicitly — no
  dynamic action construction from LLM text.
- `pyproject.toml` pins the package index to public PyPI explicitly (`[[tool.uv.index]]`,
  `default = true`). This repo must resolve/lock the same way regardless of a developer's own
  global `uv`/`pip` config (e.g. a corporate Artifactory mirror set as that machine's default
  index) — a portfolio project has to be clonable and runnable outside any one person's network.
  Don't remove this pin or let `uv.lock` re-resolve against an internal/authenticated mirror;
  if dependencies change, regenerate with `uv lock` on a machine where this pin is honored and
  confirm `uv.lock` has no internal-mirror URLs before committing it (see `DECISIONS.md`,
  2026-10-09).
- Optional/notification-style dependencies (e.g. `plyer` for desktop toasts in `cli.main watch`)
  follow the same lazy-import convention as `tools/llm.py`'s provider SDKs: import inside the
  function that needs it, not at module top-level, so the module stays importable (and the test
  suite stays green) before `uv sync` has pulled that dependency in. Wrap the call in
  `try/except` and degrade to a logged warning — never let an optional convenience crash a loop
  that's otherwise working correctly (see `DECISIONS.md`, 2026-10-11).
- A long-running polling loop (`cli.main watch`) must not persist every tick unconditionally —
  only persist when the tick is actually anomalous. `run_incident`'s default-persist behavior is
  fine for one-shot CLI commands (`analyze`/`live`) but floods the `incidents` table if called
  on every tick of an indefinite loop (see `DECISIONS.md`, 2026-10-11).
- Safety-critical threshold rules must have a corresponding entry in
  `tools/severity._RULE_MIN_SEVERITY` that maps the rule name to a minimum `Severity`. The
  orchestrator applies `severity_floor()` to the model-produced severity before routing, same
  pattern as `calibrate()` for confidence. Omitting a floor is only valid when no routing
  invariant depends on the breach being classified at a minimum severity level (see
  `DECISIONS.md`, 2026-10-11, P0 fix).
- `tools/thresholds.py` must not import from `agents/schemas.py` — it is the deterministic
  pre-screen enforcement point (FR-05/FR-06) and stays free of model/agent coupling by design.
  Severity-floor logic that needs `Severity` lives in `tools/severity.py` instead, bridging
  both sides the same way `tools/confidence.py` does.
