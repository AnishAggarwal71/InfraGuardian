# InfraGuardian — Decision Log

Every non-trivial decision made while building this project, logged as we go. This is interview
prep material as much as it is project documentation — the goal is to be able to explain *why*
we did something and what else we considered, not just that it works.

Format per entry: **Decision**, **Why**, **Alternatives considered**, **Why not the alternatives**.

---

## 2026-10-02 — Scoping approach: time-boxed milestones over other scoping styles

**Decision:** Scope the project via time-boxed milestones (M1–M5 in `MILESTONES.md`), each
mapping to a subset of the 13-phase build plan in `CLAUDE.md`, ending in a demoable checkpoint.

**Why:** Gives a predictable cadence to show progress and a natural place to cut scope if the
timeline slips, without losing track of what the PRD actually requires.

**Alternatives considered:**
1. Right-size the MVP down from the full PRD scope (cut features before any milestones existed).
2. Define a leaner walking-skeleton v0 first (one domain, no LangGraph) before building out
   breadth.
3. Keep the full MVP scope as a single undifferentiated backlog.

**Why not the alternatives:** (1) was premature — hadn't yet validated which parts of the PRD
scope were actually hard before deciding what to cut. (2) is good practice but as a *milestone
sequencing* choice, not a replacement for milestones — M1 in the chosen plan already functions
as a walking skeleton (metric contracts + rule pre-screen, no LLM yet), so the benefit of (2)
is captured inside the milestone structure anyway. (3) gives no forcing function to produce
demoable increments, which matters for portfolio narrative (being able to show progress, not
just a final diff).

---

## 2026-10-02 — Milestone grouping and sequencing rationale

**Decision:** Group phases into M1 (ingest+detect, no LLM) → M2 (specialists+orchestration) →
M3 (safety+remediation+storage) → M4 (API/CLI) → M5 (evaluation+observability+tests+README).

**Why:** Each milestone has a coherent risk profile and a single demo criterion:
- M1 is pure plumbing/rules — cheapest place to catch contract mistakes before agents depend on them.
- M2 is the highest-risk milestone (LangGraph + concurrency + synthesis quality) — called out
  explicitly so slack gets allocated here first if the schedule slips.
- M3 is where every PRD §9 safety invariant is actually enforced — treated as a checklist
  against those invariants, not just "it runs."
- M4 is deliberately small since interfaces are thin wrappers once M1–M3 exist.
- M5 is explicitly protected from being compressed, because PRD §17 frames evaluation results,
  safety behavior, and cost/latency numbers as *the* portfolio pitch, not supporting detail.

**Alternatives considered:** Running eval/observability/tests (phases 10–12) earlier, in
parallel with M2, instead of bundled into the final milestone.

**Why not (fully) adopted:** Noted as an explicit option in `MILESTONES.md` ("Sequencing
notes") rather than baked into the milestone table — starting the eval harness as soon as M2's
fixtures exist can de-risk the "did we hit the accuracy targets" question early, but bundling
it into M5 keeps the milestone table simple. Left as a judgment call to exercise if the M2 demo
reveals correctness issues worth catching earlier.

---

## 2026-10-02 — Decision logging as a standing practice for this project

**Decision:** Maintain this file going forward; log every build decision here in the same
change where it's made, including what we did, why, what else we considered, and why we didn't
pick the alternatives. Also recorded as a persistent memory ([[decision-log-requirement]]) so
it survives across sessions.

**Why:** This is a portfolio project; the user is preparing for interviews about it and needs
to be able to articulate tradeoffs, not just demo working code.

**Alternatives considered:** Keeping rationale only in chat history / commit messages.

**Why not the alternatives:** Chat history isn't reviewable later as prep material, and commit
messages tend to describe *what* changed, not the *options rejected* — interview prep needs the
rejected-alternatives half explicitly, which neither format captures well by default.

---

## 2026-10-03 — M1 implementation: environment, repo layout, and determinism approach

**Decision:** Implemented M1 (metric contracts, collector abstraction, scenario catalog, rule
pre-screen, tests, demo script) per the approved plan at `.claude/plans/validated-mixing-papert.md`.
Key choices carried over from that plan into the actual repo:

1. **`uv` for dependency/env management** (`pyproject.toml`, `.venv`), with `requirements.txt`
   generated via `uv export --no-dev` purely so the PRD's literal file-tree expectation (§15
   shows `requirements.txt`) is satisfied for anyone skimming the repo. The lockfile (`uv.lock`)
   is the real source of truth; `requirements.txt` is a generated artifact, regenerate it with
   that export command rather than hand-editing it.
   - *Alternatives:* plain `venv` + `pip install -r requirements.txt` (PRD-literal, but no
     lockfile reproducibility); `poetry` (not installed, second resolver, no benefit here).
