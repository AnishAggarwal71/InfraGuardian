# InfraGuardian

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

## Live demo

```bash
curl -s https://infraguardian.onrender.com/health
curl -s -X POST https://infraguardian.onrender.com/analyze \
    -H 'content-type: application/json' -d '{"scenarios": ["gpu_ecc_errors"]}'
```

> Replace with the actual Render URL once deployed — see Deployment below.

This public instance runs the offline `FakeInfraGuardianModel` only, by design: an
unauthenticated public endpoint with a real paid API key wired in is unbounded cost/abuse
exposure (there's no auth or rate limiting in this MVP — see Limitations). The architecture
itself is model-agnostic and has been run against a real Anthropic model locally (see Model-
agnostic LLM layer and Evaluation below) — the public demo's scope is a deliberate safety
decision, not a capability limit.

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
`.collect() -> MetricSample` works, no inheritance required. Two implementations exist today:
`SimulatorCollector` (replays a fixed scenario sample-by-sample, used by every demo/test) and
`PsutilCollector` (reads this host's real CPU/mem/swap/load via `psutil`). Run it directly
against your own machine's live metrics — no fixtures involved:

```bash
uv run python -m cli.main live
```

DB and GPU domains are still simulator-only in this MVP (a real DB-status-query or `pynvml`
collector would implement the same Protocol — PRD FR-03's "collector abstraction is
load-bearing" is the reason this swap required zero changes to the rule engine, agents, or
orchestrator).

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

For observed real-model validation results and the prioritized follow-up plan, see
[`evaluation/REAL_MODEL_VALIDATION.md`](evaluation/REAL_MODEL_VALIDATION.md).

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

**Known gap:** the pricing table (`tools/llm._PRICING_PER_MILLION_TOKENS`) only covers a
handful of Anthropic model strings — an unlisted model (including the offline fake model) prices
every call at `$0.00` rather than raising, so swapping `INFRAGUARDIAN_MODEL` to an unlisted model
silently under-reports cost instead of failing loudly. Extend the table when adding a new model.

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

**Public demo (Render):** `render.yaml` is a Render Blueprint — connect this GitHub repo on
Render, it detects the file and deploys the existing `Dockerfile` as a free web service with
`GET /health` as the health check. The Dockerfile's `CMD` uses shell form
(`--port ${PORT:-8000}`) specifically so it honors Render's injected `$PORT` while still
defaulting to 8000 for local `docker run`/`docker compose up` — an exec-form `CMD` with a
literal port wouldn't expand a shell variable. `render.yaml` deliberately sets no
`ANTHROPIC_API_KEY` — the public deploy always runs the offline fake model (see Live demo
above).

## Tests

```bash
uv run pytest -q
```

94 passing cases across 15 files, covering metric contracts, the rule engine (every scenario's
first/last sample classified correctly), the fake model, specialist agents, LangGraph
fan-out/fan-in and synthesis, confidence calibration, routing (a dedicated table-driven
safety-invariant checklist — critical-never-auto-remediates and low-confidence-always-escalates
are explicit test cases, not incidental coverage), remediation runbooks, incident-store
idempotency, the orchestrator's fail-closed behavior on a raising specialist, real and simulated
metric collectors, the continuous watch loop's edge-triggered alerting and persist-only-on-
anomaly rule, LLM cost pricing, the API, and the evaluation harness itself. All run offline
against the fake model — no network or API key required for CI.

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
  needed for CI). Real-provider behavior is proven by running `analyze`/`live`/`watch` locally
  with a real `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY` configured — that's a manual spot check,
  not a held-out eval set, and no aggregate real-model accuracy numbers are published here.
- The pricing table backing `cost_per_incident_usd` covers a handful of model strings; an
  unlisted model prices at `$0.00` rather than erroring (see Observability above).
- The real collector (`PsutilCollector`) covers CPU/mem only — DB and GPU domains remain
  simulator-only in this MVP.
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
├── tools/                   # metrics, thresholds, collectors, llm, confidence, routing,
│                             #   remediation, scenarios, observability
├── api/server.py            # FastAPI: POST /analyze, GET /health, GET /metrics
├── cli/main.py               # CLI: list, analyze, live, watch, metrics
├── storage/incident_store.py # SQLite incident + remediation-action audit trail
├── evaluation/               # fixtures/ + evaluator.py + reports/latest.json
├── docs/                      # animated system design + learn-InfraGuardian HTML walkthroughs
├── tests/
├── scripts/demo_incident.py  # manual verification script (not the FR-17 CLI — see DECISIONS.md)
├── Dockerfile / docker-compose.yml / .dockerignore / .env.example
├── render.yaml                # Render Blueprint for the public demo deploy
├── LICENSE                    # MIT
├── DECISIONS.md               # full build decision log
├── MILESTONES.md              # time-boxed milestone tracking
└── requirements.txt           # generated via `uv export` — uv.lock is the source of truth
```
