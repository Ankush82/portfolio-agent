"""Data Processing & Quality (component 03) — turns raw source into
trustworthy structured data.

Interfaces: <- Data & Sources (02), -> Knowledge & Entity Model (04).

Design: ADR-0032 (real mechanism: structural parse/extract, rule-based
normalize/transform/deduplicate/validate, a genuine multi-signal
quality score, a real freshness check, and `DefaultInfrastructure`-
backed lineage tracking). `RawDocument` gained a `fetched_at` field in
this pass to actually carry forward what `SourceDocument` (component
02) produces — see ADR-0032's Context for why that dataclass extension
was necessary, not optional.

Every real method here is structural/rule-based, no LLM involved.
`extract`'s structural (already-labeled field) extraction is real;
genuine semantic/entity extraction from unstructured prose would need
real language understanding, which is the same LLM-provider gap named
in ADR-0021 (Agent Runtime) — not re-litigated here, see `extract`'s
own docstring.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cross_cutting.observability import traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate, Provenance
from infrastructure import Infrastructure
from infrastructure_postgres import DefaultInfrastructure


@dataclass
class RawDocument:
    source_id: str
    content: bytes
    fetched_at: str = ""


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


# --- DefaultDataProcessingQuality: real, structural/rule-based mechanism ---
#
# ADR-0032. Tables this component owns:
#   "data_lineage"           — one row per document_id, an ordered list of
#                               {"step", "at"} entries, appended to by every
#                               real pipeline step below except
#                               track_data_lineage itself (a pure read).
#   "data_processing_dedup"  — one row per document_id, its content hash,
#                               used by deduplicate() to find prior documents
#                               with identical (non-metadata) content.
#
# Field-key convention: every StructuredData.fields dict may carry reserved,
# non-content metadata keys alongside real extracted content — "provenance"
# (ADR-0003 tag), "_fetched_at" (carried forward from RawDocument for
# detect_stale_data), "_duplicate_of"/"_content_hash" (deduplicate's
# findings). _RESERVED_FIELD_KEYS is exactly that set, consulted by
# normalize/transform/validate/score_data_quality so metadata never gets
# treated as scoreable/normalizable content.

_JSON_FORMAT = "json"
_TEXT_FORMAT = "text"
_BINARY_FORMAT = "binary"

_FIELD_FETCHED_AT = "_fetched_at"
_FIELD_PROVENANCE = "provenance"
_FIELD_DUPLICATE_OF = "_duplicate_of"
_FIELD_CONTENT_HASH = "_content_hash"
_RESERVED_FIELD_KEYS = frozenset(
    {_FIELD_FETCHED_AT, _FIELD_PROVENANCE, _FIELD_DUPLICATE_OF, _FIELD_CONTENT_HASH}
)

_LINEAGE_TABLE = "data_lineage"
_DEDUP_TABLE = "data_processing_dedup"

# Component 02's table/row-id convention (c02_data_sources.py's
# _SOURCES_TABLE and _metadata_record_id) — read-only coupling so
# score_data_quality can pull a real reliability signal through the shared
# Infrastructure interface (ADR-0019: never a driver directly) rather than
# inventing a duplicate reliability concept. If component 02 hasn't ingested
# this source_id yet, no row exists and _NEUTRAL_RELIABILITY is used instead
# of failing — "if available", exactly as this round's brief specified.
_C02_SOURCES_TABLE = "sources"
_NEUTRAL_RELIABILITY = 0.5

_QUALITY_WEIGHT_COMPLETENESS = 0.4
_QUALITY_WEIGHT_VALIDITY = 0.3
_QUALITY_WEIGHT_RELIABILITY = 0.3

# Financial/portfolio content (news, filings, market data) is the kind of
# data this project's documents are; a day-old price or headline is
# meaningfully stale for that domain. 24 hours, overridable per instance via
# the constructor — documented default, not a silent magic number.
_DEFAULT_STALENESS_THRESHOLD_SECONDS = 24 * 60 * 60

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches c02's time.strftime format

_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER_PATTERN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
_LABELED_FIELD_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s*(.+)$")
_NUMERIC_STRING_PATTERN = re.compile(r"^\$?-?\d[\d,]*(?:\.\d+)?$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_structure(content: bytes) -> dict:
    """Real structural parsing: plain text and JSON are genuinely
    handled; anything that doesn't decode as UTF-8 is reported as
    binary rather than guessed at. No PDF/HTML dependency added — no
    genuine need for one exists yet (ADR-0032)."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {"format": _BINARY_FORMAT, "byte_count": len(content)}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        lines = text.splitlines()
        return {
            "format": _TEXT_FORMAT,
            "text": text,
            "line_count": len(lines),
            "char_count": len(text),
        }
    return {"format": _JSON_FORMAT, "data": data}


