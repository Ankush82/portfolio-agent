"""Retrieval & Context (component 05) — context engineering: what to
hand the reasoner, and how much of it.

Design: Retrieval & Evidence Design, fig. 1 (retrieval path)
Decisions: ADR-0011 (adaptive retrieval, Self-RAG), ADR-0012
(corrective retrieval, CRAG), ADR-0033 (real mechanism: gate/evaluator
heuristics, retriever wiring to components 02/04, context assembly),
ADR-0034 (external search provider interim — placeholder, partially
superseded by ADR-0047: real Tavily-backed search, see
TavilySearchProvider below)

ADR-0011 and ADR-0012 settled the *shape* of the adaptive gate and the
corrective-retrieval loop but explicitly left the mechanism open
("Whatever decides the gate ... is not yet designed — this ADR settles
the shape, not the mechanism"). ADR-0033 closes that gap with real,
heuristic rules — see each `Default*` class's docstring for the exact
rule, and ADR-0033 for why it's a defensible first version rather than
a stand-in for a future LLM-based gate/evaluator.
"""

import re
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from components.c02_data_sources import DataSources, DefaultDataSources, Source, SourceType
from components.c04_knowledge_entity import Entity, KnowledgeEntity, StubKnowledgeEntity
from cross_cutting.observability import traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure
from tavily_client import get_api_key as get_tavily_api_key, search_tavily


@dataclass
class Query:
    text: str
    context: dict


@dataclass
class ContextPack:
    documents: list[dict]
    sufficient: bool


class RetrievalGate(Protocol):
    def should_retrieve(self, query: Query) -> bool:
        """Fig. 1's adaptive gate (ADR-0011). Existing context/memory
        is used directly when this is False."""
        ...


class StubRetrievalGate:
    """Structural implementation of RetrievalGate. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def should_retrieve(self, query: Query) -> bool:
        with traced("StubRetrievalGate.should_retrieve"):
            return True


# --- Shared keyword-overlap heuristic (ADR-0033) ----------------------------
#
# Both the adaptive gate and the sufficiency evaluator need the same basic
# operation: how much of a query's meaningful vocabulary is already present
# in some body of text? One shared helper, two different corpora (existing
# context for the gate, retrieved document content for the evaluator).

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "of", "in", "on", "at", "for", "to", "and", "or", "but", "with",
        "about", "as", "by", "from", "this", "that", "these", "those",
        "what", "which", "who", "whom", "how", "does", "do", "did", "has",
        "have", "had", "will", "would", "should", "could", "can", "it",
        "its", "into", "than", "then", "so", "not", "no", "if",
    }
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
_MIN_KEYWORD_LENGTH = 3


def _significant_keywords(text: str) -> set[str]:
    """Lowercased, stopword-filtered, length-filtered token set. No NLP
    model involved on purpose — this is the honest, first-pass version
    of "what does this query actually care about," not a stand-in for
    a future embedding-based comparison."""
    tokens = (match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text))
    return {token for token in tokens if len(token) >= _MIN_KEYWORD_LENGTH and token not in _STOPWORDS}


def _coverage_ratio(keywords: set[str], corpus_text: str) -> float:
    """Fraction of `keywords` that appear as a substring somewhere in
    `corpus_text` (lowercased). Substring rather than exact token match
    so that e.g. "revenue" still counts against "revenues" in the
    corpus — a small, deliberate leniency, not a full stemmer."""
    if not keywords:
        return 0.0
    corpus_lower = corpus_text.lower()
    covered = sum(1 for keyword in keywords if keyword in corpus_lower)
    return covered / len(keywords)


def _join_existing_content(existing_content: object) -> str:
    if isinstance(existing_content, str):
        return existing_content
    if isinstance(existing_content, list):
        return " ".join(str(item) for item in existing_content)
    return ""


_GATE_COVERAGE_THRESHOLD = 0.6


class DefaultRetrievalGate:
    """Real implementation of RetrievalGate (ADR-0011's shape, ADR-0033's
    mechanism): a rule-based heuristic, not an LLM call.

    Rule: extract `query.text`'s significant keywords (see
    `_significant_keywords`). If there are none, there's nothing to
    compare against — retrieve, since skipping silently on an
    uninformative query is the riskier failure direction. Otherwise,
    compute what fraction of those keywords already appear in
    `query.context["existing_content"]` (a list of strings or a single
    string the caller supplies — e.g. prior memory or conversation
    context). If that coverage ratio is at or above
    `_GATE_COVERAGE_THRESHOLD` (0.6 — a supermajority, not exact
    identity, since real prose rarely repeats a query's wording
    verbatim even when it already contains the answer), retrieval is
    skipped; otherwise it proceeds. `existing_content` absent or empty
    means zero coverage, i.e. always retrieve."""

    def should_retrieve(self, query: Query) -> bool:
        with traced("DefaultRetrievalGate.should_retrieve"):
            keywords = _significant_keywords(query.text)
            if not keywords:
                return True
            corpus = _join_existing_content(query.context.get("existing_content"))
            ratio = _coverage_ratio(keywords, corpus)
            return ratio < _GATE_COVERAGE_THRESHOLD


class Retriever(Protocol):
    def retrieve(self, query: Query) -> list[dict]:
        """Calls Source System (02) and Knowledge & Entity Model (04)."""
        ...


class StubRetriever:
    """Structural implementation of Retriever. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def retrieve(self, query: Query) -> list[dict]:
        with traced("StubRetriever.retrieve"):
            return []


