"""Tests for DefaultDataProcessingQuality
(src/components/c03_data_processing_quality.py, ADR-0032).

Most tests run against `_FakeInfrastructure`, a minimal in-memory test
double of the `Infrastructure` Protocol (same shape as
tests/components/test_data_sources.py's own double), so this
component's own logic (structural parse/extract, normalize/transform/
dedup rules, quality scoring, staleness, lineage persistence) is
exercised fast and without a live Postgres. `DefaultBoundaryGate`
(cross_cutting/security.py) is used for real — it has no external
dependency — so provenance tagging is verified against real ADR-0003
behavior, not a stub.

A small number of `@requires_postgres` tests at the bottom exercise
DefaultDataProcessingQuality against the real DefaultInfrastructure,
mirroring test_data_sources.py's skip-cleanly-when-no-live-DB pattern.
"""

import json
import uuid

import pytest

from components.c02_data_sources import PLACEHOLDER_FETCH_MARKER
from components.c03_data_processing_quality import (
    DefaultDataProcessingQuality,
    ParsedDocument,
    RawDocument,
    StructuredData,
)
from cross_cutting.security import DefaultBoundaryGate


class _FakeInfrastructure:
    """Minimal in-memory double of the Infrastructure Protocol: only
    store/retrieve/query, since that's all DefaultDataProcessingQuality
    calls. Same semantics as DefaultInfrastructure: store() keys off
    record["id"] when present, query() does containment matching."""

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


def _processor(infrastructure=None, staleness_threshold_seconds=None) -> DefaultDataProcessingQuality:
    kwargs = {
        "infrastructure": infrastructure or _FakeInfrastructure(),
        "boundary_gate": DefaultBoundaryGate(),
    }
    if staleness_threshold_seconds is not None:
        kwargs["staleness_threshold_seconds"] = staleness_threshold_seconds
    return DefaultDataProcessingQuality(**kwargs)


def _now_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# --- parse -------------------------------------------------------------


def test_parse_handles_plain_text_content():
    processor = _processor()
    raw = RawDocument(source_id="doc-1", content=b"Revenue: $1,234.56\nDate: 2026-08-20", fetched_at=_now_stamp())

    parsed = processor.parse(raw)

    assert parsed.structure["format"] == "text"
    assert parsed.structure["text"] == "Revenue: $1,234.56\nDate: 2026-08-20"
    assert parsed.structure["line_count"] == 2


def test_parse_handles_json_content():
    processor = _processor()
    raw = RawDocument(source_id="doc-1", content=json.dumps({"ticker": "AAPL", "price": 190.5}).encode(), fetched_at="")

    parsed = processor.parse(raw)

    assert parsed.structure["format"] == "json"
    assert parsed.structure["data"] == {"ticker": "AAPL", "price": 190.5}


def test_parse_handles_binary_content_that_is_not_valid_utf8():
    processor = _processor()
    raw = RawDocument(source_id="doc-1", content=bytes([0xFF, 0xFE, 0x00, 0x01]), fetched_at="")

    parsed = processor.parse(raw)

    assert parsed.structure["format"] == "binary"
    assert parsed.structure["byte_count"] == 4


def test_parse_operates_on_the_actual_placeholder_source_fetcher_marker():
    """This is exactly what DefaultDataSources' PlaceholderSourceFetcher
    (component 02) produces today — parse must handle it as real
    content, not assume something richer exists."""
    processor = _processor()
    raw = RawDocument(source_id="doc-1", content=PLACEHOLDER_FETCH_MARKER, fetched_at=_now_stamp())

    parsed = processor.parse(raw)

    assert parsed.structure["format"] == "text"
    assert "PLACEHOLDER_SOURCE_FETCHER" in parsed.structure["text"]


def test_parse_carries_fetched_at_forward_into_structure():
    processor = _processor()
    raw = RawDocument(source_id="doc-1", content=b"hello", fetched_at="2026-08-20T09:00:00")

    parsed = processor.parse(raw)

    assert parsed.structure["_fetched_at"] == "2026-08-20T09:00:00"


