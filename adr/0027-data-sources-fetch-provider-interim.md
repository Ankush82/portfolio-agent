# 0027 — Data & Sources fetch provider interim: injectable, non-fetching placeholder

**Status:** Proposed — 2026-08-26
**Component:** Data & Sources (02)

## Context

Data & Sources' real mechanism — persistence, provenance/timestamp/reliability tracking, the `SourceFetcher` seam's shape — is now implemented for real (ADR-0026, `DefaultDataSources` in `src/components/c02_data_sources.py`). `ingest_source` is the one capability behind that mechanism that cannot be made fully real without an actual external credential: fetching a real news article, filing, earnings transcript, presentation deck, market-data quote, or external dataset requires calling an actual third-party API, none of which this project has ever chosen, requested credentials for, or listed as a dependency in `pyproject.toml`.

This is the same shape of gap ADR-0021 already named for Agent Runtime's `reason_fn`, and this ADR follows that one's exact pattern per this project's operating instructions: name real options with honest tradeoffs, do not pick one, ship an injectable placeholder so the rest of the system builds and tests around the gap instead of stalling on it.

`SourceType` spans several genuinely different kinds of external data, so "an LLM provider" (one deferred choice, one kind of dependency) is not quite the right shape here — this is closer to a deferred choice *per source type*, since a market-data API, a filings/regulatory feed, and a news API are different products from different vendors with different data models, even though they all sit behind the same `SourceFetcher.fetch()` seam (ADR-0026).

## Decision

Ship `PlaceholderSourceFetcher` (`src/components/c02_data_sources.py`) as the default behind the injectable `SourceFetcher` interface `DefaultDataSources.ingest_source` calls through. It does no real fetching for any `SourceType`: it returns a `SourceDocument` whose `content` is the module-level `PLACEHOLDER_FETCH_MARKER` constant — an unmistakable marker string, deliberately not empty bytes, which could otherwise be misread as a legitimate empty response from a real source — and whose `fetched_at` is a real timestamp of when the placeholder ran. `DefaultDataSources.track_source_reliability_metadata` scores any document carrying that marker as low-reliability (`_RELIABILITY_SYNTHETIC_DOCUMENT = 0.3`, versus `_RELIABILITY_REAL_DOCUMENT = 1.0` for a genuinely fetched document), so the fact that no real fetch has happened yet is visible in the data this component produces, not just in a code comment.

This makes `DefaultDataSources` genuinely runnable and testable end to end today (`tests/components/test_data_sources.py`) without any external credential, and later swappable for real fetch behavior — per source type, or uniformly — without changing `DefaultDataSources`'s own code, only the `source_fetcher` passed into its constructor.

## Alternatives considered, by source type

- **`SourceType.MARKET_DATA`.**
  - *A dedicated market-data API* (e.g. IEX Cloud, Polygon.io, Alpha Vantage). Real considerations: per-call or subscription pricing that scales with symbol/quote volume, rate limits that constrain how often this component can poll, and data licensing terms (redistribution/display rights vary by vendor and matter if fetched data is ever shown back to the end user, not just reasoned over internally).
  - *A brokerage's own market-data feed*, if User & Portfolio (01)'s eventual broker connector (ADR-0022) already carries one. Real consideration: would tie Data & Sources' market-data ingestion to whichever broker connector gets built, coupling two components that are architecturally independent today — a real cost even if it saves a second vendor relationship.
- **`SourceType.FILING` / `EARNINGS` / `PRESENTATION` / `REPORT`.**
  - *SEC EDGAR's full-text search / submissions API* (free, official, U.S. filings only). Real considerations: zero licensing cost and highest trust for U.S. regulatory filings specifically, but covers only SEC-registered filers — earnings call transcripts, investor presentations, and non-U.S. filings would still need a separate source, and EDGAR's own rate-limit and fair-access policies would need to be respected.
  - *A commercial filings/transcripts aggregator* (e.g. sec-api.io, AlphaSense-style providers). Real considerations: broader coverage (transcripts, presentations, non-U.S. filings in one API) at real subscription cost, and a dependency on a single commercial vendor's uptime and parsing quality rather than the primary regulatory source.