2. **Repo root is the project directory itself**, not a nested `infra-guardian/` folder — the
   PRD's tree is a module-layout spec, not a literal nesting instruction.
3. **`StrEnum` for `Domain`** (`tools/metrics.py`) — gives string-equality/serialization for
   free in Pydantic and in scenario-name matching (`name.startswith(domain.value)` in tests),
   without a separate str-mapping layer. Plain `Enum` would require explicit `.value` access
   everywhere it's compared against a string; `StrEnum` (3.11+) removes that friction.
4. **12 hand-authored scenarios (4/domain: 1 healthy + 3 degraded)**, each a literal list of
   5-8 samples with an explicit numeric trajectory (e.g. connection pool utilization climbing
   20%→100% across 8 ticks). No RNG anywhere in `tools/scenarios.py`. Confirmed in practice: the
   rule-engine tests parametrize over every scenario and assert the *first* sample of every
   scenario (healthy and degraded) is non-anomalous and the *last* sample of every degraded
   scenario is anomalous — this encodes "scenarios start healthy and degrade" as an executable
   invariant, not just a design note.
5. **Threshold values are judgment calls, not tuned-from-real-data numbers** (e.g.
   `DB_LATENCY_P95_MS_MAX = 200`, `GPU_ECC_ERRORS_MAX = 0`) — documented inline in
   `tools/thresholds.py` where the reasoning isn't self-evident (e.g. why any ECC error counts
   as anomalous: it indicates a hardware fault, not just "high usage"). This is a known
   simplification worth stating plainly in an interview: thresholds would come from historical
   baselining in a real deployment; here they're chosen to make the hand-authored scenarios
   classify correctly and defensibly.
6. **Demo script requires `-m` invocation** (`uv run python -m scripts.demo <name>`, not
   `uv run python scripts/demo.py <name>`) — running a script directly doesn't put the repo
   root on `sys.path`, so the `from tools...` imports fail; running as a module does. Added
   `scripts/__init__.py` and fixed the usage comment after hitting this directly. Not worth
   adding a `sys.path` hack for a one-off demo script.

**Why these over other options:** All already weighed in the approved plan; this entry mainly
records that the plan was followed as written, plus the two things only became visible during
implementation (`StrEnum` convenience, the `-m` invocation requirement) rather than being
foreseen in planning.

**Verification:** `uv run pytest -q` → 34 passed. `uv run python -m scripts.demo <scenario>` for
`db_connection_pool_exhaustion`, `cpu_mem_memory_pressure`, and `gpu_ecc_errors` manually
confirmed to transition healthy → anomalous with correct breached-rule reporting.

---

## 2026-10-03 — M2 scoping: model-agnostic LLM layer (user-driven requirement)

**Decision:** Before implementing M2, the user explicitly required the LLM layer stay
model-agnostic — they're still finalizing an Anthropic key and may switch to Gemini or an
open-source model later purely for cost reasons. Resolved via `AskUserQuestion` into two
sub-decisions:

1. **Provider routing via LangChain's `init_chat_model("provider:model")`** rather than LiteLLM
   or a hand-rolled adapter `Protocol`. All three options are genuinely provider-agnostic; the
   deciding factor was that LangGraph (already required by the PRD for orchestration) depends
   on LangChain anyway, so `init_chat_model` adds zero new abstraction layers, while LiteLLM
   would add a second, overlapping one just for model calls. A hand-rolled adapter gives more
   control but reinvents `with_structured_output`, which we need regardless for schema-valid
   RCA output (FR-07).
2. **An offline, zero-cost `FakeInfraGuardianModel` as the default** when no
   `INFRAGUARDIAN_MODEL` env var is set, instead of requiring a real key before anything runs.
   This was explicitly a portfolio/pragmatic tradeoff: it means the whole M2 pipeline (and its
   test suite) is runnable and demoable today, with zero API cost, and the test suite never
   needs network access or a real key to pass in CI. The fake model is a deliberately simple
   lookup table keyed on breached-rule names found in the prompt text — documented in
   `tools/llm.py` as explicitly *not* attempting to simulate real reasoning, so it's never
   mistaken for a claim about model quality.

**Interview framing:** the mechanism that actually delivers model-agnosticism is narrow and
inspectable — `tools/llm.py` is the only file allowed to import a provider-specific LangChain
package (`langchain_anthropic`, `langchain_google_genai`, etc.), and it does so lazily inside
`get_chat_model()`, gated on an environment variable. Every agent and the orchestrator only ever
call `get_chat_model()` — switching providers is an env var change, not a code change. This is a
good "show me, don't tell me" answer to "how is this actually model-agnostic."

