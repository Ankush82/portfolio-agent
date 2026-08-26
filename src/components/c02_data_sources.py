"""Data & Sources (component 02) — the external-world ingestion layer.

Interface: -> Data Processing & Quality (component 03), raw source
documents.

Design: ADR-0026 (real mechanism: persistence, provenance/timestamp/
reliability wiring, SourceFetcher as the one seam to the outside
world), ADR-0027 (external fetch provider choice — Proposed, not yet
Accepted; see PlaceholderSourceFetcher below), ADR-0046 (MARKET_DATA/
NEWS/EARNINGS resolved via Alpha Vantage — see AlphaVantageSourceFetcher
below; FILING/REPORT/PRESENTATION/EXTERNAL_DATASET remain on the
placeholder).

register_source/discover_source/retrieve_source/update_source and all
three tracking capabilities are real, `Infrastructure`-backed logic
(ADR-0026). ingest_source genuinely needs a live news/filing/
market-data API to do real work. `get_source_fetcher()`
(`src/alpha_vantage_client.py`) resolves `DefaultDataSources`'s default
`SourceFetcher`: `AlphaVantageSourceFetcher` when `ALPHA_VANTAGE_API_KEY`
is configured (real for MARKET_DATA/NEWS/EARNINGS, ADR-0046),
`PlaceholderSourceFetcher` otherwise — an explicit, unmistakable
non-fetch for every other source type and for every source type when no
key is configured at all (ADR-0027, same pattern as ADR-0021's original
`placeholder_reason_fn` for Agent Runtime).
"""

import base64
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from alpha_vantage_client import fetch_alpha_vantage, get_api_key as get_alpha_vantage_api_key
from cross_cutting.observability import traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


class SourceType(Enum):
    NEWS = auto()
    FILING = auto()
    REPORT = auto()
    EARNINGS = auto()
    PRESENTATION = auto()
    MARKET_DATA = auto()
    EXTERNAL_DATASET = auto()


@dataclass
class Source:
    id: str
    type: SourceType


@dataclass
class SourceDocument:
    source_id: str
    content: bytes
    fetched_at: str


@dataclass
class SourceSnapshot:
    source_id: str
    state: dict


@dataclass
class Provenance:
    source_id: str
    origin: str


@dataclass
class SourceMetadata:
    source_id: str
    timestamp: str
    reliability: float


class DataSources(Protocol):
    def register_source(self, source: Source) -> None:
        ...

    def discover_source(self, criteria: dict) -> list[Source]:
        ...

    def ingest_source(self, source: Source) -> SourceDocument:
        ...

    def retrieve_source(self, source_id: str) -> SourceDocument | None:
        ...

    def update_source(self, source: Source) -> SourceSnapshot:
        ...

    def track_source_provenance(self, document: SourceDocument) -> Provenance:
        ...

    def track_source_timestamp(self, document: SourceDocument) -> str:
        ...

    def track_source_reliability_metadata(self, source: Source) -> SourceMetadata:
        ...