def test_parse_tags_provenance_untrusted():
    processor = _processor()
    raw = RawDocument(source_id="doc-1", content=b"hello", fetched_at="")

    parsed = processor.parse(raw)

    assert parsed.structure["provenance"] == "UNTRUSTED"


def test_parse_records_a_lineage_step():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    raw = RawDocument(source_id="doc-1", content=b"hello", fetched_at="")

    processor.parse(raw)

    lineage = processor.track_data_lineage(StructuredData(source_id="doc-1", fields={}))
    assert lineage.steps == ["parse"]


# --- extract -------------------------------------------------------------


def test_extract_from_json_pulls_top_level_keys_directly():
    processor = _processor()
    parsed = ParsedDocument(source_id="doc-1", structure={"format": "json", "data": {"ticker": "AAPL", "price": 190.5}})

    extracted = processor.extract(parsed)

    assert extracted.fields["ticker"] == "AAPL"
    assert extracted.fields["price"] == 190.5


def test_extract_from_text_finds_dates_numbers_and_labeled_fields():
    processor = _processor()
    text = "Filing Date: 2026-08-20\nRevenue: $1,234.56\nNet income grew by 12.5 percent."
    parsed = ParsedDocument(source_id="doc-1", structure={"format": "text", "text": text})

    extracted = processor.extract(parsed)

    assert "2026-08-20" in extracted.fields["dates"]
    assert extracted.fields["labeled_fields"]["Filing Date"] == "2026-08-20"
    assert extracted.fields["labeled_fields"]["Revenue"] == "$1,234.56"
    assert any(number.strip("$").replace(",", "") == "1234.56" for number in extracted.fields["numbers"])


def test_extract_does_not_double_count_date_components_as_numbers():
    processor = _processor()
    parsed = ParsedDocument(source_id="doc-1", structure={"format": "text", "text": "Date: 2026-08-20"})

    extracted = processor.extract(parsed)

    assert "2026" not in extracted.fields["numbers"]
    assert "08" not in extracted.fields["numbers"]


def test_extract_from_binary_reports_byte_count_only():
    processor = _processor()
    parsed = ParsedDocument(source_id="doc-1", structure={"format": "binary", "byte_count": 42})

    extracted = processor.extract(parsed)

    assert extracted.fields["byte_count"] == 42


def test_extract_carries_fetched_at_and_tags_provenance():
    processor = _processor()
    parsed = ParsedDocument(
        source_id="doc-1",
        structure={"format": "json", "data": {"x": 1}, "_fetched_at": "2026-08-20T09:00:00"},
    )

    extracted = processor.extract(parsed)

    assert extracted.fields["_fetched_at"] == "2026-08-20T09:00:00"
    assert extracted.fields["provenance"] == "UNTRUSTED"


# --- normalize -----------------------------------------------------------


def test_normalize_coerces_numeric_looking_strings_to_floats():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"revenue": "$1,234.56", "provenance": "UNTRUSTED"})

    normalized = processor.normalize(data)

    assert normalized.fields["revenue"] == 1234.56


def test_normalize_strips_whitespace_from_string_values():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"ticker": "  AAPL  "})

    normalized = processor.normalize(data)

    assert normalized.fields["ticker"] == "AAPL"


def test_normalize_leaves_reserved_metadata_keys_untouched():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"provenance": "UNTRUSTED", "_fetched_at": "2026-08-20T09:00:00"})

    normalized = processor.normalize(data)

    assert normalized.fields["provenance"] == "UNTRUSTED"
    assert normalized.fields["_fetched_at"] == "2026-08-20T09:00:00"


# --- transform -------------------------------------------------------------


def test_transform_flattens_nested_dicts_into_dot_notation():
    processor = _processor()
    data = StructuredData(
        source_id="doc-1",
        fields={"labeled_fields": {"Revenue": 1234.56, "Ticker": "AAPL"}, "provenance": "UNTRUSTED"},
    )

    transformed = processor.transform(data)

    assert transformed.fields["labeled_fields.Revenue"] == 1234.56
    assert transformed.fields["labeled_fields.Ticker"] == "AAPL"
    assert "labeled_fields" not in transformed.fields
    assert transformed.fields["provenance"] == "UNTRUSTED"


