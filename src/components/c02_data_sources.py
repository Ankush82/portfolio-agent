"""Data & Sources (component 02) — the external-world ingestion layer.

Whiteboard-level only (Component Whiteboards artifact, card 02) — no
low-level design or ADRs yet. Interface: -> Data Processing & Quality
(component 03), raw source documents.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from cross_cutting.observability import traced


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
