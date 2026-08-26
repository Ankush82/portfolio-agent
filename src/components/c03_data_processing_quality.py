"""Data Processing & Quality (component 03) — turns raw source into
trustworthy structured data.

Whiteboard-level only (Component Whiteboards artifact, card 03) — no
low-level design or ADRs yet. Interfaces: <- Data & Sources (02),
-> Knowledge & Entity Model (04).
"""

from dataclasses import dataclass


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


class DataProcessingQuality:
    def parse(self, raw: RawDocument) -> ParsedDocument:
        raise NotImplementedError

    def extract(self, parsed: ParsedDocument) -> StructuredData:
        raise NotImplementedError

    def normalize(self, data: StructuredData) -> StructuredData:
        raise NotImplementedError

    def transform(self, data: StructuredData) -> StructuredData:
        raise NotImplementedError

    def deduplicate(self, data: StructuredData) -> StructuredData:
        raise NotImplementedError

    def validate(self, data: StructuredData) -> bool:
        raise NotImplementedError

    def score_data_quality(self, data: StructuredData) -> DataQualityScore:
        raise NotImplementedError

    def detect_stale_data(self, data: StructuredData) -> bool:
        raise NotImplementedError

    def track_data_lineage(self, data: StructuredData) -> DataLineage:
        raise NotImplementedError
