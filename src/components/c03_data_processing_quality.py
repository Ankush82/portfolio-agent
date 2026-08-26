"""Data Processing & Quality (component 03) — turns raw source into
trustworthy structured data.

Whiteboard-level only (Component Whiteboards artifact, card 03) — no
low-level design or ADRs yet. Interfaces: <- Data & Sources (02),
-> Knowledge & Entity Model (04).
"""

from dataclasses import dataclass
from typing import Protocol

from cross_cutting.observability import traced


@dataclass
class RawDocument:
    source_id: str
    content: bytes


@dataclass
class ParsedDocument:
    source_id: str
    structure: dict


@dataclass
class StructuredData:
    source_id: str
    fields: dict


@dataclass
class DataQualityScore:
    document_id: str
    score: float


@dataclass
class DataLineage:
    document_id: str
    steps: list[str]


class DataProcessingQuality(Protocol):
    def parse(self, raw: RawDocument) -> ParsedDocument:
        ...

    def extract(self, parsed: ParsedDocument) -> StructuredData:
        ...

    def normalize(self, data: StructuredData) -> StructuredData:
        ...

    def transform(self, data: StructuredData) -> StructuredData:
        ...

    def deduplicate(self, data: StructuredData) -> StructuredData:
        ...

    def validate(self, data: StructuredData) -> bool:
        ...

    def score_data_quality(self, data: StructuredData) -> DataQualityScore:
        ...

    def detect_stale_data(self, data: StructuredData) -> bool:
        ...

    def track_data_lineage(self, data: StructuredData) -> DataLineage:
        ...


class StubDataProcessingQuality:
    """Structural implementation of DataProcessingQuality. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def parse(self, raw: RawDocument) -> ParsedDocument:
        with traced("StubDataProcessingQuality.parse"):
            return ParsedDocument(source_id="stub-id", structure={})

    def extract(self, parsed: ParsedDocument) -> StructuredData:
        with traced("StubDataProcessingQuality.extract"):
            return StructuredData(source_id="stub-id", fields={})

    def normalize(self, data: StructuredData) -> StructuredData:
        with traced("StubDataProcessingQuality.normalize"):
            return StructuredData(source_id="stub-id", fields={})

    def transform(self, data: StructuredData) -> StructuredData:
        with traced("StubDataProcessingQuality.transform"):
            return StructuredData(source_id="stub-id", fields={})

    def deduplicate(self, data: StructuredData) -> StructuredData:
        with traced("StubDataProcessingQuality.deduplicate"):
            return StructuredData(source_id="stub-id", fields={})

    def validate(self, data: StructuredData) -> bool:
        with traced("StubDataProcessingQuality.validate"):
            return True

    def score_data_quality(self, data: StructuredData) -> DataQualityScore:
        with traced("StubDataProcessingQuality.score_data_quality"):
            return DataQualityScore(document_id="stub-id", score=0.0)

    def detect_stale_data(self, data: StructuredData) -> bool:
        with traced("StubDataProcessingQuality.detect_stale_data"):
            return True

    def track_data_lineage(self, data: StructuredData) -> DataLineage:
        with traced("StubDataProcessingQuality.track_data_lineage"):
            return DataLineage(document_id="stub-id", steps=[])
