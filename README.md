# Portfolio Agent

An agentic, portfolio-aware financial intelligence system: watches sources, resolves them into a shared knowledge model, reasons about what changed, checks its own claims against evidence, decides what's worth telling a user, and learns from the outcome. 18 components, all real, none faked.

## Status

**All 18 components have real implementations** — not stubs, not mocks pretending to be logic. 501 tests pass; 13 skip cleanly where a live Postgres/Redis isn't running (see [Running it](#running-it)).

**3 real decisions are still open**, each a genuine external-credential or vendor-account gap this project's own process (see [How this was built](#how-this-was-built)) refuses to decide or set up by fiat — creating a real account needs a real inbox for verification and a real person agreeing to that vendor's terms, neither of which this process can do on your behalf. Every one ships with a working, honestly-labeled placeholder behind an injectable interface, so nothing downstream is blocked — but none of these are safe to treat as production behavior until resolved:

| ADR | What's waiting | Component |
|---|---|---|
| [0023](adr/0023-user-portfolio-broker-api-choice-interim.md) | Which broker/aggregator API for holdings and transactions (a real, non-broker manual-entry path exists too — [0044](adr/0044-user-portfolio-manual-stock-entry.md)) | User & Portfolio |
| [0034](adr/0034-retrieval-corrective-external-search-provider-interim.md) | Which external search API for corrective retrieval — recommended: Tavily, free tier, no card | Retrieval & Context |
| [0040](adr/0040-interaction-notification-delivery-channel-interim.md) | Which delivery channel (email/SMS/push) — recommended: Resend for email; `User` also needs a contact field added first | Interaction & Notification |

Resolving one of these is a normal ADR decision — sign up for the free tier, read the linked file, drop the resulting key into `.env`, and the placeholder swaps out behind the same interface without touching any caller (the same pattern `OPENROUTER_API_KEY` already uses, [ADR-0043](adr/0043-llm-provider-resolved-openrouter.md)).

Two other vendor gaps are already resolved. Memory's ([ADR-0028](adr/0028-memory-mem0-llm-embedding-provider-interim.md)) needed no account at all — `Mem0EntityLinker` uses a free, local, offline embedding model ([ADR-0045](adr/0045-memory-mem0-embedding-resolved-fastembed.md)). Data & Sources' ([ADR-0027](adr/0027-data-sources-fetch-provider-interim.md)) is partially resolved: a real Alpha Vantage account now backs `MARKET_DATA`/`NEWS`/`EARNINGS` fetching ([ADR-0046](adr/0046-data-sources-alpha-vantage-partial-resolution.md)); `FILING`/`REPORT`/`PRESENTATION`/`EXTERNAL_DATASET` remain open.

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
  llm.py                        The one real LLM reasoning backend (OpenRouter), ADR-0043
  mem0_embedder.py              Real local embedding provider (mem0ai + fastembed), ADR-0045
  alpha_vantage_client.py       Real market-data/news/earnings provider, ADR-0046
tests/                         501 real tests, one file per component
adr/                           46 Architecture Decision Records — every real decision, with
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
uv run --python 3.11 pytest tests/ -v    # 501 pass, 13 skip without a live DB

docker-compose up -d         # bring up local Postgres + Redis
uv run --python 3.11 pytest tests/ -v    # same suite, now with real DB coverage too

PYTHONPATH=src uv run --python 3.11 python src/run_trace.py   # static wiring demo, writes trace.log
```

Set `OPENROUTER_API_KEY` (environment variable or a `.env` file at the repo root) to turn on real reasoning for Agent Runtime and Analysis & Reasoning — see [ADR-0043](adr/0043-llm-provider-resolved-openrouter.md). Set `ALPHA_VANTAGE_API_KEY` the same way to turn on real market-data/news/earnings fetching in Data & Sources — see [ADR-0046](adr/0046-data-sources-alpha-vantage-partial-resolution.md). Without either, both components fall back to their honest, non-cognitive/non-fetching placeholders, unchanged. Nothing else in this repo calls a real model, sends real money, or reaches a real broker/search/delivery API, or fetches real filings/reports/presentations/datasets; every one of those is the honestly-labeled placeholder named in the table above until you resolve its ADR.
