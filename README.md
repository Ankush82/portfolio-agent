# Portfolio Agent

An agentic, portfolio-aware financial intelligence system: watches sources, resolves them into a shared knowledge model, reasons about what changed, checks its own claims against evidence, decides what's worth telling a user, and learns from the outcome. 18 components, all real, none faked.

## Status

**All 18 components have real implementations** — not stubs, not mocks pretending to be logic. 422 tests pass; 13 skip cleanly where a live Postgres/Redis isn't running (see [Running it](#running-it)).

**6 real decisions are still open**, each a genuine external-credential or vendor gap this project's own process (see [How this was built](#how-this-was-built)) refuses to decide by fiat. Every one ships with a working, honestly-labeled placeholder behind an injectable interface, so nothing downstream is blocked — but none of these are safe to treat as production behavior until resolved:

| ADR | What's waiting | Component |
|---|---|---|
| [0021](adr/0021-agent-runtime-llm-provider-interim.md) | Which LLM backs actual reasoning — currently a non-cognitive placeholder | Agent Runtime, and by extension Analysis & Reasoning ([0037](adr/0037-analysis-reasoning-real-mechanism-and-reasoning-seam.md)) |
| [0023](adr/0023-user-portfolio-broker-api-choice-interim.md) | Which broker/aggregator API for holdings and transactions | User & Portfolio |
| [0027](adr/0027-data-sources-fetch-provider-interim.md) | Which market/filing/news provider(s) | Data & Sources |
| [0028](adr/0028-memory-mem0-llm-embedding-provider-interim.md) | Mem0's LLM/embedding provider (its one real differentiator over this project's own Infrastructure-backed logic) | Memory |
| [0034](adr/0034-retrieval-corrective-external-search-provider-interim.md) | Which external search API for corrective retrieval | Retrieval & Context |
| [0040](adr/0040-interaction-notification-delivery-channel-interim.md) | Which delivery channel (email/SMS/push) | Interaction & Notification |

Resolving one of these is a normal ADR decision — read the linked file, pick an option or bring your own, and the placeholder swaps out behind the same interface without touching any caller.

## Architecture

18 components in a pipeline, wrapped by 4 cross-cutting concerns that apply to all of them:

```
User & Portfolio → Agent Runtime → Data & Sources → Data Processing & Quality
→ Knowledge & Entity Model → { Event & Observation, Retrieval & Context } → Memory
→ Analysis & Reasoning → Evidence & Verification → Decision & Policy
→ Interaction & Notification → Learning & Evaluation → (loops back to Memory)

wrapped throughout by: Reliability & Resilience · Observability & Governance
                        · Security & Privacy · System Infrastructure
```

Full component-by-component design, with diagrams, is in the published design series (linked from `checkpoint.md`). `Thoughts.md` is where the capability model started; `portfolio_ai_three_literature_reviews.md` and `self-evolving-harness-literature-review.md` are the research it's grounded in.

## Repo map

```
src/
  components/c01…c14_*.py     18 components: Protocol (port) + Stub* (test double) + Default* (real)
  cross_cutting/               Reliability, Observability, Security (components 15–17)
  infrastructure.py            The Infrastructure port (component 18)
  infrastructure_postgres.py   Its real Postgres/Redis implementation
  run_trace.py                 A static wiring demo (Stub* classes) — proves the blueprint's shape
tests/                         420 real tests, one file per component
adr/                           42 Architecture Decision Records — every real decision, with
                                context, alternatives considered, and consequences. Read adr/README.md first.
checkpoint.md                  The narrative log of this entire build, in order
loop.md                        The process every component's real implementation followed
orchestration.md               How this was parallelized and reviewed
docker-compose.yml             Local Postgres + Redis for real infrastructure tests
```

## How this was built

Every component followed the same loop (`loop.md`): read the design and every relevant ADR first, implement to match it exactly, test the real failure modes — not just the happy path — and report. Wherever a real tradeoff existed, it got decided and documented as an ADR with alternatives considered, not buried in a comment. Wherever a decision genuinely needed something this process doesn't have — a live API key, a chosen LLM provider, real money — it got a `Proposed` ADR and an honest placeholder instead of a silent guess (the table above).

Implementation was parallelized across subagents, each briefed with the specific files, ADRs, and constraints for one component, then reviewed against a fixed checklist (`orchestration.md`) before being merged: does it match its own ADRs, does it match its Protocol's signature, does the full test suite still pass, does it wire in the four cross-cutting concerns rather than bypass them. Standing engineering rules (single clear intent per change, no dead code, no TODOs, descriptive naming over comments) applied throughout, listed in `checkpoint.md`.

## Running it

```bash
uv sync --extra dev          # install dependencies
uv run --python 3.11 pytest tests/ -v    # 422 pass, 13 skip without a live DB

docker-compose up -d         # bring up local Postgres + Redis
uv run --python 3.11 pytest tests/ -v    # same suite, now with real DB coverage too

PYTHONPATH=src uv run --python 3.11 python src/run_trace.py   # static wiring demo, writes trace.log
```

No LLM provider is configured — see ADR-0021. Nothing in this repo calls a real model, sends real money, or reaches a real broker/market-data/news/search/delivery API; every one of those is the honestly-labeled placeholder named in the table above until you resolve its ADR.
