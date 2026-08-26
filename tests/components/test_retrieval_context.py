"""Tests for the `Default*` adapters in src/components/c05_retrieval_context.py
(ADR-0011, ADR-0012, ADR-0033, ADR-0034).

Most tests run against `_FakeInfrastructure`, a minimal in-memory test
double of the `Infrastructure` Protocol (same shape and semantics as
tests/components/test_data_sources.py's own double), and `DefaultDataSources`
wired to an in-memory double `SourceFetcher` so `DefaultRetriever` is
exercised against real component-02 logic without a live Postgres.
`DefaultBoundaryGate` (cross_cutting/security.py) is used for real — it
has no external dependency — so provenance tagging on corrective
retrieval is verified against real ADR-0003 behavior, not a stub.
`StubKnowledgeEntity` stands in for component 04 (still whiteboard-only)
except where a test double is needed to verify entity resolution is
actually being called.
"""

import time
import uuid

import pytest

from components.c02_data_sources import DefaultDataSources, Source, SourceType
from components.c04_knowledge_entity import Entity, StubKnowledgeEntity
from components.c05_retrieval_context import (
    DefaultContextBuilder,
    DefaultCorrectiveRetriever,
    DefaultRetrievalEvaluator,
    DefaultRetrievalGate,
    DefaultRetriever,
    PlaceholderExternalSearchProvider,
    Query,
    TavilySearchProvider,
    get_external_search_provider,
)
from cross_cutting.security import DefaultBoundaryGate
import tavily_client


