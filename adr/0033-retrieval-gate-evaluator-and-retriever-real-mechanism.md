# 0033 — Retrieval & Context real mechanism: gate/evaluator heuristics, retriever wiring, context assembly

**Status:** Accepted — 2026-08-26
**Component:** Retrieval & Context (05)

## Context

ADR-0011 (adaptive gate, Self-RAG) and ADR-0012 (corrective retrieval, CRAG) settled the *shape* of Retrieval & Context's fig. 1 mechanism but explicitly left the mechanism itself open: ADR-0011's Consequences say "Whatever decides the gate (a model call, a heuristic, a confidence threshold) is not yet designed — this ADR settles the shape, not the mechanism," and ADR-0012 never specified what "sufficient?" actually checks. `src/components/c05_retrieval_context.py` had five `Protocol`/`Stub*` pairs and no real implementation of any of them. Component 02 (Data & Sources) and component 04 (Knowledge & Entity Model) both now expose real `Protocol` interfaces this component can call — 02 with a real `DefaultDataSources` implementation, 04 still whiteboard-only (`StubKnowledgeEntity` only) as of this pass.

Per this build wave's operating mode (loop.md step 2, as amended for this round): design and implement a real, defensible mechanism directly rather than stopping to ask, and document the decision here as an Accepted ADR. None of the decisions below required an external credential this project doesn't have, so none of them gets the ADR-0021/Proposed treatment — that's reserved for the corrective-retrieval external search seam, split out into ADR-0034.

## Decision

Four `Default*` classes, alongside the untouched `Stub*` classes, in `src/components/c05_retrieval_context.py`:

**`DefaultRetrievalGate.should_retrieve`** — rule-based keyword-overlap heuristic, no LLM. Extracts `query.text`'s significant keywords (lowercased, stopword-filtered, length ≥ 3). If there are none, retrieves unconditionally — an uninformative query gives the gate nothing to reason about, and skipping retrieval silently on that basis is the riskier failure direction. Otherwise computes what fraction of those keywords already appear (substring match) in `query.context["existing_content"]` (a string or list of strings the caller supplies — e.g. prior memory or conversation context). At or above a 0.6 coverage ratio, retrieval is skipped; below it, retrieval proceeds. 0.6 rather than 1.0 because real prose showing the same facts rarely repeats a query's exact wording.

**`DefaultRetriever.retrieve`** — calls `DataSources.discover_source`/`retrieve_source`/`ingest_source`/`track_source_reliability_metadata` (component 02) and `KnowledgeEntity.resolve_entity` (component 04), against whichever concrete adapter each is currently backed by (`DefaultDataSources` today; `StubKnowledgeEntity` until 04 has a real implementation — this class calls the Protocol, not a specific class, so it does not need to change when 04 becomes real). Candidate entity mentions come from `query.context["entity_mentions"]` when the caller supplies them, otherwise from a capitalized-word heuristic over `query.text` (no real NER exists anywhere in this project). Source discovery uses `query.context["source_criteria"]` when supplied (passed straight through to `discover_source`), otherwise one criteria dict per `SourceType` — component 02's `discover_source` only supports JSONB-containment filters on what it actually persists (type, source_id), with no full-text or semantic search, so "every registered source of a matching type" is the most honest query this component can make without inventing a search capability nothing else in the design has built. Each returned document carries `reliability` (from `track_source_reliability_metadata`) and `matched_entity_ids`, for the evaluator and downstream consumers to use. Every call is logged to a `retrieval_log` table via the injected `Infrastructure` (`DefaultInfrastructure` by default) — own bookkeeping, not required by the Protocol, for Observability & Governance's promised retrieval-rate signal.

**`DefaultRetrievalEvaluator.is_sufficient`** — rule-based, four real signals, all failing closed:
- **document count**: at least 1 result.
- **reliability**: average `SourceMetadata.reliability` (attached by `DefaultRetriever`, sourced from component 02) at or above 0.5 — strictly between component 02's synthetic-document score (0.3) and real-document score (1.0), so a batch of only placeholder-fetched content (ADR-0027 still Proposed) never counts as sufficient on its own. This is an intended consequence, not an oversight: with no real `SourceFetcher` wired in yet, retrieval is honestly insufficient today, which is exactly what should drive `CorrectiveRetriever`'s fallback path.
- **coverage**: fraction of `query.text`'s significant keywords found in the results' combined content, at or above 0.5 (same helper as the gate, applied to a different corpus). Skipped, not failed, when the query has no significant keywords to check.
- **freshness**: the single freshest result's age (parsed from `fetched_at` using component 02's own timestamp format) at or below 180 days — generous for filings/reports that stay relevant for months, while still ruling out stale news. No result with a parseable timestamp fails this signal outright, rather than treating a missing/malformed clock reading as neutral.