def test_transform_leaves_list_values_intact():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"dates": ["2026-08-20"]})

    transformed = processor.transform(data)

    assert transformed.fields["dates"] == ["2026-08-20"]


# --- deduplicate -----------------------------------------------------------


def test_deduplicate_flags_identical_content_from_a_different_document():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    first = StructuredData(source_id="doc-1", fields={"ticker": "AAPL", "price": 190.5})
    second = StructuredData(source_id="doc-2", fields={"ticker": "AAPL", "price": 190.5})

    processor.deduplicate(first)
    result = processor.deduplicate(second)

    assert result.fields["_duplicate_of"] == "doc-1"


def test_deduplicate_does_not_flag_different_content():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    first = StructuredData(source_id="doc-1", fields={"ticker": "AAPL", "price": 190.5})
    second = StructuredData(source_id="doc-2", fields={"ticker": "MSFT", "price": 410.0})

    processor.deduplicate(first)
    result = processor.deduplicate(second)

    assert "_duplicate_of" not in result.fields


def test_deduplicate_ignores_reserved_metadata_when_hashing():
    """Two documents with identical content but different _fetched_at
    (a reserved, volatile key) must still be recognized as duplicates —
    the hash is computed over real content only."""
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    first = StructuredData(source_id="doc-1", fields={"ticker": "AAPL", "_fetched_at": "2026-08-20T09:00:00"})
    second = StructuredData(source_id="doc-2", fields={"ticker": "AAPL", "_fetched_at": "2026-08-21T09:00:00"})

    processor.deduplicate(first)
    result = processor.deduplicate(second)

    assert result.fields["_duplicate_of"] == "doc-1"


def test_deduplicate_re_running_same_document_does_not_flag_itself():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    data = StructuredData(source_id="doc-1", fields={"ticker": "AAPL"})

    processor.deduplicate(data)
    result = processor.deduplicate(data)

    assert "_duplicate_of" not in result.fields


# --- validate ----------------------------------------------------------


def test_validate_returns_true_for_well_formed_tagged_data():
    processor = _processor()
    data = StructuredData(
        source_id="doc-1",
        fields={"dates": ["2026-08-20"], "numbers": ["190.5"], "provenance": "UNTRUSTED"},
    )

    assert processor.validate(data) is True


def test_validate_returns_false_when_provenance_tag_missing():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"ticker": "AAPL"})

    assert processor.validate(data) is False


def test_validate_returns_false_for_empty_content():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"provenance": "UNTRUSTED"})

    assert processor.validate(data) is False


def test_validate_returns_false_for_malformed_date():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"dates": ["not-a-date"], "provenance": "UNTRUSTED"})

    assert processor.validate(data) is False


def test_validate_returns_false_for_unparseable_number():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"numbers": ["not-a-number"], "provenance": "UNTRUSTED"})

    assert processor.validate(data) is False


# --- score_data_quality ----------------------------------------------------


def test_score_data_quality_scores_higher_for_complete_valid_data():
    processor = _processor()
    complete = StructuredData(
        source_id="doc-1",
        fields={"dates": ["2026-08-20"], "numbers": ["190.5"], "ticker": "AAPL", "provenance": "UNTRUSTED"},
    )
    sparse = StructuredData(source_id="doc-2", fields={"ticker": "", "provenance": "UNTRUSTED"})

    complete_score = processor.score_data_quality(complete)
    sparse_score = processor.score_data_quality(sparse)

    assert complete_score.score > sparse_score.score


def test_score_data_quality_uses_neutral_reliability_when_source_unknown():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    data = StructuredData(source_id="doc-1", fields={"ticker": "AAPL", "provenance": "UNTRUSTED"})

    score = processor.score_data_quality(data)

    # completeness=1.0, validity=1.0, reliability=0.5 (neutral, unknown source)
    assert score.score == pytest.approx(0.4 * 1.0 + 0.3 * 1.0 + 0.3 * 0.5)