---

## 2026-10-03 — M2 implementation: LangGraph fan-out/fan-in, structured output, and synthesis

**Decision:** Implemented M2 per the approved plan — `agents/schemas.py` (shared Pydantic
contracts), `tools/llm.py` (model factory + fake model), `agents/{db,cpu_mem,gpu}_agent.py`
(one `diagnose()` per domain), `orchestrator.py` (LangGraph `StateGraph`). Two implementation
details surfaced only once real code ran (not foreseen in planning), both worth stating plainly
since they're genuine "what went wrong and how did you find it" interview material:

1. **LangGraph requires an explicit reducer for any state key more than one parallel node
   writes to.** The first version of `orchestrator.py` had each specialist node read-modify-write
   the shared `findings` dict directly; running two specialists concurrently (the multi-domain
   test case) raised `InvalidUpdateError: Can receive only one value per step`. Fixed by giving
   `findings` an `Annotated[dict, _merge_findings]` reducer in the `TypedDict` and having each
   specialist node return only its own single-key dict (`{"findings": {domain: finding}}`) —
   LangGraph then merges per-branch writes itself instead of the nodes racing to merge by hand.
   This is the concrete mechanic behind FR-08 (parallel specialist execution): parallelism isn't
   free, it requires state-merge semantics to be explicit.
2. **`StrEnum` membership checks against the enum type itself don't work as expected in this
   Python version** — `f.get("severity") in Severity` raised `TypeError: unsupported operand
   type(s) for 'in': 'str' and 'EnumType'` inside the fake model's synthesis parser. Fixed by
   checking against `{s.value for s in Severity}` instead. Minor, but a reminder that `StrEnum`
   gives string *equality*, not automatic membership testing against the enum class with a plain
   `in`.

**Verification:** `uv run pytest -q` → 44 passed (34 from M1 + 10 new: `test_llm.py`,
`test_agents.py`, `test_orchestrator.py`), all against the fake model, no network/API key
required. Manually ran `uv run python -m scripts.demo_incident` for the all-healthy case (empty
report, zero LLM calls), a single-domain case (one specialist + one synthesis call), and a
two-domain case (two specialists + synthesis, non-empty `correlations`) — all three produced
coherent, schema-valid `IncidentReport` JSON. Real-provider verification (Anthropic/Gemini) is
deferred until the user's API key is sorted — tracked as a follow-up, not blocking M2 completion
since the model-agnostic design means it's a config change, not a code change, to verify later.

---

## 2026-10-03 — M3 scoping: confidence calibration method and runbook selection

**Decision:** Before implementing M3, resolved two open questions via `AskUserQuestion`:

1. **Confidence calibration: calibrated combination of model-reported confidence and a
   deterministic rule/safety signal**, over "pure model-reported" and "pure rule-derived."
   *Rejected: pure model-reported* — a hallucinated/miscalibrated number would flow straight
   into the FR-12 auto-remediate gate unchecked, exactly the "fail open" failure mode PRD §9
   exists to prevent. *Rejected: pure rule-derived* — defensible for the gate itself, but
   discards the model's own signal about root-cause reasoning quality, which the rule engine
   has no way to assess (rules only see threshold breaches, not causal reasoning).
2. **Which ≥3 remediation runbooks to implement — user explicitly delegated this to the
   assistant's judgment** ("what would you reccomend?" rather than picking from given options).
   Recommended and implemented: one runbook per domain — DB connection-pool recycle, CPU/mem
   worker-process restart, GPU memory-cache clear. Rationale for the recommendation: each is the
   narrowest-blast-radius, most reversible action per domain (no data deletion, no capacity
   reduction, no network/security config changes — consistent with `CLAUDE.md`'s open-questions
   default), giving a clean "one runbook per domain" portfolio story while clearing the PRD's
   floor of ≥3 total. *Not implemented*: CPU worker-pool scaling, which has a larger blast
   radius (provisions new infra) — deferred to a post-MVP roadmap item rather than added as a
   4th runbook, keeping the MVP allowlist small enough to audit end-to-end in an interview.
   **This is flagged explicitly because it was the assistant's recommendation, not a
   user-specified choice** — logged here per the project's standing decision-log practice so
   it's clear in hindsight whose call this was and why.

---

## 2026-10-03 — M3 implementation: routing gates, remediation runbooks, incident store