class StubDataSources:
    """Structural implementation of DataSources. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def register_source(self, source: Source) -> None:
        with traced("StubDataSources.register_source"):
            return None

    def discover_source(self, criteria: dict) -> list[Source]:
        with traced("StubDataSources.discover_source"):
            return []

    def ingest_source(self, source: Source) -> SourceDocument:
        with traced("StubDataSources.ingest_source"):
            return SourceDocument(source_id="stub-id", content=b"", fetched_at="")

    def retrieve_source(self, source_id: str) -> SourceDocument | None:
        with traced("StubDataSources.retrieve_source"):
            return None

    def update_source(self, source: Source) -> SourceSnapshot:
        with traced("StubDataSources.update_source"):
            return SourceSnapshot(source_id="stub-id", state={})

    def track_source_provenance(self, document: SourceDocument) -> Provenance:
        with traced("StubDataSources.track_source_provenance"):
            return Provenance(source_id="stub-id", origin="stub")

    def track_source_timestamp(self, document: SourceDocument) -> str:
        with traced("StubDataSources.track_source_timestamp"):
            return ""

    def track_source_reliability_metadata(self, source: Source) -> SourceMetadata:
        with traced("StubDataSources.track_source_reliability_metadata"):
            return SourceMetadata(source_id="stub-id", timestamp="", reliability=0.0)


_SOURCES_TABLE = "sources"

# Distinguishes the different record shapes this component stores in
# the single "sources" table (ADR-0026) without needing a table per
# entity kind: a Source registration, its most recently ingested
# SourceDocument, its provenance tag, its tracked timestamp, and its
# reliability metadata all key off the same source_id, so each gets
# its own namespaced row id rather than colliding on one.
def _source_record_id(source_id: str) -> str:
    return f"source::{source_id}"


def _document_record_id(source_id: str) -> str:
    return f"document::{source_id}"


def _provenance_record_id(source_id: str) -> str:
    return f"provenance::{source_id}"


def _timestamp_record_id(source_id: str) -> str:
    return f"timestamp::{source_id}"


def _metadata_record_id(source_id: str) -> str:
    return f"metadata::{source_id}"


# Unmistakably synthetic: never empty bytes, which could be confused
# with a legitimate empty response from a real source. Referenced by
# both PlaceholderSourceFetcher (below) and
# DefaultDataSources.track_source_reliability_metadata, which scores a
# document carrying this exact marker as low-reliability precisely
# because it is not real fetched content.
PLACEHOLDER_FETCH_MARKER = (
    b"PLACEHOLDER_SOURCE_FETCHER: no real news/filing/market-data API "
    b"is wired in yet -- see adr/0027-data-sources-fetch-provider-interim.md. "
    b"This is not a real document."
)

_RELIABILITY_NO_DOCUMENT = 0.0
_RELIABILITY_SYNTHETIC_DOCUMENT = 0.3
_RELIABILITY_REAL_DOCUMENT = 1.0


class SourceFetcher(Protocol):
    """The one seam `DefaultDataSources.ingest_source` calls through to
    reach the outside world (ADR-0026). A real implementation needs a
    live credential: a market-data API for SourceType.MARKET_DATA, a
    filings/regulatory feed for SourceType.FILING/EARNINGS/PRESENTATION/
    REPORT, a news API for SourceType.NEWS, and something
    dataset-specific for SourceType.EXTERNAL_DATASET. See ADR-0027
    (status Proposed) for the real options per source type and their
    honest tradeoffs. `AlphaVantageSourceFetcher` (ADR-0046, below)
    resolves MARKET_DATA/NEWS/EARNINGS for real; FILING/REPORT/
    PRESENTATION/EXTERNAL_DATASET remain genuinely open.

    Swapping `PlaceholderSourceFetcher` for a real implementation
    behind this same interface is the entire fix once credentials
    exist: nothing else in `DefaultDataSources` needs to change."""

    def fetch(self, source: Source) -> SourceDocument:
        ...


class PlaceholderSourceFetcher:
    """Explicitly NOT a real fetch — see `SourceFetcher`'s docstring
    and ADR-0027 (status Proposed). Returns a synthetic `SourceDocument`
    for every `Source`/`SourceType` alike, whose content is the
    unmistakable `PLACEHOLDER_FETCH_MARKER` rather than empty bytes
    that could pass for a legitimate empty response. `fetched_at` is a
    real timestamp (when this placeholder ran) — that part is honest
    bookkeeping even though the content is not."""

    def fetch(self, source: Source) -> SourceDocument:
        with traced("PlaceholderSourceFetcher.fetch"):
            return SourceDocument(
                source_id=source.id,
                content=PLACEHOLDER_FETCH_MARKER,
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )


# Alpha Vantage's real, free-tier endpoint per SourceType this vendor
# actually covers (ADR-0046). FILING/REPORT/PRESENTATION/EXTERNAL_DATASET
# have no entry here on purpose — see AlphaVantageSourceFetcher's
# docstring for what happens for those.
_ALPHA_VANTAGE_FUNCTION_BY_SOURCE_TYPE = {
    SourceType.MARKET_DATA: "GLOBAL_QUOTE",
    SourceType.NEWS: "NEWS_SENTIMENT",
    SourceType.EARNINGS: "EARNINGS",
}


class AlphaVantageSourceFetcher:
    """Real, partial `SourceFetcher` (ADR-0046) — resolves the
    MARKET_DATA/NEWS/EARNINGS slice of ADR-0027's fetch-provider gap via
    Alpha Vantage's free-tier API (`src/alpha_vantage_client.py`).
    `source.id` is read as the ticker symbol Alpha Vantage's
    `symbol`/`tickers` parameter expects — the natural interpretation
    given `Source` carries no separate symbol field, and the shape every
    caller of `DefaultDataSources` already uses `source.id` for
    everywhere else in this file (the `_SOURCES_TABLE` row key).

    For the four source types Alpha Vantage's free tier has no real
    equivalent for (FILING, REPORT, PRESENTATION, EXTERNAL_DATASET —
    see ADR-0027's own per-source-type alternatives, e.g. SEC EDGAR for
    filings), this delegates to `fallback` (`PlaceholderSourceFetcher`
    by default) rather than guessing at an unrelated vendor or raising
    — a real, honest partial resolution, not a full one. ADR-0046 names
    the remaining gap plainly rather than papering over it."""

    def __init__(self, fallback: "SourceFetcher | None" = None) -> None:
        self._fallback = fallback or PlaceholderSourceFetcher()

    def fetch(self, source: Source) -> SourceDocument:
        with traced("AlphaVantageSourceFetcher.fetch"):
            function = _ALPHA_VANTAGE_FUNCTION_BY_SOURCE_TYPE.get(source.type)
            if function is None:
                return self._fallback.fetch(source)
            param_name = "tickers" if source.type is SourceType.NEWS else "symbol"
            content = fetch_alpha_vantage(function, **{param_name: source.id})
            return SourceDocument(
                source_id=source.id,
                content=content,
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )


def get_source_fetcher(placeholder: "SourceFetcher | None" = None) -> "SourceFetcher":
    """Selection function — the same `Default*`-vs-`Stub*`-shaped choice
    `src/llm.py`'s `get_reason_fn` already established, applied here to
    the data-fetching seam instead of the reasoning one. Returns
    `AlphaVantageSourceFetcher` (wrapping `placeholder` as its own
    fallback for the source types it doesn't cover) when
    `ALPHA_VANTAGE_API_KEY` is configured, otherwise returns
    `placeholder` unchanged. Unlike `Mem0EntityLinker` (ADR-0045),
    constructing `AlphaVantageSourceFetcher` never touches the network
    — only calling `.fetch()` does — so it is safe for
    `DefaultDataSources` to resolve this automatically at construction
    time, the same way `build_agent_runtime_graph` resolves `reason_fn`."""
    placeholder = placeholder or PlaceholderSourceFetcher()
    if get_alpha_vantage_api_key() is not None:
        return AlphaVantageSourceFetcher(fallback=placeholder)
    return placeholder


class DefaultDataSources:
    """Real implementation of DataSources (ADR-0026).

    register_source/discover_source/retrieve_source/update_source and
    all three tracking methods are real, backed by `Infrastructure`'s
    store/retrieve/query (never a database driver directly, per
    ADR-0019) against the single `_SOURCES_TABLE` ("sources") table.

    ingest_source calls through the injected `SourceFetcher`, which
    defaults to whatever `get_source_fetcher()` resolves —
    `AlphaVantageSourceFetcher` (real for MARKET_DATA/NEWS/EARNINGS,
    ADR-0046) when `ALPHA_VANTAGE_API_KEY` is configured,
    `PlaceholderSourceFetcher` otherwise; see those classes and
    ADR-0027/ADR-0046 for exactly why the remaining source types stay
    on the placeholder. Every `SourceDocument` this class
    produces (via ingest_source or update_source) is tagged UNTRUSTED
    through `BoundaryGate.tag_provenance` before it is stored, not
    just on request — ADR-0003 requires that tag before document
    content can be reasoned over elsewhere in the system, and this
    wires it in at the point of production, not as an afterthought.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        boundary_gate: BoundaryGate | None = None,
        source_fetcher: SourceFetcher | None = None,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._source_fetcher = source_fetcher or get_source_fetcher()

    def register_source(self, source: Source) -> None:
        with traced("DefaultDataSources.register_source"):
            self._infrastructure.store(
                _SOURCES_TABLE,
                {
                    "id": _source_record_id(source.id),
                    "record_kind": "source",
                    "source_id": source.id,
                    "type": source.type.name,
                },
            )

    def discover_source(self, criteria: dict) -> list[Source]:
        """`criteria` matches against the stored record shape
        (e.g. `{"type": "NEWS"}`, matching `SourceType.NEWS.name`) —
        `Infrastructure.query`'s JSONB containment match, same
        constraint documented on `infrastructure_postgres.py`."""
        with traced("DefaultDataSources.discover_source"):
            filters = {"record_kind": "source", **criteria}
            rows = self._infrastructure.query(_SOURCES_TABLE, filters)
            return [
                Source(id=row["source_id"], type=SourceType[row["type"]])
                for row in rows
            ]

    def ingest_source(self, source: Source) -> SourceDocument:
        with traced("DefaultDataSources.ingest_source"):
            document = self._source_fetcher.fetch(source)
            self.track_source_provenance(document)
            self.track_source_timestamp(document)
            self._infrastructure.store(
                _SOURCES_TABLE,
                {
                    "id": _document_record_id(document.source_id),
                    "record_kind": "source_document",
                    "source_id": document.source_id,
                    "content": base64.b64encode(document.content).decode("ascii"),
                    "fetched_at": document.fetched_at,
                    "synthetic": document.content == PLACEHOLDER_FETCH_MARKER,
                },
            )
            return document

    def retrieve_source(self, source_id: str) -> SourceDocument | None:
        with traced("DefaultDataSources.retrieve_source"):
            row = self._infrastructure.retrieve(_SOURCES_TABLE, _document_record_id(source_id))
            if row is None:
                return None
            return SourceDocument(
                source_id=row["source_id"],
                content=base64.b64decode(row["content"]),
                fetched_at=row["fetched_at"],
            )

    def update_source(self, source: Source) -> SourceSnapshot:
        """Re-ingests `source` for real and reports the transition:
        whether content actually changed since the previously stored
        document, not a hardcoded snapshot."""
        with traced("DefaultDataSources.update_source"):
            previous = self.retrieve_source(source.id)
            current = self.ingest_source(source)
            state = {
                "previous_fetched_at": previous.fetched_at if previous else None,
                "current_fetched_at": current.fetched_at,
                "content_changed": previous is None or previous.content != current.content,
            }
            return SourceSnapshot(source_id=source.id, state=state)

    def track_source_provenance(self, document: SourceDocument) -> Provenance:
        """Tags `document` UNTRUSTED via `BoundaryGate.tag_provenance`
        (ADR-0003, ADR-0026) and persists the tag — callable both
        internally (every document ingest_source/update_source
        produces goes through this) and independently by any caller
        that already holds a SourceDocument."""
        with traced("DefaultDataSources.track_source_provenance"):
            tagged = self._boundary_gate.tag_provenance(
                {"source_id": document.source_id, "fetched_at": document.fetched_at},
                source="source_document",
            )
            self._infrastructure.store(
                _SOURCES_TABLE,
                {
                    "id": _provenance_record_id(document.source_id),
                    "record_kind": "provenance",
                    "source_id": document.source_id,
                    "origin": tagged["provenance"],
                },
            )
            return Provenance(source_id=document.source_id, origin=tagged["provenance"])

    def track_source_timestamp(self, document: SourceDocument) -> str:
        with traced("DefaultDataSources.track_source_timestamp"):
            self._infrastructure.store(
                _SOURCES_TABLE,
                {
                    "id": _timestamp_record_id(document.source_id),
                    "record_kind": "timestamp",
                    "source_id": document.source_id,
                    "fetched_at": document.fetched_at,
                },
            )
            return document.fetched_at

    def track_source_reliability_metadata(self, source: Source) -> SourceMetadata:
        """Reliability is a real signal computed from what's actually
        been persisted for this source, not a hardcoded constant: no
        ingested document at all scores lowest, a document that is
        `PLACEHOLDER_FETCH_MARKER` (synthetic — see
        PlaceholderSourceFetcher) scores low but non-zero (something
        was attempted), and a document that is genuinely fetched
        content scores highest. With only `PlaceholderSourceFetcher`
        wired in today, no source can score `_RELIABILITY_REAL_DOCUMENT`
        yet — that's the honest consequence of ADR-0027 still being
        open, not a bug here."""
        with traced("DefaultDataSources.track_source_reliability_metadata"):
            row = self._infrastructure.retrieve(_SOURCES_TABLE, _document_record_id(source.id))
            if row is None:
                metadata = SourceMetadata(source_id=source.id, timestamp="", reliability=_RELIABILITY_NO_DOCUMENT)
            else:
                reliability = (
                    _RELIABILITY_SYNTHETIC_DOCUMENT if row.get("synthetic") else _RELIABILITY_REAL_DOCUMENT
                )
                metadata = SourceMetadata(source_id=source.id, timestamp=row["fetched_at"], reliability=reliability)
            self._infrastructure.store(
                _SOURCES_TABLE,
                {
                    "id": _metadata_record_id(source.id),
                    "record_kind": "reliability_metadata",
                    "source_id": source.id,
                    "timestamp": metadata.timestamp,
                    "reliability": metadata.reliability,
                },
            )
            return metadata
