"""Tests for DefaultDataSources and its SourceFetcher seam
(src/components/c02_data_sources.py, ADR-0026, ADR-0027).

Most tests run against `_FakeInfrastructure`, a minimal in-memory test
double of the `Infrastructure` Protocol, so this component's own logic
(persistence shape, provenance/timestamp/reliability wiring, the
SourceFetcher seam) is exercised fast and without a live Postgres.
`DefaultBoundaryGate` (cross_cutting/security.py) is used for real —
it has no external dependency — so provenance tagging is verified
against real ADR-0003 behavior, not a stub.

A small number of `@requires_postgres` tests at the bottom exercise
DefaultDataSources against the real DefaultInfrastructure, mirroring
tests/test_infrastructure_postgres.py's skip-cleanly-when-no-live-DB
pattern, for genuine end-to-end coverage when docker-compose is up.
"""

import uuid

import pytest

from components.c02_data_sources import (
    PLACEHOLDER_FETCH_MARKER,
    DefaultDataSources,
    PlaceholderSourceFetcher,
    Source,
    SourceDocument,
    SourceType,
)
from cross_cutting.security import DefaultBoundaryGate


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol
    (src/infrastructure.py): only store/retrieve/query, since that's
    all DefaultDataSources calls. Same semantics as
    DefaultInfrastructure: store() keys off record["id"] when present,
    query() does containment matching (every filter key/value must
    equal the stored record's)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}

    def store(self, table: str, record: dict) -> str:
        record_id = str(record["id"]) if "id" in record else str(uuid.uuid4())
        self._records[(table, record_id)] = dict(record)
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
    """Test double SourceFetcher: always returns the same content."""

    def __init__(self, content: bytes, fetched_at: str = "2026-08-26T00:00:00") -> None:
        self._content = content
        self._fetched_at = fetched_at

    def fetch(self, source: Source) -> SourceDocument:
        return SourceDocument(source_id=source.id, content=self._content, fetched_at=self._fetched_at)


class _SequentialSourceFetcher:
    """Test double SourceFetcher: returns each item of `contents` in
    order, one per call, so update_source's content-changed detection
    can be exercised against genuinely different fetches."""

    def __init__(self, contents: list[bytes]) -> None:
        self._contents = iter(contents)

    def fetch(self, source: Source) -> SourceDocument:
        return SourceDocument(source_id=source.id, content=next(self._contents), fetched_at="irrelevant")


def _data_sources(source_fetcher=None) -> DefaultDataSources:
    return DefaultDataSources(
        infrastructure=_FakeInfrastructure(),
        boundary_gate=DefaultBoundaryGate(),
        source_fetcher=source_fetcher,
    )


# --- PlaceholderSourceFetcher -------------------------------------------


def test_placeholder_fetch_returns_unmistakable_marker_not_empty_bytes():
    fetcher = PlaceholderSourceFetcher()
    source = Source(id="src-1", type=SourceType.NEWS)

    document = fetcher.fetch(source)

    assert document.content == PLACEHOLDER_FETCH_MARKER
    assert document.content != b""  # never mistakable for a real empty response
    assert document.source_id == "src-1"


def test_placeholder_fetch_reports_a_real_timestamp():
    fetcher = PlaceholderSourceFetcher()
    source = Source(id="src-1", type=SourceType.MARKET_DATA)

    document = fetcher.fetch(source)

    assert document.fetched_at  # non-empty: a real clock reading, even though content is synthetic


def test_default_data_sources_uses_placeholder_fetcher_when_none_injected():
    data_sources = DefaultDataSources(infrastructure=_FakeInfrastructure())

    assert isinstance(data_sources._source_fetcher, PlaceholderSourceFetcher)


# --- register_source / discover_source -----------------------------------


def test_register_then_discover_by_type_returns_registered_source():
    data_sources = _data_sources()
    source = Source(id="AAPL-10K", type=SourceType.FILING)

    data_sources.register_source(source)
    found = data_sources.discover_source({"type": "FILING"})

    assert found == [source]


def test_discover_source_does_not_match_unregistered_type():
    data_sources = _data_sources()
    data_sources.register_source(Source(id="AAPL-10K", type=SourceType.FILING))

    assert data_sources.discover_source({"type": "NEWS"}) == []


def test_discover_source_excludes_ingested_document_rows():
    """A source's ingested SourceDocument lives in the same table
    (ADR-0026) but must never be mistaken for a Source registration by
    discover_source's record_kind filter."""
    data_sources = _data_sources()
    source = Source(id="AAPL-10K", type=SourceType.FILING)
    data_sources.register_source(source)
    data_sources.ingest_source(source)

    found = data_sources.discover_source({"type": "FILING"})

    assert found == [source]


# --- ingest_source / retrieve_source --------------------------------------


def test_ingest_source_returns_and_persists_a_document():
    data_sources = _data_sources(_StaticSourceFetcher(b"real-looking content"))
    source = Source(id="src-1", type=SourceType.NEWS)

    ingested = data_sources.ingest_source(source)
    retrieved = data_sources.retrieve_source("src-1")

    assert ingested.content == b"real-looking content"
    assert retrieved == ingested


def test_retrieve_source_round_trips_binary_content_exactly():
    tricky_bytes = bytes(range(256))  # every byte value, including nulls and high bytes
    data_sources = _data_sources(_StaticSourceFetcher(tricky_bytes))
    source = Source(id="src-1", type=SourceType.MARKET_DATA)

    data_sources.ingest_source(source)
    retrieved = data_sources.retrieve_source("src-1")

    assert retrieved.content == tricky_bytes


def test_retrieve_source_returns_none_when_never_ingested():
    data_sources = _data_sources()

    assert data_sources.retrieve_source("never-ingested") is None


def test_ingest_source_tags_provenance_untrusted_for_real():
    """ADR-0003/ADR-0026: every produced SourceDocument is tagged
    UNTRUSTED via DefaultBoundaryGate.tag_provenance automatically,
    not only when track_source_provenance is called separately."""
    infra = _FakeInfrastructure()
    data_sources = DefaultDataSources(
        infrastructure=infra,
        boundary_gate=DefaultBoundaryGate(),
        source_fetcher=_StaticSourceFetcher(b"content"),
    )
    source = Source(id="src-1", type=SourceType.NEWS)

    data_sources.ingest_source(source)

    provenance_record = infra.retrieve("sources", "provenance::src-1")
    assert provenance_record["origin"] == "UNTRUSTED"


# --- update_source ---------------------------------------------------------


def test_update_source_first_call_reports_no_previous_document():
    data_sources = _data_sources(_StaticSourceFetcher(b"first content"))
    source = Source(id="src-1", type=SourceType.REPORT)

    snapshot = data_sources.update_source(source)

    assert snapshot.source_id == "src-1"
    assert snapshot.state["previous_fetched_at"] is None
    assert snapshot.state["content_changed"] is True


def test_update_source_reports_unchanged_when_refetched_content_is_identical():
    data_sources = _data_sources(_StaticSourceFetcher(b"stable content"))
    source = Source(id="src-1", type=SourceType.REPORT)
    data_sources.ingest_source(source)  # establishes the "previous" document

    snapshot = data_sources.update_source(source)

    assert snapshot.state["content_changed"] is False


def test_update_source_reports_changed_when_refetched_content_differs():
    fetcher = _SequentialSourceFetcher([b"version one", b"version two"])
    data_sources = _data_sources(fetcher)
    source = Source(id="src-1", type=SourceType.REPORT)
    data_sources.ingest_source(source)  # "version one" becomes the previous document

    snapshot = data_sources.update_source(source)  # fetches "version two"

    assert snapshot.state["content_changed"] is True


# --- track_source_provenance / track_source_timestamp ----------------------


def test_track_source_provenance_returns_untrusted_and_matches_document_source_id():
    data_sources = _data_sources()
    document = SourceDocument(source_id="src-1", content=b"x", fetched_at="2026-08-26T00:00:00")

    provenance = data_sources.track_source_provenance(document)

    assert provenance.source_id == "src-1"
    assert provenance.origin == "UNTRUSTED"


def test_track_source_timestamp_returns_the_documents_fetched_at():
    data_sources = _data_sources()
    document = SourceDocument(source_id="src-1", content=b"x", fetched_at="2026-08-26T12:34:56")

    assert data_sources.track_source_timestamp(document) == "2026-08-26T12:34:56"


# --- track_source_reliability_metadata --------------------------------------


def test_reliability_metadata_scores_zero_when_never_ingested():
    data_sources = _data_sources()
    source = Source(id="never-ingested", type=SourceType.NEWS)

    metadata = data_sources.track_source_reliability_metadata(source)

    assert metadata.reliability == 0.0
    assert metadata.timestamp == ""


def test_reliability_metadata_scores_low_nonzero_for_placeholder_backed_document():
    """With only PlaceholderSourceFetcher wired in, every ingested
    document is synthetic — ADR-0027's honest consequence."""
    data_sources = _data_sources()  # defaults to PlaceholderSourceFetcher
    source = Source(id="src-1", type=SourceType.NEWS)
    data_sources.ingest_source(source)

    metadata = data_sources.track_source_reliability_metadata(source)

    assert 0.0 < metadata.reliability < 1.0


def test_reliability_metadata_scores_full_for_genuinely_fetched_document():
    data_sources = _data_sources(_StaticSourceFetcher(b"genuinely fetched content"))
    source = Source(id="src-1", type=SourceType.NEWS)
    data_sources.ingest_source(source)

    metadata = data_sources.track_source_reliability_metadata(source)

    assert metadata.reliability == 1.0


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
def test_register_ingest_retrieve_round_trip_against_real_postgres():
    from infrastructure_postgres import DefaultInfrastructure

    data_sources = DefaultDataSources(
        infrastructure=DefaultInfrastructure(),
        source_fetcher=_StaticSourceFetcher(b"real postgres round trip"),
    )
    source = Source(id=f"test-src-{uuid.uuid4().hex}", type=SourceType.EARNINGS)

    data_sources.register_source(source)
    data_sources.ingest_source(source)
    retrieved = data_sources.retrieve_source(source.id)
    found = data_sources.discover_source({"type": "EARNINGS", "source_id": source.id})

    assert retrieved.content == b"real postgres round trip"
    assert found == [source]
