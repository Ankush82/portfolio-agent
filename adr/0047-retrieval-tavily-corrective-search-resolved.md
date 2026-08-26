# 0047 — Retrieval & Context: corrective-retrieval search provider resolved via Tavily

**Status:** Accepted — 2026-08-27
**Component:** Retrieval & Context (05)

## Context

ADR-0034 named the corrective-retrieval external search gap without picking a vendor: a general web search API, a financial-news-specific search API, or reusing whatever component 02 eventually picks for its own `SourceFetcher` — three real alternatives, left for the user to weigh (coverage vs. precision, a reliability signal `DefaultRetrievalEvaluator` could use, cost, and whether this shares a vendor with Data & Sources). The user has now created a real Tavily account (free tier, via Google SSO through a browser session this process drove, with the user completing the Google sign-in step and onboarding themselves) and supplied the resulting API key directly.

Tavily is purpose-built for exactly this shape of query — an LLM/agent-oriented search API that returns a ranked list of `{title, url, content, score}` results for a natural-language query, not a general-purpose web index. `CorrectiveRetriever.retrieve_externally`/`ExternalSearchProvider.search` (this component's own Protocol) take a `Query` (free text plus a context dict) and return `list[dict]` — Tavily's own result shape is already exactly that contract with no reshaping needed.

## Decision

**One new module, `src/tavily_client.py`, mirroring `src/llm.py`'s, `src/mem0_embedder.py`'s, and `src/alpha_vantage_client.py`'s existing per-vendor shape.** It is the only place in the codebase that reads `TAVILY_API_KEY` or talks to Tavily's `/search` endpoint. `get_api_key()` reads the key from the environment or a `.env` file at call time (same convention as every other vendor module in this project). `search_tavily(query, max_results=5)` makes one real HTTP POST and returns Tavily's own `results` list verbatim — deliberately not reshaped, since it already matches `ExternalSearchProvider.search`'s return contract.

**`TavilySearchProvider` added to `src/components/c05_retrieval_context.py`, alongside the untouched `PlaceholderExternalSearchProvider`.** `search(query, attempt)` calls `search_tavily(query.text)` and returns the result list as-is; `attempt` is accepted for Protocol conformance but unused — Tavily's API has no attempt-numbered variant, and `DefaultCorrectiveRetriever` already enforces the retry budget before `search()` is ever called, so nothing is lost by ignoring it here.

**`get_external_search_provider(placeholder=None)` resolves `DefaultCorrectiveRetriever`'s default `ExternalSearchProvider`**, the same key-gated selection shape `get_reason_fn` (ADR-0043) and `get_source_fetcher` (ADR-0046) already established: `TavilySearchProvider` when `TAVILY_API_KEY` is configured, `placeholder` (`PlaceholderExternalSearchProvider`) unchanged otherwise. `DefaultCorrectiveRetriever.__init__` now defaults `external_search_provider` to `get_external_search_provider()` instead of unconditionally `PlaceholderExternalSearchProvider()`. Constructing `TavilySearchProvider` never touches the network — only calling `.search()` does — so auto-resolving this at construction time is safe, the same reasoning ADR-0046 already gave for `AlphaVantageSourceFetcher`.

**Provenance tagging is unchanged.** `DefaultCorrectiveRetriever.retrieve_externally` already tags every result `UNTRUSTED` via `BoundaryGate.tag_provenance` before returning it, regardless of which `ExternalSearchProvider` produced it (ADR-0033's own design). Real Tavily results get exactly the same tagging real Alpha Vantage documents and every other external input in this project already get — nothing about this decision special-cases search results as more trustworthy than any other external content.

## Alternatives considered

- **A general web search API (Bing/Google-style) or a financial-news-specific search product, as ADR-0034 also named.** Moot once the user actually created a Tavily account specifically — this decision documents the account that exists. Tavily itself sits closer to the "purpose-built for agent/LLM search" end of ADR-0034's spectrum than a raw general web API, without being as narrow as a finance-only news product — a reasonable middle ground for a corrective-retrieval fallback that could, per `DefaultRetrievalEvaluator`'s own gate, fire on any insufficiently-covered query, not only financial-news-shaped ones.
- **Reusing whatever component 02 picks for `SourceFetcher` (Alpha Vantage, ADR-0046), rather than a second vendor.** Rejected for the same reason ADR-0034 itself already gave: Alpha Vantage fetches a *specific known* source (a ticker's quote/news/earnings) — it has no general-purpose "search the web for X" capability at all. The two capabilities are genuinely different; the user creating two separate accounts (Alpha Vantage, Tavily) reflects that they are two separate real gaps, not one gap solved twice.
- **Reshaping Tavily's result dicts into a project-specific shape before returning them from `TavilySearchProvider`.** Rejected: `ExternalSearchProvider.search`'s contract is already `list[dict]` with no fixed key schema imposed anywhere in this component, and `DefaultCorrectiveRetriever` only ever adds a `"provenance"` key on top — inventing an intermediate shape would be translation work with no caller that needs it.
- **Passing `attempt` through to Tavily as a parameter (e.g., varying `max_results` by attempt number).** Considered and rejected as unnecessary complexity: nothing in ADR-0012's corrective-retrieval design calls for escalating search breadth per attempt, and `max_results`'s real default (5) is already a reasonable single value for every attempt within budget.

## Consequences

- `DefaultCorrectiveRetriever()` (default constructor) now does a real Tavily search when `TAVILY_API_KEY` is configured — the first non-synthetic corrective-retrieval result this project has ever produced. Without the key, behavior is completely unchanged: `PlaceholderExternalSearchProvider`, honestly empty, exactly as ADR-0034 shipped it.
- `pyproject.toml` needs no new dependency — `requests` is already a direct dependency (ADR-0043).
- The existing test that constructed `DefaultCorrectiveRetriever()` bare and asserted placeholder-fetcher behavior needed a real fix, not a workaround: `tests/components/test_retrieval_context.py` gained the same `autouse` isolation fixture pattern `tests/components/test_data_sources.py` already established for `ALPHA_VANTAGE_API_KEY` — this file's own bare-constructed `test_corrective_retriever_logs_every_attempt_including_exhausted_ones` was silently issuing a real network call to Tavily before this fix (its `attempt=1` case is within a `max_attempts=1` budget, so the real provider was actually invoked), not merely at risk of a broken assertion.
- The key itself lives only in `.env` (gitignored, never committed) — nothing in this ADR, the code, or the tests hardcodes it.

## Related

- Partially resolves: [ADR-0034](0034-retrieval-corrective-external-search-provider-interim.md) — the vendor choice is made; the "one provider per concern or shared with component 02" question ADR-0034 raised is answered (separate, per the Alternatives above).
- Same shape as: [ADR-0043](0043-llm-provider-resolved-openrouter.md) (`get_reason_fn`), [ADR-0046](0046-data-sources-alpha-vantage-partial-resolution.md) (`get_source_fetcher`) — this is the third `get_<seam>()` key-gated selection function in this codebase, same pattern each time.
- Depends on: [ADR-0012](0012-retrieval-corrective-retrieval.md) (CRAG's bounded-retry shape), [ADR-0033](0033-retrieval-gate-evaluator-and-retriever-real-mechanism.md) (the real mechanism this search provider plugs into, including the provenance-tagging step this ADR doesn't change).
- Implemented by: `../src/tavily_client.py` (`get_api_key`, `search_tavily`, `MissingTavilyAPIKeyError`); `../src/components/c05_retrieval_context.py` (`TavilySearchProvider`, `get_external_search_provider`).
- Tested by: `../tests/test_tavily_client.py`, `../tests/components/test_retrieval_context.py` (`TavilySearchProvider`/`get_external_search_provider` section, plus the isolation fixture).
- Logged narratively in `../checkpoint.md`.
