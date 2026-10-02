# InfraGuardian — Milestones

Time-boxed breakdown of the 13-phase build plan in `CLAUDE.md`. Estimates assume solo,
part-time (portfolio-project pace) effort — adjust if you're working full-time on this.
Each milestone ends in something demoable, not just code written.

Total: ~5 milestones, ~3 working weeks (≈15 effort-days) at part-time pace.

| # | Milestone | Build-plan phases | Effort | Demo at end of milestone |
|---|---|---|---|---|
| M1 | **Walking skeleton: ingest → detect** | 1 Scaffolding, 2 Metric contracts & simulators, 3 Rule pre-screen | 2–3 days | Run a simulated scenario for one domain, see deterministic healthy/degraded classification — no LLM yet |
| M2 | **Agentic core: specialists + orchestration** | 4 Specialist agents, 5 LangGraph orchestrator & synthesis | 3–4 days | All 3 domains run, specialists invoked only on anomalies, unified incident report produced for a cross-domain scenario |
| M3 | **Safety & action layer** | 6 Routing & safety gates, 7 Remediation runbooks, 8 Incident store | 2–3 days | Same incident flows through routing gates to auto-remediate/recommend/escalate correctly, action logged to SQLite |
| M4 | **Interfaces** | 9 API & CLI | 1–2 days | `POST /analyze` and CLI both return a full incident report for a chosen scenario |
| M5 | **Proof & polish** | 10 Evaluation harness, 11 Observability, 12 Tests, 13 README | 4–5 days | 100+ fixture eval run with real accuracy/safety/latency/cost numbers, test suite green, README tells the portfolio story with those numbers |

## Why grouped this way

- **M1** has zero LLM dependency — it's pure plumbing and the rule engine. Good place to catch
  schema/contract mistakes cheaply before agents depend on them.
- **M2** is the highest-risk, highest-learning-curve milestone (LangGraph + concurrent
  specialists + synthesis quality). Budget slack here first if the timeline slips.
- **M3** is where every PRD §9 safety invariant actually gets enforced — treat its demo as a
  checklist against those invariants, not just "it runs."
- **M4** is intentionally small; interfaces are thin wrappers once M1–M3 exist.
- **M5** is not an afterthought even though it's last — PRD §17 says evaluation results, safety,
  and cost/latency numbers *are* the portfolio pitch. Don't compress this to make up time
  elsewhere.

## Sequencing notes

- Phases 10–12 (eval harness, observability, tests) can start as soon as M2 exists, in parallel
  with M3/M4, if you want to de-risk the "did I actually hit the accuracy targets" question
  early rather than finding out in week 3.
- If the timeline needs to compress, the safest cuts are: fewer scenarios per domain in M1 (PRD
  floor is 3/domain — don't go below that), fewer remediation runbooks in M3 (PRD floor is 3
  total), or deferring the CLI in M4 and shipping API-only. Do **not** cut M3's safety gates or
  M5's evaluation harness — those are the parts of the PRD that make this a portfolio piece
  instead of a toy.

## Tracking

Update the status column below as milestones complete. Keep entries one line.

| Milestone | Status |
|---|---|
| M1 | Done (2026-10-03) — metrics contracts, collectors, 12 scenarios, rule engine, 34 tests passing, demo script |
| M2 | Not started |
| M3 | Not started |
| M4 | Not started |
| M5 | Not started |
