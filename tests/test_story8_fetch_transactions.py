"""QA tests for STORY-8 — DefaultUpstoxBrokerConnector.fetch_transactions.

Same real seam as STORY-7's fetch_holdings tests: a ``Mock(spec=["get"])``
stands in for ``_UpstoxHttp`` (the connector's own only touchpoint with
the helper), and every fixture under ``tests/fixtures/upstox/`` is a
real, hand-authored JSON file loaded via ``json.load()`` -- never an
inline Python dict literal, per this story's own explicit warning about
prior attempts that duplicated a literal fixture dict across two test
functions. No test hits api.upstox.com or sandbox.upstox.com.
"""

import copy
import logging
from decimal import Decimal
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from components.c01_user_portfolio import (
    BrokerApiError,
    BrokerCredentials,
    DefaultUpstoxBrokerConnector,
)
from upstox_config import UpstoxConfig

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upstox"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file from tests/fixtures/upstox/."""
    import json

    with (FIXTURES_DIR / name).open() as fh:
        return json.load(fh)


_CONFIG = UpstoxConfig(
    client_id="story8-client-id",
    client_secret="story8-client-secret",
    redirect_uri="https://example.com/cb",
)
_VALID_CREDS = BrokerCredentials(access_token="story8-access-token")

_START = date(2024, 1, 1)
_END = date(2024, 12, 31)


def _connector(http_mock: Mock) -> DefaultUpstoxBrokerConnector:
    return DefaultUpstoxBrokerConnector(config=_CONFIG, http=http_mock)


def _query_params(call) -> dict:
    """Given one recorded call to ``http_mock.get``, parse its ``path``
    kwarg's query string into a plain {key: value} dict."""
    path = call.kwargs["path"]
    parsed = urlparse(path)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def _path_only(call) -> str:
    path = call.kwargs["path"]
    return urlparse(path).path


# ---------------------------------------------------------------------------
# AC: Request URL/query string shape.
# ---------------------------------------------------------------------------


