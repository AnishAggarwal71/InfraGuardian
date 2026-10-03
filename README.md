# InfraGuardian

An agentic infrastructure incident-response system: deterministic anomaly detection across
DB / CPU-mem / GPU domains, specialist LLM agents invoked only on flagged anomalies, a
LangGraph orchestrator that correlates cross-domain findings into a unified incident report,
and safety-gated routing to auto-remediation, human recommendation, or escalation.

Built as a portfolio/FDE-style project: the pitch is bounded autonomy, evaluation rigor, and
failure handling — not "look, a LangGraph demo." Source of truth for requirements is
`InfraGuardian_PRD.docx`; `CLAUDE.md` translates that into the build plan actually followed,
and `DECISIONS.md` is the full decision log (what we did, why, what we rejected and why) for
every milestone.

## Architecture

```
Metric Sources → Ingestion → Rule Pre-Screen → Specialist Agents (parallel, anomalies only)
    → LangGraph Synthesis → Routing & Safety Gates → Auto-Remediate / Recommend / Escalate
    → Incident Report (+ SQLite audit trail)
```

- **Rule pre-screen** (`tools/thresholds.py`) runs before any LLM call. A healthy domain never
  reaches a specialist — this is enforced structurally in the LangGraph conditional edge
  (`orchestrator.py::_route_anomalous_domains`), not just by convention.
- **Specialist agents** (`agents/{db,cpu_mem,gpu}_agent.py`) run as parallel LangGraph branches,
  one per anomalous domain, each returning a schema-valid `SpecialistFinding` via
  `with_structured_output()`.
- **Synthesis** (`orchestrator.py::_synthesize`) correlates findings across domains into a
  unified root cause, explicitly labeling causal claims as hypotheses unless backed by directly
  measured evidence (PRD §9).
- **Confidence calibration** (`tools/confidence.py`) caps the model's self-reported confidence
  against a deterministic signal (domain corroboration + automation-safety verdicts) before it
  ever reaches the routing gate — a hallucinated confidence number can't flow through unchecked.
- **Routing & safety gates** (`tools/routing.py`) are a pure deterministic function, not an LLM
  call: critical severity and low confidence are checked first and unconditionally override
  everything else.
- **Remediation** (`tools/remediation.py`) executes only from a static allowlist the LLM can
  select from but never construct. Execution is simulated (no real infra in this project) —
  the mechanism being demonstrated is the safety gate and audit trail, not real remediation.
- **Incident store** (`storage/incident_store.py`) is a two-table SQLite file: full incident
  reports, and a separate FR-16 audit log of every executed action, idempotency-guarded by a
  `UNIQUE(incident_id, runbook_id)` constraint.

## Model-agnostic LLM layer

`tools/llm.py` is the only module allowed to import a provider-specific LangChain package, and
it does so lazily via `init_chat_model("provider:model")`. Every agent and the orchestrator only
ever call `get_chat_model()` — switching from Anthropic to Gemini or an open-source model is an
`INFRAGUARDIAN_MODEL` environment variable change, never a code change.

When `INFRAGUARDIAN_MODEL` is unset, `get_chat_model()` returns `FakeInfraGuardianModel`: an
offline, zero-cost lookup table keyed on breached-rule names, used by default so the entire
pipeline, test suite, and evaluation harness run with no API key and no network access. It does
not simulate real reasoning — that's a deliberate, documented simplification, not a quality claim.

## Setup

```bash
uv sync
```

Runs entirely offline by default (fake model). To use a real provider:

```bash
INFRAGUARDIAN_MODEL=anthropic:claude-sonnet-4-5-20250929 ANTHROPIC_API_KEY=... \
    uv run python -m cli.main analyze db_connection_pool_exhaustion
```

## CLI

```bash
uv run python -m cli.main list                                    # available scenarios
uv run python -m cli.main analyze db_connection_pool_exhaustion    # human-readable summary
uv run python -m cli.main analyze gpu_ecc_errors --json            # full IncidentReport JSON
uv run python -m cli.main analyze db_healthy cpu_mem_healthy --no-persist
uv run python -m cli.main metrics                                 # self-observability snapshot
```

## API

```bash
uv run uvicorn api.server:app --reload
```

- `POST /analyze` — body is `{"scenarios": [...]}` and/or `{"samples": [...]}` (raw per-domain
  metric dicts); returns a full `IncidentReport`.
- `GET /health` — liveness check.
- `GET /metrics` — self-observability snapshot (PRD §11).

```bash
curl -s -X POST localhost:8000/analyze -H 'content-type: application/json' \
    -d '{"scenarios": ["db_connection_pool_exhaustion"]}'
```

## Demo scenarios

12 hand-authored scenarios (4 per domain: 1 healthy + 3 degraded), each a literal, fixed
trajectory — no RNG — defined in `tools/scenarios.py`. Two worth running to see the safety
gates in action:

- `db_connection_pool_exhaustion` → high confidence, single domain, SAFE → **auto-remediates**
  (`db.recycle_connection_pool`), audit row written to `infraguardian.db`.