def _extract_text_fields(text: str) -> dict:
    """Structural (pattern-matching) extraction only — dates via
    ISO-8601 regex, numbers via a digit/currency regex (run after
    dates are stripped out, so a date's year/month/day components
    don't also get counted as loose numbers), and already-labeled
    "Key: Value" lines. No semantic understanding of what these values
    mean — see this module's docstring and `extract`'s own docstring
    for the ADR-0021 boundary."""
    dates = _DATE_PATTERN.findall(text)
    text_without_dates = _DATE_PATTERN.sub(" ", text)
    numbers = _NUMBER_PATTERN.findall(text_without_dates)
    labeled_fields = {}
    for line in text.splitlines():
        match = _LABELED_FIELD_PATTERN.match(line.strip())
        if match:
            labeled_fields[match.group(1).strip()] = match.group(2).strip()
    return {"dates": dates, "numbers": numbers, "labeled_fields": labeled_fields}


def _normalize_value(value):
    if isinstance(value, str):
        stripped = value.strip()
        if _NUMERIC_STRING_PATTERN.match(stripped):
            return float(stripped.replace("$", "").replace(",", ""))
        return stripped
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _flatten(value, prefix: str = "") -> dict:
    """Canonical schema shaping for transform(): nested dicts become
    dot-notation keys (e.g. "labeled_fields.Revenue"); lists and
    scalars are left as-is at their own key."""
    if isinstance(value, dict):
        flat: dict = {}
        for key, item in value.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(item, child_key))
        return flat
    return {prefix: value}


def _content_fields(fields: dict) -> dict:
    """`fields` minus the reserved metadata keys — the actual
    extracted/processed content, used by validate/score/dedup so
    metadata never gets mistaken for scoreable content."""
    return {key: value for key, value in fields.items() if key not in _RESERVED_FIELD_KEYS}