- **`SourceType.NEWS`.**
  - *A dedicated news API* (e.g. NewsAPI, Benzinga News API). Real considerations: per-call/subscription pricing, coverage breadth versus financial-news specificity (Benzinga-style vendors specialize in market-moving news; general news APIs are broader but noisier for this project's purpose), and how much of ADR-0003's "untrusted by default" posture should be treated as extra-cautious for a source whose entire business model is unverified, fast-moving claims.
  - *GDELT* (free, global, event-coded). Real consideration: no per-call cost and very broad coverage, but event-coding rather than full-article text means Data Processing & Quality (03) downstream would receive structured events, not raw documents — a genuinely different shape of `SourceDocument.content` than this component's other source types produce, which is a real design implication, not just a vendor swap.
- **`SourceType.EXTERNAL_DATASET`.** No single vendor category applies — what counts as an "external dataset" depends entirely on what a future user of this component needs (a specific research dataset, a specific data provider's bulk export, etc.). Naming a specific vendor here would be inventing a need nothing in this project has stated yet; this type is flagged as needing its own scoping conversation once a concrete use case exists, not resolved by a placeholder guess.

None of these is picked here. The real considerations — per-vendor cost, rate limits, licensing/redistribution terms, coverage breadth versus specificity, and whether `SourceFetcher` should dispatch to one vendor per `SourceType` or several — are named so the user has them, not resolved on the user's behalf.

## Consequences

- Every `SourceDocument` `DefaultDataSources.ingest_source` (and `update_source`, which re-ingests) produces today is synthetic content carrying `PLACEHOLDER_FETCH_MARKER`, for every `SourceType`. Anything downstream — Data Processing & Quality (03) especially — that depends on Data & Sources actually having fetched real content is not yet safe to build on top of this implementation.
- `PlaceholderSourceFetcher.fetch`'s "always return the same marker, always report a real timestamp" behavior is exactly what makes `DefaultDataSources` genuinely testable today — those tests exercise the persistence/provenance/timestamp/reliability mechanism (ADR-0026), not any real fetch behavior, and should not be read as validating that this component retrieves real data.
- `track_source_reliability_metadata`'s synthetic-vs-real scoring means every source in this system will report `_RELIABILITY_SYNTHETIC_DOCUMENT` (0.3), never `_RELIABILITY_REAL_DOCUMENT` (1.0), until at least one real `SourceFetcher` implementation exists for at least one `SourceType` — an honest, visible consequence of this gap rather than a hidden one.
- Once a provider (or providers) is chosen per source type, the fix is narrowly scoped by ADR-0026's design: implement `SourceFetcher.fetch` for real (dispatching on `source.type` internally, if different source types end up needing different vendors) and pass it into `DefaultDataSources(source_fetcher=...)` instead of relying on the `PlaceholderSourceFetcher` default. No method on `DefaultDataSources` needs to change shape for that swap.
- This is scoped to Data & Sources' `ingest_source` fetch step only. It says nothing about whether other components' eventual external calls (e.g. a future User & Portfolio broker connector, ADR-0022/ADR-0023) should share a vendor with any of the choices named above — that is a separate decision, or decisions, not settled here.

## Related

- Depends on: [ADR-0026](0026-data-sources-real-mechanism.md) (the `SourceFetcher` interface shape and reliability-scoring mechanism this ADR's placeholder plugs into).
- Extends the same "ask, don't decide" pattern as: [ADR-0021](0021-agent-runtime-llm-provider-interim.md) (`placeholder_reason_fn` — this ADR's direct template, same tone, same shape), [ADR-0010](0010-memory-technology-purpose-built-layer.md) (Mem0 vs. Supermemory, left open), [ADR-0020](0020-security-authorize-interim-default.md), [ADR-0023](0023-user-portfolio-broker-api-choice-interim.md) (a sibling external-credential gap raised the same round, for User & Portfolio's broker connector).
- Implemented by: `../src/components/c02_data_sources.py`, `PlaceholderSourceFetcher` and `SourceFetcher`.
- Open question originates in: this task's own instruction that `ingest_source` "genuinely needs a live news/filing/market-data API in reality" — named as the external-credential exception up front, not discovered mid-implementation.
- Logged narratively in `../checkpoint.md`.