_RETRIEVAL_LOG_TABLE = "retrieval_log"
_CAPITALIZED_WORD_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")


def _candidate_entity_mentions(text: str) -> list[str]:
    """No real NER exists anywhere in this project (component 04 is
    still whiteboard-only). Capitalized-word runs are a cheap, honest
    stand-in for "things that look like they might name an entity" —
    good enough to give KnowledgeEntity.resolve_entity something to
    try, not a claim of real entity extraction. Order-preserving,
    de-duplicated."""
    seen: dict[str, None] = {}
    for match in _CAPITALIZED_WORD_PATTERN.finditer(text):
        seen.setdefault(match.group(0), None)
    return list(seen.keys())


def _decode_document_content(content: bytes) -> str:
    """Documents (component 02) carry raw bytes; ContextBuilder and
    the evaluator both work on text. UTF-8 first since that's what
    every real source format (news, filings, reports) uses; hex
    fallback for anything genuinely binary rather than raising and
    dropping the document."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.hex()


class DefaultRetriever:
    """Real implementation of Retriever (ADR-0033): calls into
    component 02 (`DataSources`) and component 04 (`KnowledgeEntity`)
    for real, against whichever concrete adapter each is currently
    backed by — `DefaultDataSources` today, and `StubKnowledgeEntity`
    until component 04 gets a real implementation of its own (this
    class does not block on that; it calls the Protocol, not a
    specific class).

    retrieve(query) does two things:
      1. Resolves candidate entity mentions from `query.context
         ["entity_mentions"]` if the caller supplies them, otherwise
         derives candidates from `query.text` via
         `_candidate_entity_mentions` — each is passed to
         `KnowledgeEntity.resolve_entity`; resolved entities are
         attached to every returned document as `matched_entity_ids`
         so downstream consumers (the evaluator, Analysis & Reasoning)
         can see what the retrieval was actually anchored to.
      2. Discovers sources via `DataSources.discover_source`, using
         `query.context["source_criteria"]` when the caller supplies
         it (a single criteria dict or a list of them, passed straight
         through to `discover_source` — component 02's own filter
         shape), or, when absent, one criteria dict per `SourceType` —
         DataSources has no full-text/semantic search of its own (its
         `discover_source` only supports the JSONB-containment filters
         component 02 actually persists), so "every registered source
         of a matching type" is the most honest thing this component
         can ask for without inventing a search capability that
         doesn't exist anywhere else in the design. For each
         discovered `Source`, an already-ingested document is reused
         (`retrieve_source`) or fetched fresh (`ingest_source`, which
         is itself real-or-placeholder per ADR-0027 — this class does
         not care which); `track_source_reliability_metadata` attaches
         a real reliability signal to each returned document for the
         evaluator to use.

    Every call is logged to `_RETRIEVAL_LOG_TABLE` via the injected
    `Infrastructure` (own bookkeeping, `DefaultInfrastructure`-backed
    by default) — query text, resolved entity ids, and document count,
    for Observability & Governance's promised retrieval-rate signal.
    """

    def __init__(
        self,
        data_sources: DataSources | None = None,
        knowledge_entity: KnowledgeEntity | None = None,
        infrastructure: Infrastructure | None = None,
    ) -> None:
        self._data_sources = data_sources or DefaultDataSources()
        self._knowledge_entity = knowledge_entity or StubKnowledgeEntity()
        self._infrastructure = infrastructure or DefaultInfrastructure()

    def _resolve_entities(self, query: Query) -> list[Entity]:
        mentions = query.context.get("entity_mentions") or _candidate_entity_mentions(query.text)
        resolved: list[Entity] = []
        for mention in mentions:
            entity = self._knowledge_entity.resolve_entity(mention)
            if entity is not None:
                resolved.append(entity)
        return resolved

    def _criteria_list(self, query: Query) -> list[dict]:
        source_criteria = query.context.get("source_criteria")
        if source_criteria is None:
            return [{"type": source_type.name} for source_type in SourceType]
        if isinstance(source_criteria, dict):
            return [source_criteria]
        return list(source_criteria)

    def _document_for(self, source: Source, matched_entity_ids: list[str]) -> dict:
        document = self._data_sources.retrieve_source(source.id)
        if document is None:
            document = self._data_sources.ingest_source(source)
        metadata = self._data_sources.track_source_reliability_metadata(source)
        return {
            "source_id": document.source_id,
            "source_type": source.type.name,
            "content": _decode_document_content(document.content),
            "fetched_at": document.fetched_at,
            "reliability": metadata.reliability,
            "matched_entity_ids": matched_entity_ids,
        }

    def retrieve(self, query: Query) -> list[dict]:
        with traced("DefaultRetriever.retrieve"):
            resolved_entities = self._resolve_entities(query)
            matched_entity_ids = [entity.id for entity in resolved_entities]

            documents: list[dict] = []
            seen_source_ids: set[str] = set()
            for criteria in self._criteria_list(query):
                for source in self._data_sources.discover_source(criteria):
                    if source.id in seen_source_ids:
                        continue
                    seen_source_ids.add(source.id)
                    documents.append(self._document_for(source, matched_entity_ids))

            self._infrastructure.store(
                _RETRIEVAL_LOG_TABLE,
                {
                    "id": f"retrieval::{uuid.uuid4().hex}",
                    "query_text": query.text,
                    "resolved_entity_ids": matched_entity_ids,
                    "document_count": len(documents),
                    "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )
            return documents


class RetrievalEvaluator(Protocol):
    def is_sufficient(self, results: list[dict], query: Query) -> bool:
        """Fig. 1's 'sufficient?' gate (ADR-0012)."""
        ...