def test_score_data_quality_reads_real_reliability_from_component_02_table():
    infra = _FakeInfrastructure()
    infra.store("sources", {"id": "metadata::doc-1", "source_id": "doc-1", "reliability": 1.0})
    processor = _processor(infra)
    data = StructuredData(source_id="doc-1", fields={"ticker": "AAPL", "provenance": "UNTRUSTED"})

    score = processor.score_data_quality(data)

    assert score.score == pytest.approx(0.4 * 1.0 + 0.3 * 1.0 + 0.3 * 1.0)


def test_score_data_quality_does_not_double_log_a_validate_lineage_step():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    data = StructuredData(source_id="doc-1", fields={"ticker": "AAPL", "provenance": "UNTRUSTED"})

    processor.score_data_quality(data)

    lineage = processor.track_data_lineage(data)
    assert lineage.steps == ["score_data_quality"]


# --- detect_stale_data -------------------------------------------------


def test_detect_stale_data_returns_false_for_a_recent_timestamp():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"_fetched_at": _now_stamp()})

    assert processor.detect_stale_data(data) is False


def test_detect_stale_data_returns_true_past_the_threshold():
    processor = _processor(staleness_threshold_seconds=1)
    data = StructuredData(source_id="doc-1", fields={"_fetched_at": "2020-01-01T00:00:00"})

    assert processor.detect_stale_data(data) is True


def test_detect_stale_data_treats_missing_timestamp_as_stale():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={})

    assert processor.detect_stale_data(data) is True


def test_detect_stale_data_treats_unparseable_timestamp_as_stale():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"_fetched_at": "not-a-timestamp"})

    assert processor.detect_stale_data(data) is True


# --- track_data_lineage --------------------------------------------------


def test_track_data_lineage_reflects_the_real_pipeline_order():
    infra = _FakeInfrastructure()
    processor = _processor(infra)
    raw = RawDocument(source_id="doc-1", content=b"Revenue: $100\nDate: 2026-08-20", fetched_at=_now_stamp())

    parsed = processor.parse(raw)
    extracted = processor.extract(parsed)
    normalized = processor.normalize(extracted)
    transformed = processor.transform(normalized)
    deduplicated = processor.deduplicate(transformed)
    processor.validate(deduplicated)
    processor.score_data_quality(deduplicated)
    processor.detect_stale_data(deduplicated)

    lineage = processor.track_data_lineage(deduplicated)

    assert lineage.steps == [
        "parse",
        "extract",
        "normalize",
        "transform",
        "deduplicate",
        "validate",
        "score_data_quality",
        "detect_stale_data",
    ]
    assert lineage.document_id == "doc-1"


def test_track_data_lineage_returns_empty_steps_when_nothing_processed_yet():
    processor = _processor()
    data = StructuredData(source_id="never-processed", fields={})

    lineage = processor.track_data_lineage(data)

    assert lineage.steps == []


def test_track_data_lineage_itself_does_not_append_a_step():
    processor = _processor()
    data = StructuredData(source_id="doc-1", fields={"provenance": "UNTRUSTED"})
    processor.validate(data)

    processor.track_data_lineage(data)
    lineage_again = processor.track_data_lineage(data)

    assert lineage_again.steps == ["validate"]


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
def test_full_pipeline_and_lineage_against_real_postgres():
    from infrastructure_postgres import DefaultInfrastructure

    processor = DefaultDataProcessingQuality(infrastructure=DefaultInfrastructure())
    source_id = f"test-doc-{uuid.uuid4().hex}"
    raw = RawDocument(source_id=source_id, content=b"Ticker: AAPL\nPrice: 190.5", fetched_at=_now_stamp())

    parsed = processor.parse(raw)
    extracted = processor.extract(parsed)
    normalized = processor.normalize(extracted)
    is_valid = processor.validate(normalized)
    score = processor.score_data_quality(normalized)
    lineage = processor.track_data_lineage(normalized)

    assert is_valid is True
    assert 0.0 <= score.score <= 1.0
    assert lineage.steps == ["parse", "extract", "normalize", "validate", "score_data_quality"]
