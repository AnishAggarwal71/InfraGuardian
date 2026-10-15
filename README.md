# InfraGuardian

[![CI](https://github.com/AnishAggarwal71/InfraGaurdian/actions/workflows/ci.yml/badge.svg)](https://github.com/AnishAggarwal71/InfraGaurdian/actions/workflows/ci.yml)

An agentic infrastructure incident-response system: deterministic anomaly detection across
DB / CPU-mem / GPU domains, specialist LLM agents invoked only on flagged anomalies, a
LangGraph orchestrator that correlates cross-domain findings into a unified incident report,
and safety-gated routing to auto-remediation, human recommendation, or escalation. It also
watches a live host continuously (`cli.main watch`) and alerts the moment something trips a
threshold or escalates — not just one-shot analysis.

Built as a portfolio/FDE-style project: the pitch is bounded autonomy, evaluation rigor, and
failure handling — not "look, a LangGraph demo." Source of truth for requirements is
`InfraGuardian_PRD.docx`; `CLAUDE.md` translates that into the build plan actually followed,
and `DECISIONS.md` is the full decision log (what we did, why, what we rejected and why) for
every milestone.

## Quickstart

This is a CLI + local API, not a hosted web app — there's no UI to click through. Clone it, run
it locally:

```bash
uv sync
uv run python -m cli.main analyze gpu_ecc_errors
```

See CLI and API below for the full command/endpoint surface. A `render.yaml` Blueprint exists
for deploying the API as a free, unauthenticated, fake-model-only instance (see Deployment) —
that's an optional deployment path, not the primary way this project is meant to be used.

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
  everything else. Even a high-confidence, model-verdict-`SAFE` finding only reaches
  `AUTO_REMEDIATE` if `tools/preconditions.py::check_preconditions()` also confirms the specific
  rule breach that runbook targets is the *only* breach in that domain — a model's diagnosis is
  necessary but not sufficient; the deterministic precondition gate is what actually authorizes
  automation (`REAL_MODEL_VALIDATION.md` P1). This also closes an indirect prompt-injection
  path: even if attacker-controlled text (e.g. from a monitored query string) reached a
  specialist's free-text output, routing and preconditions only ever read typed
  `Severity`/`AutomationSafety`/`DetectionResult` fields — never the free text itself
  (`tests/test_prompt_injection.py`).
- **Remediation** (`tools/remediation.py`) executes only from a static allowlist the LLM can
  select from but never construct. Execution is simulated (no real infra in this project) —
  the mechanism being demonstrated is the safety gate and audit trail, not real remediation.
- **Incident store** (`storage/incident_store.py`) is a two-table SQLite file: full incident
  reports, and a separate FR-16 audit log of every executed action, idempotency-guarded by a
  `UNIQUE(incident_id, runbook_id)` constraint.

**Diagrams:** animated high-level and low-level system design walkthroughs, plus a narrated
"learn InfraGuardian" explainer, live under `docs/`:
[`system_design_high_level.html`](docs/system_design_high_level.html),
[`system_design_low_level.html`](docs/system_design_low_level.html),
[`learn_infraguardian.html`](docs/learn_infraguardian.html). Open any of them directly in a
browser (`open docs/system_design_high_level.html` on macOS) — no server needed.

## Model-agnostic LLM layer

`tools/llm.py` is the only module allowed to import a provider-specific LangChain package, and
it does so lazily via `init_chat_model("provider:model")`. Every agent and the orchestrator only
ever call `get_chat_model()` — switching from Anthropic to Gemini or an open-source model is an
`INFRAGUARDIAN_MODEL` environment variable change, never a code change.

When `INFRAGUARDIAN_MODEL` is unset, `get_chat_model()` returns `FakeInfraGuardianModel`: an
offline, zero-cost lookup table keyed on breached-rule names, used by default so the entire
pipeline, test suite, and evaluation harness run with no API key and no network access. It does
not simulate real reasoning — that's a deliberate, documented simplification, not a quality claim.

## Real metric collection

`tools/collectors.py` defines `MetricCollector` as a `Protocol` — anything exposing
`.collect() -> MetricSample` works, no inheritance required. Three implementations exist today:
`SimulatorCollector` (replays a fixed scenario sample-by-sample, used by every demo/test),
`PsutilCollector` (reads this host's real CPU/mem/swap/load via `psutil`), and
`PostgresCollector` (queries a live PostgreSQL instance for DB domain metrics via `psycopg2`).

Run `PsutilCollector` directly against your own machine's live metrics — no fixtures involved:

```bash
uv run python -m cli.main live
```

GPU domain is still simulator-only in this MVP (a `pynvml` collector would implement the same
Protocol — PRD FR-03's "collector abstraction is load-bearing" is the reason adding each new
real collector required zero changes to the rule engine, agents, or orchestrator).

## Real Postgres integration

`PostgresCollector` connects to a running PostgreSQL instance and queries `pg_stat_activity`,
`pg_settings`, `pg_stat_database`, and `pg_stat_statements` for the five DB domain metrics. A
docker-compose demo creates a controlled connection pool exhaustion scenario against a real
Postgres:

```bash
docker compose up -d                                  # start Postgres (max_connections=50)
uv run python scripts/demo_postgres_load.py &         # hold ~43 connections (>85% utilization)
uv run python scripts/demo_postgres.py                # collect real metrics → full pipeline
# pipeline detects connection_pool_utilization_pct breach, diagnoses, routes
```

Known limitation: `replication_lag_sec` is 0.0 in this single-node demo — there is no replica
to lag against. This is documented honestly rather than fabricated.

## Continuous monitoring

```bash
uv run python -m cli.main watch --interval 15
```

Unlike `live` (one sample, one report, exit), `watch` is the actual "agentic infrastructure
incident-response system" running continuously against a live host: it polls `PsutilCollector`
every `--interval` seconds, prints a quiet one-line heartbeat while healthy, and surfaces an
anomaly loudly — a full bannered incident report plus a desktop toast notification (`plyer`,
cross-platform including Windows) — the moment one is detected.

- **Edge-triggered alerting, not per-tick spam.** It alerts once on the healthy→anomalous
  transition and again only if severity escalates while still anomalous — never on an unchanged
  ongoing anomaly or on recovery. A tool that pages on every tick of a flapping metric trains
  people to ignore it, which defeats the point; this is the same reasoning a real on-call system
  needs. Logic lives in `cli/main.py::_should_alert`, unit-tested in isolation in
  `tests/test_watch.py`.
- **Persistence only on anomalies.** `run_incident` is called with `persist=False` on every
  tick; an anomalous tick is then persisted explicitly. `analyze`/`live` persist unconditionally
  (fine for one-shot use), but a long-running watcher doing that would flood the `incidents`
  table with thousands of identical healthy rows — a deliberate deviation, logged in
  `DECISIONS.md`.
- **Desktop notification failures degrade, never crash.** `_notify` lazily imports `plyer`
  inside the function (same lazy-import convention as `tools/llm.py`'s provider SDKs) and wraps
  the call in `try/except` — a missing backend prints a one-line stderr warning; the alert
  itself was already printed to stdout regardless (fail-soft, per PRD §9's "tool failures are
  represented explicitly, never swallowed").
- **Known limitation:** `watch` covers the CPU/mem domain only, same as `live` — DB/GPU remain
  simulator-only.

## Setup

```bash
uv sync
```

`pyproject.toml` pins the package index to public PyPI explicitly
(`[[tool.uv.index]]`, `default = true`) so `uv sync`/`uv lock` resolve the same way on any
machine, regardless of a developer's own global `uv`/`pip` config (e.g. a corporate Artifactory
mirror set as that machine's default index) — the project's dependency resolution isn't allowed
to depend on where it happens to be run. If you change dependencies, regenerate the lock with
`uv lock` before committing.

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
uv run python -m cli.main live                                     # analyze this host's real CPU/mem/swap/load
uv run python -m cli.main watch --interval 15                      # continuously watch this host, alert on anomalies
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
a production claim — the fixture harness only proves the deterministic pipeline (thresholds,
routing, calibration) is wired correctly; it says nothing about real LLM reasoning quality. See
below for that.

### Real-model results (Claude Haiku, 3 scenarios)

The 104-case harness above is intentionally fake-model-only (deterministic, free, no API key for
CI). To get an honest read on real reasoning quality, 3 scenarios were separately run against
Claude Haiku with a real API key. Full methodology, traces, and follow-ups:
[`evaluation/REAL_MODEL_VALIDATION.md`](evaluation/REAL_MODEL_VALIDATION.md).

| Scenario | Fake-model expected | Claude Haiku result | Outcome |
|---|---|---|---|
| `gpu_ecc_errors` | `critical` / `escalate` | `high` / `recommend` | **Fail** — model downgraded a critical hardware fault |
| `db_replication_lag` | `high` / `recommend` | `medium` / `recommend` | Routing safe, severity under-classified |
| `db_connection_pool_exhaustion` | `high` / `auto_remediate` | `high` / `recommend` | Safe divergence — model declined automation given only one sample |

1 of 3 cases matched fake-model routing exactly; 0 false auto-remediations across all 3. This is
a small manual spot-check, not a statistically meaningful accuracy claim — publishing it ugly
rather than rounding it up is the point. The `gpu_ecc_errors` failure is the one that mattered: a
real model silently downgrading a zero-tolerance hardware fault below the escalation threshold
is exactly the failure mode this project's safety invariants exist to catch. It's now closed by a
deterministic severity floor (`tools/severity.py::severity_floor()`, applied in
`orchestrator._synthesize()`) that forces `ecc_errors > 0` to `CRITICAL` regardless of what the
model returns, backed by 11 regression tests (`tests/test_severity_floor.py`) — the model can no
longer soften that specific signal, by construction, not by a prompt instruction it could ignore.
The other two findings (severity drift to `medium`, and the model being more conservative than
the fake baseline about automation safety) remain open P1 items — see the validation doc.

### Real-model investigation traces

The fixture evaluator intentionally forces the offline fake model. For a manual live-provider
run, set `INFRAGUARDIAN_TRACE_DIR` to capture a local `events.jsonl` artifact containing the
incident input, every LLM prompt and raw/parsed response, usage metadata, parser errors,
latency, and final incident report. The trace never records API keys, but prompts and responses
can contain operationally sensitive data, so `evaluation/real-runs/` is gitignored by default.

```powershell
$run = "evaluation/real-runs/haiku-gpu-ecc-$(Get-Date -Format yyyyMMdd-HHmmss)"
$env:INFRAGUARDIAN_TRACE_DIR = Join-Path $PWD $run
uv run python -m cli.main analyze gpu_ecc_errors --no-persist --json |
    Tee-Object (Join-Path $run "report.json")
```

An unparsed or schema-invalid live-model response is recorded in the trace and represented by
the orchestrator's fail-closed sentinel finding; it cannot flow downstream as a `None` finding
or trigger automated remediation.

### Live evaluator (`evaluation/live_evaluator.py`)

The manual spot-check above doesn't repeat cases or quantify variance. `evaluation/live_evaluator.py`
is the harness `REAL_MODEL_VALIDATION.md`'s P1 asks for: it runs the same 17 `INCIDENT_CASES`
fixtures the fake-model evaluator uses, repeated N times per case against a real provider/model,
and writes an immutable JSON manifest plus a per-case JSONL trace tree — all without this repo
or CI ever making a real API call. It makes zero LLM calls on its own; running it against a real
model and spending real API budget is a deliberate choice you make yourself with your own key:

```bash
ANTHROPIC_API_KEY=... uv run python -m evaluation.live_evaluator \
    --model anthropic:claude-sonnet-4-5-20250929 --repeats 5
```

Reports, per case and overall: severity/routing/RCA accuracy against the hand-derived fixture
expectations, how many *distinct* severities/routing decisions a case produced across its
repeats (the run-to-run variance signal — 1 means the model was consistent, >1 means it wasn't),
p50/p95 latency, total and per-run USD cost (via the same `tools.llm` pricing/`pricing_unknown`
path the fake-model path uses), and an explicit count of PRD §9 safety-invariant violations
(critical-severity auto-remediation, confidence-gate bypass, false auto-remediation) computed
from the live report's own fields — this is the same category of check that caught the
`gpu_ecc_errors` severity-downgrade P0 above, now repeatable on demand instead of a one-off
manual run. `--max-total-cost-usd` caps the harness's own cumulative spend across the whole run,
on top of (not instead of) the existing per-incident `INFRAGUARDIAN_MAX_COST_USD_PER_RUN` cap.
Omitting `--model` runs the harness against the offline fake model instead — that's how
`tests/test_live_evaluator.py` covers the harness's own mechanics (model resolution, manifest
I/O, trace isolation, env-var restoration) without an API key. Manifests write to
`evaluation/real-runs/<run_id>/manifest.json`, gitignored for the same reason as the trace
artifacts above: they can contain prompts, responses, and other operationally sensitive data.

## Observability

`tools/observability.py` is a thread-safe, in-memory recorder the orchestrator writes to on
every specialist/synthesis call and every completed incident: invocation counts, per-agent
latency, failure rate, end-to-end latency (p50/p95), routing-decision distribution, and
confidence distribution. Exposed via `GET /metrics` and `cli.main metrics`.

Cost-per-incident (PRD §11) is computed from real token usage: every specialist/synthesis call
goes through `tools.llm.invoke_structured()`, which requests `include_raw=True` from
`with_structured_output()` so the raw message's `usage_metadata` survives, prices it against a
small hardcoded per-model table, and hands the result to `record_agent_call(cost_usd=...)`.
`snapshot()` averages the recorded per-call costs over completed incidents.

The pricing table (`tools/llm._PRICING_PER_MILLION_TOKENS`) only covers a handful of Anthropic
model strings. A call against an unlisted real model is distinguished from the offline fake
model's genuine `$0.00`: `_price_usage()` returns `(None, pricing_unknown=True)` for nonzero
token usage against an unlisted model, vs. `(0.0, pricing_unknown=False)` for the fake model's
zero usage. `tools.llm.get_last_pricing_unknown()` and `ObservabilitySnapshot.pricing_unknown_calls`
surface that gap explicitly instead of letting it silently average into `cost_per_incident_usd`
as a free call. Extend the pricing table when adding a new model to close the gap for real.

`invoke_structured()` also wraps every provider call in a timeout (`_DEFAULT_TIMEOUT_SEC`, 20s)
and up to `_MAX_RETRIES` (2) retries with exponential backoff for transient failures, and
supports an opt-in per-run cost budget: set `INFRAGUARDIAN_MAX_COST_USD_PER_RUN` and
`run_incident()` raises `CostBudgetExceededError` (routed through the same fail-closed sentinel
path as any other LLM failure — PRD §9) once a run's known cost reaches the cap, rather than
making an unbounded number of calls against a single incident.

## Deployment

```bash
docker build -t infraguardian .
docker run -p 8000:8000 --env-file .env infraguardian
# or, for a persistent incident store across restarts:
docker compose up --build
```

- `.env` (copy from `.env.example`) carries `INFRAGUARDIAN_MODEL`/`ANTHROPIC_API_KEY`; omit both
  to run the container against the offline fake model.
- `docker-compose.yml` mounts a named volume over `/app` so `infraguardian.db` survives a
  container restart; a code change requires `docker compose down -v` to pick up (documented
  tradeoff in the compose file's comment, acceptable at this project's single-container scope).
- Confirm with `curl localhost:8000/health` once the container is up.
- Out of scope at this MVP stage, same as the rest of the project: no auth in front of the API,
  no TLS termination, no Postgres/multi-replica story, no PagerDuty/Slack integration.

**Optional public deploy (Render):** `render.yaml` is a Render Blueprint — connect this GitHub
repo on Render, it detects the file and deploys the existing `Dockerfile` as a free web service
with `GET /health` as the health check. The Dockerfile's `CMD` uses shell form
(`--port ${PORT:-8000}`) specifically so it honors Render's injected `$PORT` while still
defaulting to 8000 for local `docker run`/`docker compose up` — an exec-form `CMD` with a
literal port wouldn't expand a shell variable. `render.yaml` deliberately sets no
`ANTHROPIC_API_KEY` — a deploy from this Blueprint always runs the offline fake model, since an
unauthenticated public `POST /analyze` with a real paid key wired in is unbounded cost/abuse
exposure (there's no auth or rate limiting in this MVP — see Limitations). This isn't currently
deployed anywhere; this project is meant to be run locally (see Quickstart above).

## Tests

```bash
uv run pytest -q
```

143 passing cases across 21 files, covering metric contracts, the rule engine (every scenario's
first/last sample classified correctly), the fake model, specialist agents, LangGraph
fan-out/fan-in and synthesis, confidence calibration, routing (a dedicated table-driven
safety-invariant checklist — critical-never-auto-remediates and low-confidence-always-escalates
are explicit test cases, not incidental coverage), runbook preconditions (a model-diagnosed
`SAFE` finding alone is not sufficient for `AUTO_REMEDIATE` unless the deterministic rule breach
is isolated to that runbook's required signal), prompt-injection resistance (`pg_stat_statements`
is never read for raw query text, and routing/preconditions never consult free-text fields even
when a specialist's output is fully attacker-controlled), remediation runbooks, incident-store
idempotency, the orchestrator's fail-closed behavior on a raising specialist, real and simulated
metric collectors, the Postgres collector (monkeypatched, no live DB needed), the deterministic
severity floor for safety-critical rules (P0 fix — parameterized across every ECC breach
variant), the continuous watch loop's edge-triggered alerting and persist-only-on-
anomaly rule, LLM cost pricing/timeout/retry-backoff/cost-budget behavior (including the
cross-thread cost-budget propagation LangGraph's parallel specialist branches require — see
`tools/llm.py`'s `_CostBudget`/`ContextVar` design), the live-evaluator harness's own mechanics
(model resolution, manifest I/O, trace isolation — run against the fake model, same as everything
else here), the API, and the evaluation harness itself. All run offline against the fake model —
no network or API key required for CI. GitHub Actions (`.github/workflows/ci.yml`) runs this
exact command plus the
evaluation harness on every push — see the badge at the top of this README.

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
- The 104-case harness above is fake-model-only by design (deterministic, free, no API key
  needed for CI). A separate 3-scenario spot-check against Claude Haiku is published in the
  Evaluation section above — a manual sample, not a held-out eval set, and the numbers there are
  intentionally not rounded up to look better than they are (1 of 3 cases matched the fake-model
  routing expectation).
- The pricing table backing `cost_per_incident_usd` covers a handful of model strings; an
  unlisted model with real token usage is now reported as `pricing_unknown` (see Observability
  above) rather than silently priced as free, but the table itself still needs manual extension
  to actually price a new model.
- The real collector (`PsutilCollector`) covers CPU/mem; `PostgresCollector` covers DB. GPU
  domain remains simulator-only in this MVP.
- No auth on the API/Docker deployment — see Deployment below; this is a conscious MVP scope
  limit, not an oversight.

## Simulator → production collector migration path

`tools/collectors.py` defines `MetricCollector` as a `Protocol` (structural typing, not
inheritance): anything exposing `.collect() -> MetricSample` works. `SimulatorCollector` replays
fixed scenarios; `PsutilCollector` (see Real metric collection above) is the first real
implementation, covering CPU/mem. A GPU collector (`pynvml`) or DB collector (a driver's status
queries) just needs to implement the same `.collect()` signature — no change required to the
rule engine, specialist agents, orchestrator, or anything downstream of ingestion (PRD FR-03).
This is the concrete mechanism behind "collector abstraction is load-bearing."

## Repository layout

```
infra-guardian/
├── orchestrator.py          # LangGraph StateGraph: screen -> specialists -> synthesize -> route
├── agents/                  # schemas.py (shared contracts) + one specialist per domain
├── tools/                   # metrics, thresholds, collectors, llm, confidence, severity,
│                             #   routing, remediation, scenarios, observability, tracing
├── api/server.py            # FastAPI: POST /analyze, GET /health, GET /metrics
├── cli/main.py               # CLI: list, analyze, live, watch, metrics
├── storage/incident_store.py # SQLite incident + remediation-action audit trail
├── evaluation/               # fixtures/ + evaluator.py + live_evaluator.py + reports/ +
│                             #   REAL_MODEL_VALIDATION.md
├── docs/                      # animated system design + learn-InfraGuardian HTML walkthroughs
├── tests/
├── scripts/demo_incident.py  # manual verification script (not the FR-17 CLI — see DECISIONS.md)
├── scripts/demo_postgres.py  # real Postgres integration demo (see Real Postgres integration)
├── scripts/demo_postgres_load.py  # connection pressure generator for the Postgres demo
├── scripts/postgres_init.sql # Postgres init (enables pg_stat_statements)
├── Dockerfile / docker-compose.yml / .dockerignore / .env.example
├── render.yaml                # Render Blueprint for the public demo deploy
├── LICENSE                    # MIT
├── DECISIONS.md               # full build decision log
├── MILESTONES.md              # time-boxed milestone tracking
└── requirements.txt           # generated via `uv export` — uv.lock is the source of truth
```