def _content_hash(fields: dict) -> str:
    canonical = json.dumps(_content_fields(fields), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_valid(fields: dict) -> bool:
    """The structural validation rules validate() and
    score_data_quality() both rely on (factored out so scoring doesn't
    silently double-log a "validate" lineage step — see
    DefaultDataProcessingQuality.score_data_quality's docstring):
    at least one real content field, the ADR-0003 provenance tag
    present and UNTRUSTED (proof this data actually passed through
    extract()'s tagging rather than being hand-assembled), every
    extracted date matching ISO-8601, and every extracted number
    actually parseable as a number."""
    content = _content_fields(fields)
    if not content:
        return False
    if fields.get(_FIELD_PROVENANCE) != Provenance.UNTRUSTED.name:
        return False
    dates = content.get("dates")
    if isinstance(dates, list) and not all(
        isinstance(entry, str) and _ISO_DATE_PATTERN.match(entry) for entry in dates
    ):
        return False
    numbers = content.get("numbers")
    if isinstance(numbers, list):
        for entry in numbers:
            try:
                float(str(entry).replace("$", "").replace(",", ""))
            except ValueError:
                return False
    return True


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


class DefaultDataProcessingQuality:
    """Real implementation of DataProcessingQuality (ADR-0032).

    parse/extract/normalize/transform/deduplicate/validate are all
    structural/rule-based — no LLM anywhere in this class.
    score_data_quality and detect_stale_data are genuine computed
    signals, not constants. track_data_lineage is `Infrastructure`-
    backed (never an in-memory dict): every other method here appends
    its own name to a persisted, ordered lineage record as it
    completes, and track_data_lineage reads that record back.

    document_id == source_id throughout this component's pipeline —
    the identity a raw document is registered under in Data & Sources
    (02) never changes as it moves through parse/extract/.../score,
    so lineage, dedup, and quality-score records all key off the same
    string.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        boundary_gate: BoundaryGate | None = None,
        staleness_threshold_seconds: float = _DEFAULT_STALENESS_THRESHOLD_SECONDS,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._staleness_threshold_seconds = staleness_threshold_seconds

    def _record_lineage_step(self, document_id: str, step: str) -> None:
        existing = self._infrastructure.retrieve(_LINEAGE_TABLE, document_id)
        steps = list(existing["steps"]) if existing else []
        steps.append({"step": step, "at": datetime.now().strftime(_TIMESTAMP_FORMAT)})
        self._infrastructure.store(
            _LINEAGE_TABLE,
            {"id": document_id, "document_id": document_id, "steps": steps},
        )

    def parse(self, raw: RawDocument) -> ParsedDocument:
        with traced("DefaultDataProcessingQuality.parse"):
            structure = _parse_structure(raw.content)
            structure[_FIELD_FETCHED_AT] = raw.fetched_at
            tagged_structure = self._boundary_gate.tag_provenance(structure, source="parsed_document")
            self._record_lineage_step(raw.source_id, "parse")
            return ParsedDocument(source_id=raw.source_id, structure=tagged_structure)

    def extract(self, parsed: ParsedDocument) -> StructuredData:
        """Structural field extraction only: already-labeled JSON keys
        pass through directly; text content is mined via regex for
        dates/numbers/labeled lines. This is NOT semantic/entity
        extraction from unstructured prose — genuine understanding of
        what a passage of prose is actually saying needs an LLM, which
        is the same provider gap ADR-0021 already named for Agent
        Runtime. Deeper extraction depends on that gap's resolution;
        what's implemented here is the honest, real, structural
        extraction available today."""
        with traced("DefaultDataProcessingQuality.extract"):
            structure = parsed.structure
            document_format = structure.get("format")
            if document_format == _JSON_FORMAT:
                data = structure.get("data")
                fields = dict(data) if isinstance(data, dict) else {"value": data}
            elif document_format == _TEXT_FORMAT:
                fields = _extract_text_fields(structure.get("text", ""))
            else:
                fields = {"byte_count": structure.get("byte_count", 0)}
            fields[_FIELD_FETCHED_AT] = structure.get(_FIELD_FETCHED_AT, "")
            tagged_fields = self._boundary_gate.tag_provenance(fields, source="extracted_data")
            self._record_lineage_step(parsed.source_id, "extract")
            return StructuredData(source_id=parsed.source_id, fields=tagged_fields)

    def normalize(self, data: StructuredData) -> StructuredData:
        with traced("DefaultDataProcessingQuality.normalize"):
            normalized_fields = {
                key: (value if key in _RESERVED_FIELD_KEYS else _normalize_value(value))
                for key, value in data.fields.items()
            }
            self._record_lineage_step(data.source_id, "normalize")
            return StructuredData(source_id=data.source_id, fields=normalized_fields)

    def transform(self, data: StructuredData) -> StructuredData:
        with traced("DefaultDataProcessingQuality.transform"):
            reserved = {key: value for key, value in data.fields.items() if key in _RESERVED_FIELD_KEYS}
            content = {key: value for key, value in data.fields.items() if key not in _RESERVED_FIELD_KEYS}
            transformed_fields = {**_flatten(content), **reserved}
            self._record_lineage_step(data.source_id, "transform")
            return StructuredData(source_id=data.source_id, fields=transformed_fields)

    def deduplicate(self, data: StructuredData) -> StructuredData:
        """Exact-duplicate detection via a real content hash (SHA-256
        over the canonicalized, metadata-stripped fields), checked
        against every other document this instance has ever
        deduplicated. Not near-duplicate/similarity detection — see
        ADR-0032's Alternatives for why exact hashing was chosen for
        this pass."""
        with traced("DefaultDataProcessingQuality.deduplicate"):
            content_hash = _content_hash(data.fields)
            existing_matches = self._infrastructure.query(_DEDUP_TABLE, {"content_hash": content_hash})
            duplicate_of = next(
                (row["source_id"] for row in existing_matches if row["source_id"] != data.source_id),
                None,
            )
            updated_fields = dict(data.fields)
            updated_fields[_FIELD_CONTENT_HASH] = content_hash
            if duplicate_of is not None:
                updated_fields[_FIELD_DUPLICATE_OF] = duplicate_of
            self._infrastructure.store(
                _DEDUP_TABLE,
                {
                    "id": f"dedup::{data.source_id}",
                    "source_id": data.source_id,
                    "content_hash": content_hash,
                },
            )
            self._record_lineage_step(data.source_id, "deduplicate")
            return StructuredData(source_id=data.source_id, fields=updated_fields)

    def validate(self, data: StructuredData) -> bool:
        with traced("DefaultDataProcessingQuality.validate"):
            result = _is_valid(data.fields)
            self._record_lineage_step(data.source_id, "validate")
            return result

    def score_data_quality(self, data: StructuredData) -> DataQualityScore:
        """Weighted sum of three real, independently computed signals,
        each in [0, 1]: completeness (fraction of content fields that
        are actually populated), structural validity (`_is_valid`,
        the same rules `validate()` applies — reused directly here
        rather than calling `self.validate()`, so scoring doesn't
        silently double-log an extra "validate" lineage step for a
        check that was never separately requested), and source
        reliability (read from component 02's persisted reliability
        metadata when available, else a neutral 0.5 — never a
        hardcoded score)."""
        with traced("DefaultDataProcessingQuality.score_data_quality"):
            content = _content_fields(data.fields)
            populated = sum(1 for value in content.values() if value not in (None, "", [], {}))
            completeness = populated / len(content) if content else 0.0
            validity = 1.0 if _is_valid(data.fields) else 0.0
            reliability_row = self._infrastructure.retrieve(_C02_SOURCES_TABLE, f"metadata::{data.source_id}")
            reliability = reliability_row["reliability"] if reliability_row else _NEUTRAL_RELIABILITY
            score = (
                _QUALITY_WEIGHT_COMPLETENESS * completeness
                + _QUALITY_WEIGHT_VALIDITY * validity
                + _QUALITY_WEIGHT_RELIABILITY * reliability
            )
            self._record_lineage_step(data.source_id, "score_data_quality")
            return DataQualityScore(document_id=data.source_id, score=score)

    def detect_stale_data(self, data: StructuredData) -> bool:
        """Real freshness check: age of `_fetched_at` (carried forward
        from RawDocument by parse/extract) against
        `_staleness_threshold_seconds` (default 24h — see this
        module's constant for why). A missing or unparseable timestamp
        is treated as stale, not fresh — unknown age is the
        conservative case for financial data, not a free pass."""
        with traced("DefaultDataProcessingQuality.detect_stale_data"):
            fetched_at = data.fields.get(_FIELD_FETCHED_AT, "")
            parsed_timestamp = _parse_timestamp(fetched_at) if fetched_at else None
            if parsed_timestamp is None:
                is_stale = True
            else:
                age_seconds = (datetime.now() - parsed_timestamp).total_seconds()
                is_stale = age_seconds > self._staleness_threshold_seconds
            self._record_lineage_step(data.source_id, "detect_stale_data")
            return is_stale

    def track_data_lineage(self, data: StructuredData) -> DataLineage:
        """Pure read: returns the lineage actually persisted so far
        for `data.source_id`, built up by every other method's real
        `_record_lineage_step` call as it runs. Does not append itself
        as a step — track_data_lineage observes the pipeline, it isn't
        one of its transforming steps."""
        with traced("DefaultDataProcessingQuality.track_data_lineage"):
            row = self._infrastructure.retrieve(_LINEAGE_TABLE, data.source_id)
            steps = [entry["step"] for entry in row["steps"]] if row else []
            return DataLineage(document_id=data.source_id, steps=steps)