**`DefaultContextBuilder.construct`** — structural assembly of `documents` into a `ContextPack`, `sufficient` set from a real `RetrievalEvaluator.is_sufficient(documents, query)` call when the caller supplies `query` (see "Alternatives considered" for the one flagged signature deviation this required), falling back to `bool(documents)` when it doesn't.

## Alternatives considered

- **LLM-based gate/evaluator now.** Rejected for this pass, per the task's own instruction: a heuristic is a legitimate, honest first version, not a stand-in waiting to be replaced before it can be trusted. An LLM-based version remains a real future improvement, not a currently-missing requirement.
- **`ContextBuilder.construct(documents)` left at its bare Protocol signature, with `sufficient` always structural (`bool(documents)`).** Rejected: this would not set `sufficient` "from the real evaluator result" as required, only from document count, discarding the evaluator's reliability/coverage/freshness signal entirely whenever a query is actually available. Instead, `construct` gained an additive `query: Query | None = None` keyword parameter — a flagged, deliberate deviation from the Protocol's exact signature (loop.md step 6), backward-compatible with every existing bare-signature call site.
- **`DefaultRetriever` discovering sources by embedding/semantic similarity to the query.** Rejected: no embedding infrastructure exists anywhere in this project yet (Memory's own ADR-0028 flags the identical embedding-provider gap for component 06), and component 02's `discover_source` has no content-search capability to call into even if one existed. Type-based discovery is what the actually-built interfaces support today.

## Consequences

- With only `PlaceholderSourceFetcher` wired in (ADR-0027 still Proposed), `DefaultRetrievalEvaluator.is_sufficient` will honestly return `False` for essentially every real query today, since no source can clear the 0.5 reliability threshold yet. This is the correct, intended behavior given the current state of component 02, not a bug in this component — it is what should route control to `CorrectiveRetriever`, whose own placeholder (ADR-0034) then honestly reports "no useful evidence found" rather than either component fabricating success.
- `DefaultRetriever`'s type-based discovery returns every registered source of a matching type, not sources actually relevant to the query's content — a real, named scoping limit inherited from component 02's interface, not invented here. A future semantic-search capability in component 02 (or a dedicated search index) would let this component filter more precisely without changing its own shape.
- `DefaultContextBuilder.construct`'s additive `query` parameter means a caller that doesn't have (or doesn't pass) the originating `Query` gets a weaker, count-only sufficiency signal. Fig. 1's actual flow (gate → retrieve → evaluate → construct, same query throughout) always has the query available, so this only matters for a caller that deliberately calls `construct` in isolation.
- The 0.6/0.5/0.5/180-day thresholds are all named constants in the module (`_GATE_COVERAGE_THRESHOLD`, `_MIN_AVERAGE_RELIABILITY`, `_MIN_COVERAGE_RATIO`, `_MAX_STALENESS_DAYS`) — tunable without touching control flow if real production data later suggests different values.

## Related

- Implements the mechanism ADR-0011 and ADR-0012 explicitly left open.
- Depends on: [ADR-0026](0026-data-sources-real-mechanism.md) (Data & Sources real mechanism — `DefaultRetriever`'s only concrete dependency today), [ADR-0027](0027-data-sources-fetch-provider-interim.md) (why reliability is honestly capped at 0.3 today).
- Split from: [ADR-0034](0034-retrieval-corrective-external-search-provider-interim.md) — the corrective-retrieval external search seam, which does hit a genuine external-credential gap and is Proposed, not Accepted, unlike this ADR.
- Implemented by: `../src/components/c05_retrieval_context.py`, `DefaultRetrievalGate`, `DefaultRetriever`, `DefaultRetrievalEvaluator`, `DefaultContextBuilder`.
- Logged narratively in `../checkpoint.md`.