- `gpu_ecc_errors` → severity resolves to **CRITICAL** → **escalates** unconditionally, even
  though the underlying confidence (0.65) would otherwise be in range. This is the single most
  important safety behavior to see working: critical incidents never auto-remediate, full stop.

## Evaluation

```bash
uv run python -m evaluation.evaluator
```

104 fixture cases (87 detection cases spanning every scenario's sample-by-sample trajectory, 17
end-to-end incident cases including 4 cross-domain combinations), run against the fake model.
Expected values in `evaluation/fixtures/` were hand-derived from the threshold constants and
routing/calibration logic — not generated by running the pipeline and recording its own output —
so a real regression in any of those modules would actually be caught, not just confirmed as
"whatever the code currently does."

Current measured results (fake model, this fixture set — see `evaluation/reports/latest.json`
for the full per-case detail):

| Metric | Target (PRD §10) | Measured |
|---|---|---|
| Detection accuracy | ≥ 95% | 100% |
| RCA accuracy | ≥ 85% | 100% |
| Routing accuracy | ≥ 95% | 100% |
| False auto-remediations | 0 | 0 |
| Schema validity | ≥ 99% | 100% |
| Latency p50 / p95 | — | ~1ms / ~3ms (fake model; no network) |

These are measured numbers against this project's own fixtures and the offline fake model, not
a production claim — real-provider latency/cost and a held-out eval set are the natural next
step once a model API key is in place.

## Observability

`tools/observability.py` is a thread-safe, in-memory recorder the orchestrator writes to on
every specialist/synthesis call and every completed incident: invocation counts, per-agent
latency, failure rate, end-to-end latency (p50/p95), routing-decision distribution, and
confidence distribution. Exposed via `GET /metrics` and `cli.main metrics`.

**Known gap:** cost-per-incident (PRD §11) is not implemented. `with_structured_output()`
returns a parsed Pydantic instance, not the raw message a provider attaches token-usage
metadata to — getting real token counts needs `include_raw=True` and reshaping every call site.
Reported explicitly as `null` rather than faked.

## Tests

```bash
uv run pytest -q
```

79 passing cases across 13 files, covering metric contracts, the rule engine (every scenario's
first/last sample classified correctly), the fake model, specialist agents, LangGraph
fan-out/fan-in and synthesis, confidence calibration, routing (a dedicated table-driven
safety-invariant checklist — critical-never-auto-remediates and low-confidence-always-escalates
are explicit test cases, not incidental coverage), remediation runbooks, incident-store
idempotency, the orchestrator's fail-closed behavior on a raising specialist, the API, and the
evaluation harness itself. All run offline against the fake model — no network or API key
required for CI.

## Limitations

- Remediation execution is simulated — no real DB/host/GPU is touched. The safety-gate and
  audit mechanism is real; the "action" behind it is a logged no-op.
- Thresholds in `tools/thresholds.py` are judgment calls chosen to classify the hand-authored
  scenarios correctly and defensibly, not values tuned from real production incident data.
- The fake model's canned findings/confidence are a lookup table over breached-rule names, not a
  simulation of real LLM reasoning quality — evaluation numbers against it describe pipeline
  correctness, not RCA quality from an actual model.
- Cross-domain incidents always route to at least `RECOMMEND` in this MVP, even at high
  confidence with every contributing finding `SAFE` — a single runbook only ever targets one
  domain, so auto-executing one domain's fix while leaving a corroborated second domain
  unaddressed is a scope mismatch this MVP doesn't attempt to solve.
- No real-provider (Anthropic/Gemini) latency, cost, or accuracy numbers yet — deferred until an
  API key is configured; the model-agnostic design means that's a config change, not a rebuild.
- Cost-per-incident observability is unimplemented (see Observability above).

## Simulator → production collector migration path

`tools/collectors.py` defines `MetricCollector` as a `Protocol` (structural typing, not
inheritance): anything exposing `.collect() -> MetricSample` works. The only implementation
today is `SimulatorCollector`, which replays a fixed scenario sample-by-sample. A real collector
(`psutil` for CPU/mem, `pynvml` for GPU, a DB driver's status queries for DB) just needs to
implement the same `.collect()` signature — no change required to the rule engine, specialist
agents, orchestrator, or anything downstream of ingestion (PRD FR-03). This is the concrete
mechanism behind "collector abstraction is load-bearing."

## Repository layout

```
infra-guardian/
├── orchestrator.py          # LangGraph StateGraph: screen -> specialists -> synthesize -> route
├── agents/                  # schemas.py (shared contracts) + one specialist per domain
├── tools/                   # metrics, thresholds, collectors, llm, confidence, routing,
│                             #   remediation, scenarios, observability
├── api/server.py            # FastAPI: POST /analyze, GET /health, GET /metrics
├── cli/main.py               # CLI: list, analyze, metrics
├── storage/incident_store.py # SQLite incident + remediation-action audit trail
├── evaluation/               # fixtures/ + evaluator.py + reports/latest.json
├── tests/
├── scripts/demo_incident.py  # manual verification script (not the FR-17 CLI — see DECISIONS.md)
├── DECISIONS.md               # full build decision log
├── MILESTONES.md              # time-boxed milestone tracking
└── requirements.txt           # generated via `uv export` — uv.lock is the source of truth
```
