"""Data & Sources (component 02) — the external-world ingestion layer.

Whiteboard-level only (Component Whiteboards artifact, card 02) — no
low-level design or ADRs yet. Interface: -> Data Processing & Quality
(component 03), raw source documents.
"""

from dataclasses import dataclass, field
from enum import Enum, auto


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


class DataSources:
    def register_source(self, source: Source) -> None:
        raise NotImplementedError

    def discover_source(self, criteria: dict) -> list[Source]:
        raise NotImplementedError

    def ingest_source(self, source: Source) -> SourceDocument:
        raise NotImplementedError

    def retrieve_source(self, source_id: str) -> SourceDocument | None:
        raise NotImplementedError

    def update_source(self, source: Source) -> SourceSnapshot:
        raise NotImplementedError

    def track_source_provenance(self, document: SourceDocument) -> Provenance:
        raise NotImplementedError

    def track_source_timestamp(self, document: SourceDocument) -> str:
        raise NotImplementedError

    def track_source_reliability_metadata(self, source: Source) -> SourceMetadata:
        raise NotImplementedError