**Decision:** Implemented M3 per the approved plan — `tools/confidence.py` (calibration),
`agents/schemas.py` extensions (`RoutingDecision` enum, `selected_runbook_id` field),
`tools/remediation.py` (allowlist), `tools/routing.py` (deterministic gates),
`storage/incident_store.py` (SQLite), and `orchestrator.py` extensions (fail-closed error
handling, `route_and_remediate` node, `persist` param). Concrete choices:

1. **Calibration formula**: `calibrated_confidence = min(model_confidence, domain_count_cap)`
   where the cap is 0.65 for a single domain with `UNKNOWN`/`UNSAFE` automation safety, 0.95 for
   ≥2 corroborating domains all `SAFE`, and 0.8 otherwise (`tools/confidence.py::calibrate`). A
   stated judgment call, not derived from real incident data — documented inline and in the
   function's docstring rather than presented as empirically tuned.
2. **Routing (`tools/routing.py::route`) is a deterministic function, not an LLM call** — checks
   severity/confidence/safety in a fixed priority order (critical → escalate; confidence < 0.5 →
   escalate; confidence ≥ 0.8 + single-domain SAFE + runbook exists → auto-remediate; anything
   else → recommend). Cross-domain incidents always route to at least `RECOMMEND` even at high
   confidence/safety in this MVP, because a single runbook only ever targets one domain —
   auto-executing one domain's fix while leaving a corroborated second domain unaddressed is a
   scope mismatch this MVP doesn't attempt to solve.
3. **Allowlist is a static dict of dataclasses** (`tools/remediation.py::RUNBOOKS`), not Pydantic
   models — runbooks never cross an LLM structured-output boundary (the LLM only ever selects an
   id, never constructs one), so Pydantic's validation benefit doesn't apply here; a plain
   `@dataclass` holding a callable is simpler. Execution is simulated (logs what it would do,
   returns a canned success) since there's no real DB/host/GPU to act on in a portfolio project
   — stated explicitly in each runbook's docstring, not glossed over.
4. **Fail-closed error handling wraps both specialist and synthesis LLM calls** in
   `orchestrator.py`, producing sentinel `SpecialistFinding`/`SynthesisOutput` instances on
   exception. The sentinel's severity is forced to `CRITICAL` (not `HIGH`, as drafted in
   planning) — found during implementation that `HIGH` severity plus `UNKNOWN` safety could
   still combine with a high model-reported confidence from a *different* domain's valid
   finding to land outside the escalate path in some multi-domain combinations; `CRITICAL`
   unconditionally trips the "critical never auto-remediates" gate (checked first in `route()`)
   regardless of any other signal, which is the stronger and simpler fail-closed guarantee PRD
   §9 actually asks for.
5. **SQLite incident store** (`storage/incident_store.py`) uses two tables — `incidents` (full
   report as JSON + indexed columns) and `remediation_actions` (FR-16 audit log) — with a
   `UNIQUE(incident_id, runbook_id)` constraint backing the idempotency check
   (`was_already_executed`), consulted before `_route_and_remediate` calls a runbook's
   `execute()`.
6. **`run_incident()` gained a `persist: bool = True` parameter** so tests can opt out
   (`persist=False`) and never touch a real `infraguardian.db` file, while the demo script and
   any future API/CLI caller get persistence by default.
7. **Fake model's canned single-domain synthesis confidence raised from 0.75 to 0.9** — found
   during manual verification that the auto-remediate gate (threshold 0.8) could never actually
   fire end-to-end with the fake model's original canned confidence, which would have made the
   milestone's primary demo criterion ("same incident flows through routing gates to
   auto-remediate... correctly") undemonstrable without a real API key. Multi-domain confidence
   stays at 0.6, since M3 routing sends cross-domain incidents to RECOMMEND regardless of
   confidence anyway.