class StubRetrievalEvaluator:
    """Structural implementation of RetrievalEvaluator. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def is_sufficient(self, results: list[dict], query: Query) -> bool:
        with traced("StubRetrievalEvaluator.is_sufficient"):
            return True


_MIN_DOCUMENT_COUNT = 1
_MIN_AVERAGE_RELIABILITY = 0.5
_MIN_COVERAGE_RATIO = 0.5
_MAX_STALENESS_DAYS = 180.0
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _freshest_age_days(results: list[dict]) -> float | None:
    """Age, in days, of the most recently fetched document among
    `results` whose "fetched_at" parses with component 02's own
    timestamp format. `None` when no result carries a parseable
    timestamp — treated by the caller as maximally stale, not
    neutral, so a missing/malformed clock reading can't accidentally
    satisfy freshness."""
    now = time.time()
    ages = []
    for result in results:
        fetched_at = result.get("fetched_at")
        if not fetched_at:
            continue
        try:
            parsed = time.strptime(fetched_at, _TIMESTAMP_FORMAT)
        except ValueError:
            continue
        age_seconds = now - time.mktime(parsed)
        ages.append(age_seconds / 86400.0)
    return min(ages) if ages else None


class DefaultRetrievalEvaluator:
    """Real implementation of RetrievalEvaluator (ADR-0012's shape,
    ADR-0033's mechanism): a rule-based sufficiency check across four
    real signals, all failing closed (any one signal failing makes the
    whole check insufficient):

      document count  — at least `_MIN_DOCUMENT_COUNT` (1) result;
                         zero results is never sufficient regardless
                         of anything else.
      reliability      — the average of each result's "reliability"
                         field (component 02's `SourceMetadata
                         .reliability`, attached by `DefaultRetriever`)
                         must be at or above `_MIN_AVERAGE_RELIABILITY`
                         (0.5). This sits strictly between
                         `DefaultDataSources`'s synthetic-document
                         score (0.3) and real-document score (1.0), so
                         a batch of only placeholder-fetched content
                         (ADR-0027 still Proposed) honestly never
                         counts as sufficient on its own — that's a
                         real, intended consequence of this threshold,
                         not a bug: it's what correctly drives
                         CorrectiveRetriever's fallback path today.
      coverage         — the fraction of `query.text`'s significant
                         keywords (see `_significant_keywords`) that
                         appear somewhere in the results' combined
                         content, at or above `_MIN_COVERAGE_RATIO`
                         (0.5): retrieved documents that don't actually
                         talk about what was asked aren't sufficient
                         just because there are enough of them.
      freshness        — the single freshest result's age must be
                         at or below `_MAX_STALENESS_DAYS` (180, ~6
                         months — a deliberately generous window for
                         financial filings/reports, which are
                         legitimately relevant for months, while still
                         ruling out stale news). No result with a
                         parseable "fetched_at" at all fails this
                         signal outright, per `_freshest_age_days`.
    """

    def is_sufficient(self, results: list[dict], query: Query) -> bool:
        with traced("DefaultRetrievalEvaluator.is_sufficient"):
            if len(results) < _MIN_DOCUMENT_COUNT:
                return False

            reliabilities = [result.get("reliability", 0.0) for result in results]
            average_reliability = sum(reliabilities) / len(reliabilities)
            if average_reliability < _MIN_AVERAGE_RELIABILITY:
                return False

            keywords = _significant_keywords(query.text)
            corpus = " ".join(str(result.get("content", "")) for result in results)
            # An uninformative query (no significant keywords) has nothing
            # for coverage to check against; skip rather than fail a signal
            # that can't be computed, same "don't fail on a missing signal"
            # posture as freshness above.
            if keywords and _coverage_ratio(keywords, corpus) < _MIN_COVERAGE_RATIO:
                return False

            freshest_age = _freshest_age_days(results)
            if freshest_age is None or freshest_age > _MAX_STALENESS_DAYS:
                return False

            return True


class CorrectiveRetriever(Protocol):
    def retrieve_externally(self, query: Query, attempt: int) -> list[dict]:
        """External search, bounded attempts. Once the budget is
        exhausted, the caller should treat 'no useful evidence found'
        as a legitimate terminal state (CRAG), not an empty context."""
        ...


class StubCorrectiveRetriever:
    """Structural implementation of CorrectiveRetriever. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def retrieve_externally(self, query: Query, attempt: int) -> list[dict]:
        with traced("StubCorrectiveRetriever.retrieve_externally"):
            return []


_CORRECTIVE_RETRIEVAL_LOG_TABLE = "retrieval_corrective_log"
_DEFAULT_MAX_CORRECTIVE_ATTEMPTS = 3


class ExternalSearchProvider(Protocol):
    """The one seam `DefaultCorrectiveRetriever.retrieve_externally`
    calls through to reach the outside world — same pattern as
    component 02's `SourceFetcher` (ADR-0026/0027). See ADR-0034 (status
    partially superseded) for the real options considered and their
    honest tradeoffs. `TavilySearchProvider` (ADR-0047, below) is now
    the real implementation behind this interface when `TAVILY_API_KEY`
    is configured; nothing in `DefaultCorrectiveRetriever` needed to
    change for that swap."""

    def search(self, query: Query, attempt: int) -> list[dict]:
        ...


class PlaceholderExternalSearchProvider:
    """Explicitly NOT a real external search — see `ExternalSearchProvider`'s
    docstring and ADR-0034 (status partially superseded by ADR-0047).
    Used whenever `TAVILY_API_KEY` isn't configured. Always returns an empty
    result list for every query/attempt. That emptiness is itself
    meaningful here, not a stand-in masquerading as a real answer:
    CRAG's fig. 1 "no useful evidence found" terminal state (ADR-0012)
    already expects exactly this case once corrective retrieval turns
    up nothing, so an honest empty list is structurally correct today,
    not just a placeholder waiting to be replaced."""

    def search(self, query: Query, attempt: int) -> list[dict]:
        with traced("PlaceholderExternalSearchProvider.search"):
            return []


class TavilySearchProvider:
    """Real `ExternalSearchProvider` (ADR-0047) — resolves ADR-0034's
    corrective-retrieval search-provider gap via Tavily's real search
    API (`src/tavily_client.py`). Returns Tavily's own `results` list
    verbatim (a list of dicts); `DefaultCorrectiveRetriever` is what
    tags each one `UNTRUSTED` before returning it, exactly as it already
    does for `PlaceholderExternalSearchProvider`'s output — this class
    changes nothing about that contract, only what `search()` actually
    returns. `attempt` is accepted (Protocol conformance) but not used:
    Tavily's `/search` endpoint has no attempt-numbered variant, and
    `DefaultCorrectiveRetriever` already enforces the retry budget
    before this method is ever called."""

    def search(self, query: Query, attempt: int) -> list[dict]:
        with traced("TavilySearchProvider.search"):
            return search_tavily(query.text)


def get_external_search_provider(placeholder: "ExternalSearchProvider | None" = None) -> "ExternalSearchProvider":
    """Selection function — the same key-gated shape `src/llm.py`'s
    `get_reason_fn` and `src/components/c02_data_sources.py`'s
    `get_source_fetcher` already established. Returns
    `TavilySearchProvider` when `TAVILY_API_KEY` is configured,
    otherwise returns `placeholder` (`PlaceholderExternalSearchProvider`
    by default) unchanged. Constructing `TavilySearchProvider` never
    touches the network — only calling `.search()` does — so it is safe
    for `DefaultCorrectiveRetriever` to resolve this automatically at
    construction time."""
    placeholder = placeholder or PlaceholderExternalSearchProvider()
    if get_tavily_api_key() is not None:
        return TavilySearchProvider()
    return placeholder


class DefaultCorrectiveRetriever:
    """Real implementation of CorrectiveRetriever (ADR-0012's shape,
    ADR-0033's mechanism, ADR-0034/ADR-0047's search seam — real via
    `TavilySearchProvider` when `TAVILY_API_KEY` is configured,
    `PlaceholderExternalSearchProvider` otherwise).

    Bounded retry budget (ADR-0012's Consequences, mirroring Agent
    Runtime's replan budget shape, ADR-0004): `attempt` values beyond
    `max_attempts` (default `_DEFAULT_MAX_CORRECTIVE_ATTEMPTS`, 3)
    return an empty list immediately without even calling the search
    provider — the budget is exhausted, so "no useful evidence found"
    is the correct terminal state (ADR-0012), not another external
    call.

    Within budget, every result the injected `ExternalSearchProvider`
    returns is tagged UNTRUSTED via `BoundaryGate.tag_provenance`
    before being returned — external/corrective search is a strictly
    less-vetted source than component 02's own `DataSources` (which
    already tags its own documents at the point of production), so
    this component tags at its own point of production instead of
    assuming the provider already did. Every call (successful or
    budget-exhausted) is logged to `_CORRECTIVE_RETRIEVAL_LOG_TABLE`
    via the injected `Infrastructure` for Observability & Governance's
    promised corrective-retrieval-rate signal.
    """

    def __init__(
        self,
        external_search_provider: ExternalSearchProvider | None = None,
        boundary_gate: BoundaryGate | None = None,
        infrastructure: Infrastructure | None = None,
        max_attempts: int = _DEFAULT_MAX_CORRECTIVE_ATTEMPTS,
    ) -> None:
        self._external_search_provider = external_search_provider or get_external_search_provider()
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._max_attempts = max_attempts

    def retrieve_externally(self, query: Query, attempt: int) -> list[dict]:
        with traced("DefaultCorrectiveRetriever.retrieve_externally"):
            budget_exhausted = attempt > self._max_attempts
            if budget_exhausted:
                results: list[dict] = []
            else:
                raw_results = self._external_search_provider.search(query, attempt)
                results = [
                    self._boundary_gate.tag_provenance(document, source="external_search")
                    for document in raw_results
                ]

            self._infrastructure.store(
                _CORRECTIVE_RETRIEVAL_LOG_TABLE,
                {
                    "id": f"corrective::{uuid.uuid4().hex}",
                    "query_text": query.text,
                    "attempt": attempt,
                    "budget_exhausted": budget_exhausted,
                    "result_count": len(results),
                    "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )
            return results


class ContextBuilder(Protocol):
    def construct(self, documents: list[dict]) -> ContextPack:
        ...


class StubContextBuilder:
    """Structural implementation of ContextBuilder. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def construct(self, documents: list[dict]) -> ContextPack:
        with traced("StubContextBuilder.construct"):
            return ContextPack(documents=[], sufficient=False)


class DefaultContextBuilder:
    """Real implementation of ContextBuilder (ADR-0033): structural
    assembly of `documents` into a `ContextPack`, with `sufficient`
    set from the real `RetrievalEvaluator` result rather than a
    hardcoded value.

    One flagged, deliberate deviation from the `ContextBuilder`
    Protocol's bare `construct(documents)` signature (loop.md step 6):
    `construct` here also accepts an optional `query: Query | None`
    keyword parameter. The Protocol's signature has no way to pass the
    query a sufficiency check needs — `RetrievalEvaluator.is_sufficient`
    takes `(results, query)`, not `(results)` — so without this
    addition `sufficient` could only ever be a documents-only guess,
    which is not "from the real evaluator result" as asked. The
    addition is purely additive (a keyword default of `None`), so
    every existing call site built against the bare Protocol signature
    is unaffected; when a caller does have the originating `Query`
    (the normal case — a fig. 1 caller runs gate -> retrieve ->
    evaluate -> construct on the same query throughout), passing it
    gets a real evaluator-backed sufficiency signal. When `query` is
    omitted, this falls back to the weakest structural signal alone
    (non-empty `documents`) rather than fabricating a query-less
    evaluator call — documented here rather than silently degraded.
    """

    def __init__(self, evaluator: "RetrievalEvaluator | None" = None) -> None:
        self._evaluator = evaluator or DefaultRetrievalEvaluator()

    def construct(self, documents: list[dict], query: "Query | None" = None) -> ContextPack:
        with traced("DefaultContextBuilder.construct"):
            if query is not None:
                sufficient = self._evaluator.is_sufficient(documents, query)
            else:
                sufficient = bool(documents)
            return ContextPack(documents=documents, sufficient=sufficient)