@pytest.fixture(autouse=True)
def _no_tavily_key_by_default(monkeypatch):
    """Several tests in this file construct a bare `DefaultCorrectiveRetriever`
    and rely on `PlaceholderExternalSearchProvider` being its default —
    `get_external_search_provider()` (ADR-0047) reads `TAVILY_API_KEY`,
    and this repo's own `.env` may carry a real key for actual use
    elsewhere in this project. Without this fixture, that would make
    those tests silently issue real network calls to Tavily (or make
    their placeholder-identity assertions machine-dependent) instead of
    deterministic — the same hygiene `tests/components/test_data_sources.py`
    already established for `ALPHA_VANTAGE_API_KEY`."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(tavily_client, "_ENV_FILE_PATH", tavily_client._ENV_FILE_PATH.parent / "does-not-exist.env")


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol
    (src/infrastructure.py): store/retrieve/query only, since that's
    all this component's Default* adapters call. Same semantics as
    DefaultInfrastructure: store() keys off record["id"] when present,
    query() does containment matching."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}
        self.stored: list[tuple[str, dict]] = []

    def store(self, table: str, record: dict) -> str:
        record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
        record = dict(record)
        self._records[(table, record_id)] = record
        self.stored.append((table, record))
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        record = self._records.get((table, id_))
        return dict(record) if record is not None else None

    def query(self, table: str, filters: dict) -> list[dict]:
        matches = []
        for (record_table, _), record in self._records.items():
            if record_table != table:
                continue
            if all(record.get(key) == value for key, value in filters.items()):
                matches.append(dict(record))
        return matches


class _StaticSourceFetcher:
    """Test double SourceFetcher (component 02's seam): always returns
    the same content for every source."""

    def __init__(self, content: bytes, fetched_at: str | None = None) -> None:
        self._content = content
        self._fetched_at = fetched_at or time.strftime("%Y-%m-%dT%H:%M:%S")

    def fetch(self, source: Source):
        from components.c02_data_sources import SourceDocument

        return SourceDocument(source_id=source.id, content=self._content, fetched_at=self._fetched_at)


def _data_sources(content: bytes = b"real fetched content about revenue and earnings") -> DefaultDataSources:
    return DefaultDataSources(
        infrastructure=_FakeInfrastructure(),
        boundary_gate=DefaultBoundaryGate(),
        source_fetcher=_StaticSourceFetcher(content),
    )


class _StubKnowledgeEntityWithFixedResolutions:
    """Test double KnowledgeEntity: resolves only mentions present in a
    fixed lookup table, everything else resolves to None — exercises
    DefaultRetriever's real call into the Protocol without needing a
    real component-04 implementation."""

    def __init__(self, lookup: dict[str, Entity]) -> None:
        self._lookup = lookup
        self.resolved_mentions: list[str] = []

    def resolve_entity(self, mention: str) -> Entity | None:
        self.resolved_mentions.append(mention)
        return self._lookup.get(mention)

    def create_entity(self, details):
        raise NotImplementedError

    def merge_entities(self, a, b):
        raise NotImplementedError

    def link_entities(self, a, b, relationship):
        raise NotImplementedError

    def represent_relationships(self, entity):
        raise NotImplementedError

    def update_knowledge(self, entity, updates):
        raise NotImplementedError

    def query_relationships(self, entity, kind=None):
        raise NotImplementedError


# --- DefaultRetrievalGate ---------------------------------------------------


def test_gate_retrieves_when_query_has_no_significant_keywords():
    gate = DefaultRetrievalGate()
    query = Query(text="is it", context={"existing_content": ["is it"]})

    assert gate.should_retrieve(query) is True


def test_gate_retrieves_when_existing_content_absent():
    gate = DefaultRetrievalGate()
    query = Query(text="What is Apple's quarterly revenue growth", context={})

    assert gate.should_retrieve(query) is True


def test_gate_skips_retrieval_when_existing_content_already_covers_query():
    gate = DefaultRetrievalGate()
    query = Query(
        text="What is Apple's quarterly revenue growth",
        context={
            "existing_content": [
                "Apple reported strong quarterly revenue growth driven by services."
            ]
        },
    )

    assert gate.should_retrieve(query) is False


def test_gate_retrieves_when_existing_content_only_partially_covers_query():
    gate = DefaultRetrievalGate()
    query = Query(
        text="What is Apple's quarterly revenue growth compared to Microsoft's cloud earnings",
        context={"existing_content": ["Apple reported quarterly revenue growth."]},
    )

    assert gate.should_retrieve(query) is True


def test_gate_accepts_existing_content_as_a_single_string_not_only_a_list():
    gate = DefaultRetrievalGate()
    query = Query(
        text="quarterly revenue growth",
        context={"existing_content": "quarterly revenue growth was strong"},
    )

    assert gate.should_retrieve(query) is False


# --- DefaultRetriever --------------------------------------------------------


def test_retriever_discovers_and_returns_documents_across_all_source_types_by_default():
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-NEWS", type=SourceType.NEWS))
    retriever = DefaultRetriever(
        data_sources=data_sources,
        knowledge_entity=StubKnowledgeEntity(),
        infrastructure=_FakeInfrastructure(),
    )

    documents = retriever.retrieve(Query(text="Apple earnings", context={}))

    assert len(documents) == 1
    assert documents[0]["source_id"] == "AAPL-NEWS"
    assert documents[0]["source_type"] == "NEWS"
    assert "revenue" in documents[0]["content"]


def test_retriever_honors_explicit_source_criteria_from_query_context():
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-NEWS", type=SourceType.NEWS))
    data_sources.register_source(Source(id="AAPL-10K", type=SourceType.FILING))
    retriever = DefaultRetriever(data_sources=data_sources, infrastructure=_FakeInfrastructure())

    documents = retriever.retrieve(Query(text="Apple filing", context={"source_criteria": {"type": "FILING"}}))

    assert len(documents) == 1
    assert documents[0]["source_id"] == "AAPL-10K"


def test_retriever_accepts_a_list_of_source_criteria():
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-NEWS", type=SourceType.NEWS))
    data_sources.register_source(Source(id="AAPL-10K", type=SourceType.FILING))
    data_sources.register_source(Source(id="AAPL-REPORT", type=SourceType.REPORT))
    retriever = DefaultRetriever(data_sources=data_sources, infrastructure=_FakeInfrastructure())

    documents = retriever.retrieve(
        Query(text="Apple", context={"source_criteria": [{"type": "NEWS"}, {"type": "FILING"}]})
    )

    found_ids = {document["source_id"] for document in documents}
    assert found_ids == {"AAPL-NEWS", "AAPL-10K"}


def test_retriever_resolves_entity_mentions_via_knowledge_entity_protocol():
    lookup = {"Apple": Entity(id="ent-aapl", kind="Company")}
    knowledge_entity = _StubKnowledgeEntityWithFixedResolutions(lookup)
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-NEWS", type=SourceType.NEWS))
    retriever = DefaultRetriever(
        data_sources=data_sources, knowledge_entity=knowledge_entity, infrastructure=_FakeInfrastructure()
    )

    documents = retriever.retrieve(Query(text="Apple reported strong earnings", context={}))

    assert "Apple" in knowledge_entity.resolved_mentions
    assert documents[0]["matched_entity_ids"] == ["ent-aapl"]


def test_retriever_uses_explicit_entity_mentions_when_supplied():
    lookup = {"AAPL": Entity(id="ent-aapl", kind="Security")}
    knowledge_entity = _StubKnowledgeEntityWithFixedResolutions(lookup)
    retriever = DefaultRetriever(
        data_sources=_data_sources(), knowledge_entity=knowledge_entity, infrastructure=_FakeInfrastructure()
    )

    retriever.retrieve(Query(text="some lowercase query text", context={"entity_mentions": ["AAPL"]}))

    assert knowledge_entity.resolved_mentions == ["AAPL"]


def test_retriever_deduplicates_sources_seen_under_multiple_criteria():
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-NEWS", type=SourceType.NEWS))
    retriever = DefaultRetriever(data_sources=data_sources, infrastructure=_FakeInfrastructure())

    documents = retriever.retrieve(
        Query(text="Apple", context={"source_criteria": [{"type": "NEWS"}, {"type": "NEWS"}]})
    )

    assert len(documents) == 1


def test_retriever_attaches_reliability_from_data_sources():
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-NEWS", type=SourceType.NEWS))
    retriever = DefaultRetriever(data_sources=data_sources, infrastructure=_FakeInfrastructure())

    documents = retriever.retrieve(Query(text="Apple", context={"source_criteria": {"type": "NEWS"}}))

    assert documents[0]["reliability"] == 1.0  # _StaticSourceFetcher content is not the placeholder marker


def test_retriever_logs_each_call_to_infrastructure():
    infra = _FakeInfrastructure()
    data_sources = _data_sources()
    retriever = DefaultRetriever(data_sources=data_sources, infrastructure=infra)

    retriever.retrieve(Query(text="Apple earnings call", context={"source_criteria": {"type": "NEWS"}}))

    logged = [record for table, record in infra.stored if table == "retrieval_log"]
    assert len(logged) == 1
    assert logged[0]["query_text"] == "Apple earnings call"


# --- DefaultRetrievalEvaluator -----------------------------------------------


def _fresh_document(reliability: float = 1.0, content: str = "Apple reported quarterly revenue growth") -> dict:
    return {
        "content": content,
        "reliability": reliability,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def test_evaluator_insufficient_when_no_results():
    evaluator = DefaultRetrievalEvaluator()

    assert evaluator.is_sufficient([], Query(text="Apple revenue growth", context={})) is False


def test_evaluator_insufficient_when_average_reliability_too_low():
    evaluator = DefaultRetrievalEvaluator()
    results = [_fresh_document(reliability=0.3)]  # synthetic/placeholder-level reliability

    assert evaluator.is_sufficient(results, Query(text="Apple revenue growth", context={})) is False


def test_evaluator_insufficient_when_content_does_not_cover_query_keywords():
    evaluator = DefaultRetrievalEvaluator()
    results = [_fresh_document(content="completely unrelated content about weather patterns")]

    assert evaluator.is_sufficient(results, Query(text="Apple quarterly revenue growth", context={})) is False


def test_evaluator_insufficient_when_freshest_document_is_stale():
    evaluator = DefaultRetrievalEvaluator()
    results = [_fresh_document(content="Apple reported quarterly revenue growth", reliability=1.0)]
    results[0]["fetched_at"] = "2020-01-01T00:00:00"  # far beyond the 180-day staleness window

    assert evaluator.is_sufficient(results, Query(text="Apple quarterly revenue growth", context={})) is False


def test_evaluator_insufficient_when_fetched_at_is_unparseable():
    evaluator = DefaultRetrievalEvaluator()
    results = [_fresh_document(content="Apple reported quarterly revenue growth")]
    results[0]["fetched_at"] = "not-a-timestamp"

    assert evaluator.is_sufficient(results, Query(text="Apple quarterly revenue growth", context={})) is False


def test_evaluator_sufficient_when_all_four_signals_pass():
    evaluator = DefaultRetrievalEvaluator()
    results = [_fresh_document(content="Apple reported strong quarterly revenue growth", reliability=1.0)]

    assert evaluator.is_sufficient(results, Query(text="Apple quarterly revenue growth", context={})) is True


def test_evaluator_skips_coverage_check_for_uninformative_query():
    evaluator = DefaultRetrievalEvaluator()
    results = [_fresh_document(content="anything at all", reliability=1.0)]

    assert evaluator.is_sufficient(results, Query(text="is it", context={})) is True


# --- PlaceholderExternalSearchProvider / DefaultCorrectiveRetriever ----------


def test_placeholder_external_search_provider_always_returns_empty():
    provider = PlaceholderExternalSearchProvider()

    assert provider.search(Query(text="anything", context={}), attempt=1) == []


def test_default_corrective_retriever_uses_placeholder_when_none_injected():
    retriever = DefaultCorrectiveRetriever(infrastructure=_FakeInfrastructure())

    assert isinstance(retriever._external_search_provider, PlaceholderExternalSearchProvider)


# --- get_external_search_provider / TavilySearchProvider (ADR-0047) ----------


def test_get_external_search_provider_returns_placeholder_when_key_unset():
    placeholder = PlaceholderExternalSearchProvider()

    assert get_external_search_provider(placeholder) is placeholder


def test_get_external_search_provider_returns_tavily_provider_when_key_set(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "demo-key")

    provider = get_external_search_provider()

    assert isinstance(provider, TavilySearchProvider)


def test_default_corrective_retriever_resolves_tavily_provider_when_key_set(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "demo-key")

    retriever = DefaultCorrectiveRetriever(infrastructure=_FakeInfrastructure())

    assert isinstance(retriever._external_search_provider, TavilySearchProvider)


def test_tavily_provider_search_returns_real_results_list(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "demo-key")
    captured = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"title": "Apple Q3 earnings", "url": "https://example.com/a", "content": "..."}]}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(tavily_client.requests, "post", fake_post)

    results = TavilySearchProvider().search(Query(text="Apple Q3 earnings", context={}), attempt=1)

    assert captured["url"] == tavily_client.TAVILY_SEARCH_URL
    assert captured["json"] == {"api_key": "demo-key", "query": "Apple Q3 earnings", "max_results": 5}
    assert results == [{"title": "Apple Q3 earnings", "url": "https://example.com/a", "content": "..."}]


def test_tavily_provider_search_raises_specific_error_when_key_missing():
    with pytest.raises(tavily_client.MissingTavilyAPIKeyError):
        TavilySearchProvider().search(Query(text="anything", context={}), attempt=1)


class _StaticExternalSearchProvider:
    def __init__(self, results: list[dict]) -> None:
        self._results = results
        self.calls: list[int] = []

    def search(self, query: Query, attempt: int) -> list[dict]:
        self.calls.append(attempt)
        return list(self._results)


def test_corrective_retriever_tags_external_results_untrusted():
    provider = _StaticExternalSearchProvider([{"content": "a corrective search hit"}])
    retriever = DefaultCorrectiveRetriever(
        external_search_provider=provider,
        boundary_gate=DefaultBoundaryGate(),
        infrastructure=_FakeInfrastructure(),
    )

    results = retriever.retrieve_externally(Query(text="Apple", context={}), attempt=1)

    assert len(results) == 1
    assert results[0]["provenance"] == "UNTRUSTED"
    assert results[0]["content"] == "a corrective search hit"


def test_corrective_retriever_returns_empty_once_retry_budget_exhausted():
    provider = _StaticExternalSearchProvider([{"content": "should never be returned"}])
    retriever = DefaultCorrectiveRetriever(
        external_search_provider=provider, infrastructure=_FakeInfrastructure(), max_attempts=3
    )

    results = retriever.retrieve_externally(Query(text="Apple", context={}), attempt=4)

    assert results == []
    assert provider.calls == []  # budget exhausted before the provider was even called


def test_corrective_retriever_calls_provider_within_budget():
    provider = _StaticExternalSearchProvider([])
    retriever = DefaultCorrectiveRetriever(
        external_search_provider=provider, infrastructure=_FakeInfrastructure(), max_attempts=3
    )

    retriever.retrieve_externally(Query(text="Apple", context={}), attempt=2)

    assert provider.calls == [2]


def test_corrective_retriever_logs_every_attempt_including_exhausted_ones():
    infra = _FakeInfrastructure()
    retriever = DefaultCorrectiveRetriever(infrastructure=infra, max_attempts=1)

    retriever.retrieve_externally(Query(text="Apple", context={}), attempt=1)
    retriever.retrieve_externally(Query(text="Apple", context={}), attempt=2)

    logged = [record for table, record in infra.stored if table == "retrieval_corrective_log"]
    assert len(logged) == 2
    assert logged[0]["budget_exhausted"] is False
    assert logged[1]["budget_exhausted"] is True


# --- DefaultContextBuilder ---------------------------------------------------


def test_context_builder_uses_evaluator_result_when_query_supplied():
    class _AlwaysInsufficientEvaluator:
        def is_sufficient(self, results, query):
            return False

    builder = DefaultContextBuilder(evaluator=_AlwaysInsufficientEvaluator())
    documents = [_fresh_document()]

    pack = builder.construct(documents, query=Query(text="Apple revenue", context={}))

    assert pack.documents == documents
    assert pack.sufficient is False


def test_context_builder_uses_real_evaluator_by_default():
    builder = DefaultContextBuilder()
    documents = [_fresh_document(content="Apple reported strong quarterly revenue growth", reliability=1.0)]

    pack = builder.construct(documents, query=Query(text="Apple quarterly revenue growth", context={}))

    assert pack.sufficient is True


def test_context_builder_falls_back_to_structural_check_without_a_query():
    builder = DefaultContextBuilder()

    empty_pack = builder.construct([])
    non_empty_pack = builder.construct([_fresh_document()])

    assert empty_pack.sufficient is False
    assert non_empty_pack.sufficient is True


# --- Live Postgres integration (skips cleanly without docker-compose) ------


def _postgres_available() -> bool:
    try:
        import psycopg

        from infrastructure_postgres import DEFAULT_POSTGRES_DSN

        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="no live Postgres reachable — run `docker-compose up -d` for real coverage",
)


@requires_postgres
def test_retriever_against_real_postgres_and_data_sources():
    from infrastructure_postgres import DefaultInfrastructure

    infra = DefaultInfrastructure()
    data_sources = DefaultDataSources(
        infrastructure=infra,
        source_fetcher=_StaticSourceFetcher(b"real postgres retrieval content about earnings"),
    )
    source_id = f"test-src-{uuid.uuid4().hex}"
    data_sources.register_source(Source(id=source_id, type=SourceType.NEWS))
    retriever = DefaultRetriever(data_sources=data_sources, infrastructure=infra)

    documents = retriever.retrieve(Query(text="earnings", context={"source_criteria": {"type": "NEWS"}}))

    assert any(document["source_id"] == source_id for document in documents)