def test_fetch_transactions_request_shape_is_exactly_historical_trades_endpoint():
    """Path is exactly /v2/charges/historical-trades; query string
    carries start_date, end_date (YYYY-mm-dd), page_number, page_size,
    and no segment key."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture("historical_trades_empty.json")

    connector = _connector(http_mock)
    connector.fetch_transactions(
        credentials=_VALID_CREDS, start_date=_START, end_date=_END
    )

    call = http_mock.get.call_args_list[0]
    assert _path_only(call) == "/v2/charges/historical-trades"
    params = _query_params(call)
    assert params["start_date"] == "2024-01-01"
    assert params["end_date"] == "2024-12-31"
    assert params["page_number"] == "1"
    assert params["page_size"] == "1000"
    assert "segment" not in params


# ---------------------------------------------------------------------------
# AC: 3-page fixture set -> exactly 3 GETs, page_number 1,2,3, union of
# all rows returned.
# ---------------------------------------------------------------------------


def test_fetch_transactions_pages_through_all_three_pages():
    page1 = load_fixture("historical_trades_page1.json")
    page2 = load_fixture("historical_trades_page2.json")
    page3 = load_fixture("historical_trades_page3.json")
    http_mock = Mock(spec=["get"])
    http_mock.get.side_effect = [page1, page2, page3]

    connector = _connector(http_mock)
    result = connector.fetch_transactions(
        credentials=_VALID_CREDS, start_date=_START, end_date=_END
    )

    assert http_mock.get.call_count == 3
    page_numbers = [
        _query_params(call)["page_number"] for call in http_mock.get.call_args_list
    ]
    assert page_numbers == ["1", "2", "3"]

    # page3's TRD004 is a duplicate of page2's -- deduped, so the union
    # is 2 + 2 + 1 unique-new (TRD005; TRD006 is skipped for an
    # unrecognized transaction_type) = 5 real transactions.
    external_ids = [t.external_id for t in result]
    assert external_ids == ["TRD001", "TRD002", "TRD003", "TRD004", "TRD005"]


# ---------------------------------------------------------------------------
# AC: duplicate trade_id appears exactly once.
# ---------------------------------------------------------------------------


def test_fetch_transactions_deduplicates_by_trade_id_keeping_first_occurrence():
    page1 = load_fixture("historical_trades_page1.json")
    page2 = load_fixture("historical_trades_page2.json")
    page3 = load_fixture("historical_trades_page3.json")
    http_mock = Mock(spec=["get"])
    http_mock.get.side_effect = [page1, page2, page3]

    connector = _connector(http_mock)
    result = connector.fetch_transactions(
        credentials=_VALID_CREDS, start_date=_START, end_date=_END
    )

    duplicated = [t for t in result if t.external_id == "TRD004"]
    assert len(duplicated) == 1
    # The kept copy is the FIRST occurrence (page2's), not page3's --
    # both fixtures carry identical data for TRD004 in this fixture
    # set, so this also implicitly checks page2's own values survived.
    assert duplicated[0].symbol == "GOOGL"
    assert duplicated[0].quantity == Decimal("3")


# ---------------------------------------------------------------------------
# AC: field mapping is exact; trade_date is a date, quantity/price/
# amount are Decimal, side is 'BUY'/'SELL'.
# ---------------------------------------------------------------------------


def test_fetch_transactions_maps_every_field_per_the_story_table():
    page1 = load_fixture("historical_trades_page1.json")
    http_mock = Mock(spec=["get"])
    http_mock.get.side_effect = [
        page1,
        load_fixture("historical_trades_empty.json"),
    ]
    # Force this single-page case to look like page 1 of 1, so the
    # loop makes exactly one real request and returns right away --
    # built by mutating a COPY of the loaded fixture at runtime (per
    # the story's own explicit rule), never a second hand-typed
    # literal.
    single_page = copy.deepcopy(page1)
    single_page["meta_data"]["page"]["total_pages"] = 1
    http_mock.get.side_effect = [single_page]

    connector = _connector(http_mock)
    result = connector.fetch_transactions(
        credentials=_VALID_CREDS, start_date=_START, end_date=_END
    )

    assert len(result) == 2
    reliance = next(t for t in result if t.external_id == "TRD001")
    assert reliance.symbol == "RELIANCE"
    assert reliance.isin == "INE002A01018"
    assert reliance.trade_date == date(2024, 1, 5)
    assert isinstance(reliance.trade_date, date)
    assert reliance.side == "BUY"
    assert reliance.quantity == Decimal("10")
    assert isinstance(reliance.quantity, Decimal)
    assert reliance.price == Decimal("2400.50")
    assert isinstance(reliance.price, Decimal)
    assert reliance.amount == Decimal("24005.00")
    assert isinstance(reliance.amount, Decimal)
    assert reliance.exchange == "NSE"
    assert reliance.segment == "EQ"
    assert reliance.raw["scrip_name"] == "Reliance Industries"

    tcs = next(t for t in result if t.external_id == "TRD002")
    assert tcs.side == "SELL"


# ---------------------------------------------------------------------------
# AC: a row with transaction_type outside BUY/SELL is skipped with a
# warning, and does not fail the call.
# ---------------------------------------------------------------------------


def test_fetch_transactions_skips_unrecognized_transaction_type_with_warning():
    page1 = load_fixture("historical_trades_page1.json")
    page2 = load_fixture("historical_trades_page2.json")
    page3 = load_fixture("historical_trades_page3.json")
    http_mock = Mock(spec=["get"])
    http_mock.get.side_effect = [page1, page2, page3]

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.WARNING)
    try:
        connector = _connector(http_mock)
        result = connector.fetch_transactions(
            credentials=_VALID_CREDS, start_date=_START, end_date=_END
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    # TRD006 (transaction_type "DIVIDEND") must not appear in the
    # result, and the call itself must not have raised.
    assert not any(t.external_id == "TRD006" for t in result)
    assert any(
        "UPSTOX_TRANSACTION_ROW_SKIPPED" in record.getMessage()
        for record in captured
    )


# ---------------------------------------------------------------------------
# AC: empty fixture -> [] with exactly one request; error status ->
# BrokerApiError; 200-page cap -> BrokerApiError.
# ---------------------------------------------------------------------------


def test_fetch_transactions_empty_fixture_returns_empty_list_with_one_request():
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture("historical_trades_empty.json")

    connector = _connector(http_mock)
    result = connector.fetch_transactions(
        credentials=_VALID_CREDS, start_date=_START, end_date=_END
    )

    assert result == []
    assert http_mock.get.call_count == 1


def test_fetch_transactions_error_status_raises_broker_api_error():
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture("historical_trades_error.json")

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError):
        connector.fetch_transactions(
            credentials=_VALID_CREDS, start_date=_START, end_date=_END
        )


def test_fetch_transactions_exceeding_200_pages_raises_broker_api_error():
    page1 = load_fixture("historical_trades_page1.json")
    # A page that always claims there's more after it, built by
    # mutating a COPY of a real loaded fixture at runtime -- never a
    # second hand-typed literal, per the story's own explicit rule.
    never_ending_page = copy.deepcopy(page1)
    never_ending_page["meta_data"]["page"]["total_pages"] = 9999

    http_mock = Mock(spec=["get"])
    http_mock.get.side_effect = lambda path: never_ending_page

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError):
        connector.fetch_transactions(
            credentials=_VALID_CREDS, start_date=_START, end_date=_END
        )

    assert http_mock.get.call_count == 200


# ---------------------------------------------------------------------------
# AC: start_date > end_date raises ValueError before any HTTP call.
# ---------------------------------------------------------------------------


def test_fetch_transactions_inverted_date_range_raises_before_any_http_call():
    http_mock = Mock(spec=["get"])

    connector = _connector(http_mock)
    with pytest.raises(ValueError):
        connector.fetch_transactions(
            credentials=_VALID_CREDS,
            start_date=date(2024, 12, 31),
            end_date=date(2024, 1, 1),
        )

    http_mock.get.assert_not_called()