**Verification:** `uv run pytest -q` → 68 passed (44 from M1+M2 + 24 new:
`test_confidence.py`, `test_remediation.py`, `test_routing.py`, `test_incident_store.py`, plus 2
new `test_orchestrator.py` cases for the auto-remediate path and the fail-closed specialist
failure path). Manually ran `uv run python -m scripts.demo_incident db_connection_pool_exhaustion`
— confirmed `routing_decision: "auto_remediate"`, `selected_runbook_id:
"db.recycle_connection_pool"`, and a corresponding `remediation_actions` row in
`infraguardian.db`. Manually ran `uv run python -m scripts.demo_incident gpu_ecc_errors` —
confirmed `severity: "critical"` forces `routing_decision: "escalate"` even though the
underlying confidence (0.65) would otherwise be within range, demonstrating the "critical never
auto-remediates" gate holding regardless of other signals. Confirmed via `sqlite3
infraguardian.db ".schema"` that the two-table structure matches the design.


---

## 2026-10-03 — M4 scoping and implementation: API, CLI, autonomous execution window

**Context:** The user granted broad autonomy for this milestone ("give yourself all the
necessary permissions... always go with the recommended... I'll go through everything once")
for a ~1.5 hour window while away. All M4 decisions below were made without pausing for
confirmation, per that explicit delegation; irreversible/destructive actions were still avoided
or flagged per standing safety practice (none were needed for M4 — only additive file creation,
dependency installation, and one local `.db` artifact cleanup already covered by `.gitignore`).

**Decision 1 — FastAPI + uvicorn, added via `uv add fastapi "uvicorn[standard]"`.** PRD §15
names FastAPI explicitly as the stack choice, so this wasn't a real alternatives decision; the
only live choice was request schema shape (below).

**Decision 2 — `AnalyzeRequest.samples` uses a plain `Union[DBMetrics, CPUMemMetrics,
GPUMetrics]`, not a Pydantic discriminated union.** *Why:* Pydantic v2's discriminated-union
feature requires the discriminator field to be typed `Literal` on every member model, but
`domain: Domain` (a `StrEnum`) is not `Literal` — using it as a discriminator raised
`PydanticUserError: Model 'DBMetrics' needs field 'domain' to be of type 'Literal'` at import
time. *Alternative rejected:* retyping `domain` as `Literal["db"]` etc. on the metrics models
themselves — rejected because those models are shared with the rule engine and specialist
agents from M1/M2, and loosening/retyping a field used across the whole pipeline just to satisfy
one API-layer convenience isn't worth the blast radius. Plain `Union` with Pydantic v2's default
"smart mode" matching works correctly here because each domain's metrics model has a distinct,
non-overlapping set of required fields — verified empirically by parsing a raw DB-shaped dict
and confirming it resolves to `DBMetrics`, not misclassified as another domain.

**Decision 3 — `/analyze` accepts both `scenarios` (names) and `samples` (raw metric dicts) in
one request body**, matching the PRD's FR-17 ("run predefined scenarios") and FR-18 ("metric
inputs") as two input modes of the same endpoint rather than two endpoints. *Alternative
considered:* separate `/analyze/scenario` and `/analyze/samples` endpoints — rejected as
unnecessary surface area; a single endpoint with optional fields and a 400 on "neither provided"
covers both FRs with one code path and one response contract.

**Decision 4 — Built a dedicated `cli/main.py` (argparse, `list`/`analyze` subcommands) rather
than promoting `scripts/demo_incident.py` into the CLI.** *Why:* `demo_incident.py` was written
as an internal verification tool during M2/M3 and always prints full JSON; FR-17 asks for a
user-facing CLI that can list scenarios and choose output format, which argparse subcommands
give cleanly. *Alternative considered:* extending `demo_incident.py` with flags instead of
writing a new file — rejected because `demo_incident.py`'s docstring and existing behavior are
tied to M2/M3's manual-verification narrative ("Manual demo for M2/M3"), and conflating
"internal demo script" with "the FR-17 user-facing CLI" would blur that distinction for anyone
reading the repo later. Both files are kept, with `cli/main.py`'s docstring explicitly noting
the distinction.

**Decision 5 — API tests monkeypatch `api.server.run_incident` to force `persist=False`**,
rather than letting `/analyze` write to the real `infraguardian.db` during tests. *Why:*
consistent with the `persist: bool` parameter added in M3 specifically so tests never depend on
or pollute a real database file.

**Verification:** `uv run pytest -q` → 73 passed (68 from M1-M3 + 5 new in `test_api.py`:
`/health`, `/analyze` with scenario names, `/analyze` with raw samples, 400 for unknown
scenario, 400 for empty request). Manually ran `uv run python -m cli.main list` (prints all 12
scenario names) and `uv run python -m cli.main analyze <name> --no-persist` for both an
auto-remediate case (`db_connection_pool_exhaustion`) and the critical-escalate case
(`gpu_ecc_errors`) — both matched the M3 demo's known-correct routing. Also started a real
`uvicorn api.server:app` process and `curl`'d it directly (not just an in-process test client):
`/health` returned `{"status":"ok"}`, `POST /analyze` with a scenario name returned a full
schema-valid `IncidentReport` JSON body, an empty body returned `400`, and an unknown scenario
name returned `400` with the full list of valid names in the error detail.
`requirements.txt` regenerated via `uv export --no-hashes --no-dev -o requirements.txt` to
capture the new fastapi/uvicorn/starlette transitive dependencies.
