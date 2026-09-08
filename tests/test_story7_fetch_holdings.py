"""QA tests for STORY-7 — DefaultUpstoxBrokerConnector.fetch_holdings.

Every test exercises one of the STORY-7 acceptance criteria verbatim
against ``DefaultUpstoxBrokerConnector`` with a hand-authored
``UpstoxConfig`` and a ``Mock(spec=["get"])`` standing in for
``_UpstoxHttp`` (the helper is the seam — the connector's
``fetch_holdings`` calls ``self._http.get`` and nothing else on the
helper). No network is ever contacted, and no module-level
``requests`` patching is needed because the helper itself is mocked
wholesale.

Fixtures under ``tests/fixtures/upstox/`` are hand-authored JSON files
matching the real Upstox v2 response shape. No test hits
api.upstox.com or sandbox.upstox.com.
"""

import contextlib
import json
import logging
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest


@contextlib.contextmanager
def _no_log_warning_emitted():
    """Yield a list that, on exit, holds every WARNING-or-above log
    record produced during the ``with`` body. Attaches a capturing
    handler to the connector's own logger (and propagates to root)
    so we don't have to touch pytest's LogCaptureFixture machinery."""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    # Attach at root with propagation enabled so records from any
    # logger surface here — including the connector's own
    # ``logging.getLogger(__name__)``.
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.WARNING)
    try:
        yield captured
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

from components.c01_user_portfolio import (
    BrokerApiError,
    BrokerCredentials,
    BrokerHolding,
    DefaultUpstoxBrokerConnector,
)
from upstox_config import UpstoxConfig


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upstox"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file from tests/fixtures/upstox/."""
    path = FIXTURES_DIR / name
    with path.open() as fh:
        return json.load(fh)


# Verbatim config — the connector doesn't use redirect_uri for holdings,
# but we keep the same config shape as STORY-5/6 for consistency.
_CONFIG = UpstoxConfig(
    client_id="story7-client-id",
    client_secret="story7-client-secret",
    redirect_uri="https://example.com/cb",
)

_VALID_CREDS = BrokerCredentials(access_token="story7-access-token")


def _connector(http_mock: Mock) -> DefaultUpstoxBrokerConnector:
    """Construct a connector with the QA-fixture config and the
    caller-supplied ``http_mock`` standing in for ``_UpstoxHttp``.
    No real network, no real ``_UpstoxHttp``."""
    return DefaultUpstoxBrokerConnector(config=_CONFIG, http=http_mock)


# ---------------------------------------------------------------------------
# AC: Request URL is exactly
# ``https://api.upstox.com/v2/portfolio/long-term-holdings`` with
# Bearer and Accept headers.
# ---------------------------------------------------------------------------


def test_fetch_holdings_request_url_is_exactly_long_term_holdings_endpoint():
    """The connector calls ``_UpstoxHttp.get`` with path
    ``/v2/portfolio/long-term-holdings`` — the Upstox v2 endpoint
    for long-term holdings. No extra query string, no trailing slash,
    no alternate version. The host ``api.upstox.com`` is the base URL
    of the HTTP helper and is never overridden in the connector."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success", "data": []}

    connector = _connector(http_mock)
    connector.fetch_holdings(credentials=_VALID_CREDS)

    http_mock.get.assert_called_once_with(
        path="/v2/portfolio/long-term-holdings"
    )


# ---------------------------------------------------------------------------
# AC: ``status != 'success'`` -> ``BrokerApiError``.
# ---------------------------------------------------------------------------


def test_fetch_holdings_raises_broker_api_error_when_status_is_not_success():
    """The HTTP helper already raises ``BrokerApiError`` for any 2xx
    response whose ``status`` field is not ``'success'``. We assert
    the exception propagates correctly from the connector."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "error",
        "data": [],
    }

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError):
        connector.fetch_holdings(credentials=_VALID_CREDS)


def test_fetch_holdings_raises_broker_api_error_on_500_http_response():
    """A 500 from Upstox (handled by the HTTP helper) propagates as
    ``BrokerApiError``."""
    http_mock = Mock(spec=["get"])
    exc = BrokerApiError("Upstox returned HTTP 500")
    exc.http_status = 500
    exc.body_snippet = "Internal Server Error"
    http_mock.get.side_effect = exc

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError) as excinfo:
        connector.fetch_holdings(credentials=_VALID_CREDS)
    assert excinfo.value.http_status == 500


# ---------------------------------------------------------------------------
# AC: Missing/`null` ``data`` -> return ``[]``.
# ---------------------------------------------------------------------------


def test_fetch_holdings_returns_empty_list_when_data_is_null():
    """A ``data: null`` response is a legal empty-portfolio state —
    not an error. Returns ``[]``."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success", "data": None}

    connector = _connector(http_mock)
    result = connector.fetch_holdings(credentials=_VALID_CREDS)
    assert result == []


def test_fetch_holdings_returns_empty_list_when_data_key_is_absent():
    """A response with no ``data`` key at all also returns ``[]``."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success"}

    connector = _connector(http_mock)
    result = connector.fetch_holdings(credentials=_VALID_CREDS)
    assert result == []


def test_fetch_holdings_returns_empty_list_when_data_is_empty_list():
    """A response with ``data: []`` (the ``long_term_holdings_empty.json``
    fixture) returns ``[]``."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture("long_term_holdings_empty.json")

    connector = _connector(http_mock)
    result = connector.fetch_holdings(credentials=_VALID_CREDS)
    assert result == []


def test_fetch_holdings_raises_broker_api_error_when_data_is_not_a_list():
    """``data`` as a dict or string (not a list) is a contract
    violation — surface as ``BrokerApiError`` rather than silently
    returning ``[]``."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success", "data": {"foo": "bar"}}

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError) as excinfo:
        connector.fetch_holdings(credentials=_VALID_CREDS)
    assert "not a list" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC: Parsing the 3-holding fixture returns 3 ``BrokerHolding`` objects
# with every field mapped per the table, quantities/prices as ``Decimal``,
# and ``raw`` equal to the source element.
# ---------------------------------------------------------------------------


def test_fetch_holdings_returns_three_holdings_with_all_fields_from_success_fixture():
    """Integration test: the ``long_term_holdings_success.json`` fixture
    (3 holdings, one with ``average_price: null``, one with an extra
    undocumented key) is parsed into exactly 3 ``BrokerHolding``
    objects. Every mapped field is verified. Numeric fields are
    ``Decimal`` instances, not ``float``."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture(
        "long_term_holdings_success.json"
    )

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 3

    # --- Holding 0: RELIANCE (all fields present, no extra keys) ---
    h0 = holdings[0]
    assert h0.symbol == "RELIANCE"
    assert h0.isin == "INE002A01018"
    assert h0.quantity == Decimal("10")
    assert isinstance(h0.quantity, Decimal)
    assert h0.average_price == Decimal("2400.5")
    assert isinstance(h0.average_price, Decimal)
    assert h0.last_price == Decimal("2510.75")
    assert isinstance(h0.last_price, Decimal)
    assert h0.exchange == "NSE"
    assert h0.product == "CNC"
    assert h0.instrument_id == "NSE_EQ|INE002A01018"
    assert h0.name == "Reliance Industries Limited"
    assert h0.raw == {
        "isin": "INE002A01018",
        "trading_symbol": "RELIANCE",
        "quantity": 10,
        "average_price": 2400.50,
        "last_price": 2510.75,
        "close_price": 2505.00,
        "pnl": 1102.50,
        "exchange": "NSE",
        "product": "CNC",
        "instrument_token": "NSE_EQ|INE002A01018",
        "company_name": "Reliance Industries Limited",
    }
    assert "undocumented_field_added_by_upstox" not in h0.raw

    # --- Holding 1: TCS (average_price is null) ---
    h1 = holdings[1]
    assert h1.symbol == "TCS"
    assert h1.isin == "INE467B01029"
    assert h1.quantity == Decimal("5")
    assert h1.average_price is None  # AC: null -> None
    assert h1.last_price == Decimal("3850.25")
    assert h1.exchange == "NSE"
    assert h1.product == "CNC"
    assert h1.instrument_id == "NSE_EQ|INE467B01029"
    assert h1.name == "Tata Consultancy Services Limited"
    # raw must preserve the null value
    assert h1.raw["average_price"] is None

    # --- Holding 2: AAPL (extra undocumented key preserved in raw) ---
    h2 = holdings[2]
    assert h2.symbol == "AAPL"
    assert h2.isin == "US0378331005"
    assert h2.quantity == Decimal("20")
    assert h2.average_price == Decimal("150.25")
    assert h2.last_price == Decimal("175.5")
    assert h2.exchange == "NASDAQ"
    assert h2.product == "CNC"
    assert h2.instrument_id == "NASDAQ_EQ|US0378331005"
    assert h2.name == "Apple Inc."
    # AC: extra undocumented key must be retained in raw
    assert "undocumented_field_added_by_upstox" in h2.raw
    assert h2.raw["undocumented_field_added_by_upstox"] == "preserved-in-raw"


# ---------------------------------------------------------------------------
# AC: Numeric fields use ``Decimal(str(value))`` (never ``float``).
# ---------------------------------------------------------------------------


def test_fetch_holdings_numeric_fields_are_decimal_not_float():
    """Numeric values must be ``Decimal`` instances, not ``float``.
    ``Decimal(str(value))`` is used (never ``Decimal(value)``) so
    binary-float rounding surprises are avoided."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "TEST",
                "trading_symbol": "TEST",
                "quantity": 3,          # int
                "average_price": 100.1, # float  (str() converts cleanly)
                "last_price": "200.2",  # string (str() is a no-op)
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "T1",
                "company_name": "Test Co",
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 1
    h = holdings[0]
    assert type(h.quantity).__name__ == "Decimal"
    assert type(h.average_price).__name__ == "Decimal"
    assert type(h.last_price).__name__ == "Decimal"
    # Verify the exact value — ``Decimal(str(100.1))`` is clean
    assert h.average_price == Decimal("100.1")


# ---------------------------------------------------------------------------
# AC: A holding element missing ``trading_symbol`` is skipped, the rest
# are returned, and a warning is logged.
# ---------------------------------------------------------------------------


def test_fetch_holdings_skips_element_missing_trading_symbol_and_returns_rest():
    """An element without ``trading_symbol`` is skipped (not raised
    as an error). The remaining valid elements are still returned.
    A warning is emitted via the standard logging channel."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "VALID1",
                "trading_symbol": "VALID1",
                "quantity": 1,
                "average_price": 100,
                "last_price": 105,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "V1",
                "company_name": "Valid One",
            },
            {
                # Missing trading_symbol — must be skipped
                "isin": "NOMARKET",
                "quantity": 0,
                "average_price": 0,
                "last_price": 0,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "NOMARKET",
                "company_name": "No Market Symbol",
            },
            {
                "isin": "VALID2",
                "trading_symbol": "VALID2",
                "quantity": 2,
                "average_price": 200,
                "last_price": 210,
                "exchange": "BSE",
                "product": "CNC",
                "instrument_token": "V2",
                "company_name": "Valid Two",
            },
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 2
    symbols = [h.symbol for h in holdings]
    assert "VALID1" in symbols
    assert "NOMARKET" not in symbols
    assert "VALID2" in symbols


def test_fetch_holdings_logs_warning_for_missing_trading_symbol(caplog):
    """The skip for a ``trading_symbol``-less element emits a
    warning-level log on the connector's logger channel with the
    ``UPSTOX_HOLDING_ELEMENT_SKIPPED`` error code."""
    caplog.set_level(logging.WARNING)

    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "ORPHAN",
                # trading_symbol deliberately absent
                "quantity": 1,
                "average_price": 10,
                "last_price": 10,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "ORPHAN",
                "company_name": "Orphaned Entry",
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert holdings == []
    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert any(
        "skipping holding element missing 'trading_symbol'" in msg
        for msg in warning_messages
    )
    assert any(
        "UPSTOX_HOLDING_ELEMENT_SKIPPED" in msg
        for msg in warning_messages
    )


def test_fetch_holdings_null_trading_symbol_is_skipped():
    """A holding element with ``trading_symbol: null`` is treated
    identically to one missing the key entirely — both are skipped."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "NULLSYM",
                "trading_symbol": None,
                "quantity": 1,
                "average_price": 10,
                "last_price": 10,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "NULLSYM",
                "company_name": "Null Symbol",
            },
            {
                "isin": "REAL",
                "trading_symbol": "REAL",
                "quantity": 3,
                "average_price": 50,
                "last_price": 60,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "REAL",
                "company_name": "Real Symbol",
            },
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 1
    assert holdings[0].symbol == "REAL"


# ---------------------------------------------------------------------------
# AC: An element missing an extra undocumented key parses successfully
# and the key is retained in ``raw``.
# ---------------------------------------------------------------------------


def test_fetch_holdings_preserves_extra_undocumented_keys_in_raw():
    """The documented response shape ends in '...' — Upstox is known
    to add undocumented keys without notice. ``raw`` must be the
    entire element verbatim, not a filtered subset of known keys."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "EXTRAS",
                "trading_symbol": "EXTRAS",
                "quantity": 1,
                "average_price": 10,
                "last_price": 11,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "EX1",
                "company_name": "Extras Co",
                # These three keys are NOT in the documented shape
                "upstox_internal_field": "internal-val",
                "another_mystery_key": 42,
                "yet_another": {"nested": "object"},
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 1
    h = holdings[0]
    assert "upstox_internal_field" in h.raw
    assert h.raw["upstox_internal_field"] == "internal-val"
    assert "another_mystery_key" in h.raw
    assert h.raw["another_mystery_key"] == 42
    assert "yet_another" in h.raw
    assert h.raw["yet_another"] == {"nested": "object"}


# ---------------------------------------------------------------------------
# AC: ``isinstance(connector, BrokerConnector)`` remains True after
# the implementation fills in ``fetch_holdings``.
# ---------------------------------------------------------------------------


def test_fetch_holdings_connector_still_satisfies_brokerconnector_protocol():
    """Filling in ``fetch_holdings`` must not break the Protocol
    conformance check that STORY-5 already verified via
    ``isinstance``. ``NotImplementedError`` satisfied the Protocol
    signature; the real implementation must keep doing so."""
    from components.c01_user_portfolio import BrokerConnector

    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success", "data": []}

    connector = _connector(http_mock)
    assert isinstance(connector, BrokerConnector)


# ---------------------------------------------------------------------------
# AC: ``quantity`` = ``Decimal(str(quantity))`` — integer values must
# not produce float-implied decimals.
# ---------------------------------------------------------------------------


def test_fetch_holdings_integer_quantity_becomes_exact_decimal():
    """A quantity of ``10`` (int, no decimal point) must become
    ``Decimal('10')``, not ``Decimal('10.0')``. ``str(10) == '10'``
    so the conversion is exact."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "INTQ",
                "trading_symbol": "INTQ",
                "quantity": 10,  # int, no decimal
                "average_price": 100,
                "last_price": 110,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "IQ1",
                "company_name": "Integer Quantity Co",
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 1
    assert holdings[0].quantity == Decimal("10")
    # The string representation must NOT show trailing zeros from float
    assert str(holdings[0].quantity) == "10"


# ---------------------------------------------------------------------------
# Below: QA-author's own NEW test functions for STORY-7 — one fresh
# assertion per acceptance criterion that the existing tests do not
# independently cover. These are NOT the same assertions as the
# pre-existing 15 tests; each targets a different angle of the AC so
# this turn's verification stands on its own, regardless of any
# re-run of the pre-existing file.
# ---------------------------------------------------------------------------


def test_qa_request_url_is_exactly_long_term_holdings_full_path():
    """QA's own AC: the request URL path must be exactly
    ``/v2/portfolio/long-term-holdings`` — not
    ``/v2/portfolio/long-term-holdings/`` (no trailing slash), not
    ``/v2/portfolio/holdings`` (alternate route), not anything else.
    Also confirms the call uses the keyword-only path argument (no
    positional / no extra keyword args)."""
    from components.c01_user_portfolio import (
        DefaultUpstoxBrokerConnector as ConnectorClass,
    )
    # The class constant must exist and be exactly the documented path
    assert ConnectorClass._UPSTOX_LONG_TERM_HOLDINGS_PATH == (
        "/v2/portfolio/long-term-holdings"
    )

    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success", "data": []}

    connector = _connector(http_mock)
    connector.fetch_holdings(credentials=_VALID_CREDS)

    # Exactly one call, exactly the right path, keyword-only.
    http_mock.get.assert_called_once_with(
        path="/v2/portfolio/long-term-holdings"
    )
    _, kwargs = http_mock.get.call_args
    assert list(kwargs.keys()) == ["path"], (
        "fetch_holdings must pass only `path=` to _UpstoxHttp.get, "
        "not query-string args or headers (those live on the helper)."
    )


def test_qa_credentials_are_passed_to_fetch_holdings_but_not_to_http():
    """QA's own AC: the connector requires ``credentials`` as a
    keyword-only argument (per the Protocol) and forwards them only
    to the connector itself — not to ``_UpstoxHttp.get``. The
    helper owns the Authorization header construction via its
    injected token_provider, so the connector must not try to
    duplicate that work."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {"status": "success", "data": []}

    connector = _connector(http_mock)
    connector.fetch_holdings(credentials=_VALID_CREDS)

    # _UpstoxHttp.get receives ONLY the path; no credentials,
    # no token, no Authorization header (the helper does that).
    _, kwargs = http_mock.get.call_args
    forbidden = ["credentials", "token", "access_token", "headers"]
    for k in forbidden:
        assert k not in kwargs, (
            f"fetch_holdings must not pass `{k}` to _UpstoxHttp.get; "
            f"the helper owns the Authorization header contract."
        )


def test_qa_success_fixture_three_holdings_with_decimal_types():
    """QA's own AC: parsing the 3-holding success fixture returns
    EXACTLY 3 BrokerHolding objects; quantity/average_price/last_price
    are Decimal instances (not float); ``raw`` equals the source
    element verbatim (object identity preserved by dict equality
    works for the AC's "raw == source element" requirement)."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture(
        "long_term_holdings_success.json"
    )

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 3
    assert all(isinstance(h, BrokerHolding) for h in holdings)

    # Every numeric field must be Decimal, never float
    for h in holdings:
        assert isinstance(h.quantity, Decimal), (
            f"quantity for {h.symbol} is {type(h.quantity).__name__}, "
            f"expected Decimal"
        )
        # average_price may be None (TCS in the fixture is null)
        if h.average_price is not None:
            assert isinstance(h.average_price, Decimal)
        if h.last_price is not None:
            assert isinstance(h.last_price, Decimal)

    # raw must equal the source element — for each holding, look
    # back at the source data list.
    source_data = load_fixture("long_term_holdings_success.json")["data"]
    by_symbol = {elem["trading_symbol"]: elem for elem in source_data}
    for h in holdings:
        assert h.raw == by_symbol[h.symbol], (
            f"raw for {h.symbol} does not equal its source element"
        )


def test_qa_empty_fixture_returns_empty_list_with_no_warning():
    """QA's own AC: the long_term_holdings_empty.json fixture
    (``data: []``) returns an empty list AND does not emit any
    spurious warnings — an empty portfolio is a normal state, not
    something to warn about."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture(
        "long_term_holdings_empty.json"
    )

    connector = _connector(http_mock)
    with _no_log_warning_emitted() as records:
        result = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert result == []
    # No warnings should have been emitted for an empty fixture
    warnings = [
        r for r in records if r.levelno == logging.WARNING
    ]
    assert warnings == [], (
        f"empty fixture must not emit warnings; got "
        f"{[r.message for r in warnings]}"
    )


def test_qa_status_error_raises_broker_api_error():
    """QA's own AC: a response with ``status: 'error'`` raises
    BrokerApiError. Distinct from the existing
    test_fetch_holdings_raises_broker_api_error_when_status_is_not_success
    in that this one specifically loads the SUCCESS fixture, mutates
    only ``status`` to ``error`` while keeping ``data`` as the
    3-holding list, and confirms both that the exception is raised
    AND that none of the 3 holdings silently leak through."""
    import contextlib

    payload = load_fixture("long_term_holdings_success.json")
    payload["status"] = "error"

    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = payload

    connector = _connector(http_mock)
    with contextlib.suppress(BrokerApiError):
        result = connector.fetch_holdings(credentials=_VALID_CREDS)
        # If we reach here, the connector failed to raise on
        # status='error' — that is itself a real AC failure.
        assert False, (
            f"fetch_holdings must raise BrokerApiError on status='error'; "
            f"got result={result!r}"
        )


def test_qa_extra_undocumented_key_in_holding2_is_preserved_in_raw():
    """QA's own AC: holding 2 of the success fixture has the
    key ``undocumented_field_added_by_upstox`` which is not in the
    documented shape. After parsing, that key MUST be retained
    verbatim in the resulting ``BrokerHolding.raw`` dict — the
    AC's "Unknown/extra keys must be preserved in raw and must
    not cause failure" rule. Holding 0 has no such key."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = load_fixture(
        "long_term_holdings_success.json"
    )

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    # Locate holding 2 by trading_symbol (the fixture is the source
    # of truth on which index carries the extra key).
    by_symbol = {h.symbol: h for h in holdings}
    aapl = by_symbol["AAPL"]
    assert aapl is not None, "AAPL holding must be parsed"

    # The undocumented key is in raw and retains its original value
    assert "undocumented_field_added_by_upstox" in aapl.raw
    assert aapl.raw["undocumented_field_added_by_upstox"] == (
        "preserved-in-raw"
    )

    # Holding 0 (RELIANCE) does NOT have the undocumented key
    reliance = by_symbol["RELIANCE"]
    assert "undocumented_field_added_by_upstox" not in reliance.raw


def test_qa_missing_trading_symbol_is_skipped_and_rest_returned(caplog):
    """QA's own AC: a 3-element data list with element #1 missing
    ``trading_symbol`` returns exactly 2 holdings (the other two),
    in their original order. Element #1 (the skipped one) MUST NOT
    appear in the returned holdings by any field (symbol, isin,
    instrument_id, company_name)."""
    caplog.set_level(logging.WARNING)

    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "GOOD-A",
                "trading_symbol": "GOOD-A",
                "quantity": 1,
                "average_price": 10,
                "last_price": 11,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "GA",
                "company_name": "Good A",
            },
            {
                # NO trading_symbol — to be skipped
                "isin": "BAD-B",
                "quantity": 0,
                "average_price": 0,
                "last_price": 0,
                "exchange": "BSE",
                "product": "CNC",
                "instrument_token": "BB",
                "company_name": "Bad B",
            },
            {
                "isin": "GOOD-C",
                "trading_symbol": "GOOD-C",
                "quantity": 3,
                "average_price": 30,
                "last_price": 33,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "GC",
                "company_name": "Good C",
            },
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    # Exactly 2 holdings returned, in their original order
    assert len(holdings) == 2
    assert [h.symbol for h in holdings] == ["GOOD-A", "GOOD-C"]

    # The skipped element must not appear under ANY field
    assert "BAD-B" not in [h.symbol for h in holdings]
    assert "BAD-B" not in [h.isin for h in holdings]
    assert "BB" not in [h.instrument_id for h in holdings]
    assert "Bad B" not in [h.name for h in holdings]

    # A WARNING-level log entry was emitted (the AC's "warning log"
    # requirement — we don't enforce a specific message because
    # message vs extra-field formatting is implementation-defined).
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(warnings) >= 1, (
        "missing-trading_symbol skip must emit at least one WARNING-level "
        f"log record; got {[r.levelname for r in caplog.records]}"
    )


def test_qa_no_test_calls_upstox_network_endpoints():
    """QA's own AC: the AC's "no test hits api.upstox.com or
    sandbox.upstox.com" rule. This test acts as a static guard:
    the QA suite must use ONLY the ``Mock(spec=["get"])`` seam on
    the injected ``_UpstoxHttp`` and never touch ``requests``
    directly. We verify by checking that this test module's
    top-level namespace does NOT contain any ``requests`` submodule
    binding — if ``requests`` or any ``requests.*`` name were
    imported at module level, it would appear in the module's
    ``__dict__``."""
    import tests.test_story7_fetch_holdings as this_module  # noqa: F401

    module_dict_keys = list(vars(this_module).keys())

    # ``requests`` itself must not be imported into this test file's
    # namespace (other files in this project that test real network
    # calls DO import requests; this file must not).
    assert "requests" not in module_dict_keys, (
        "this QA file must not import 'requests' — the connector is "
        "tested exclusively through the injected _UpstoxHttp seam"
    )

    # No real network utilities either
    assert "urllib.request" not in module_dict_keys
    assert "urllib2" not in module_dict_keys
    assert "http.client" not in module_dict_keys
    assert "httpx" not in module_dict_keys

    # No direct reference to the real Upstox hostnames as URLs.
    # We only check actual code-defined names (skip dunders like
    # __doc__/__name__ that legitimately quote the forbidden hosts
    # in documentation prose).
    code_names = [
        n for n in module_dict_keys
        if not n.startswith("__") and n not in (
            "annotations", "builtins",
        )
    ]
    for name in code_names:
        val = getattr(this_module, name, None)
        if isinstance(val, str) and "api.upstox.com" in val:
            raise AssertionError(
                f"module-level name {name!r} contains the real host "
                f"'api.upstox.com' — use Mock(spec=['get']) instead"
            )
        if isinstance(val, str) and "sandbox.upstox.com" in val:
            raise AssertionError(
                f"module-level name {name!r} contains the real host "
                f"'sandbox.upstox.com' — use Mock(spec=['get']) instead"
            )


def test_qa_numeric_field_with_float_uses_decimal_str_not_decimal_direct():
    """QA's own AC: the AC specifies ``Decimal(str(value))`` — never
    ``Decimal(value)``. We probe this with the canonical float-
    rounding bug: ``Decimal(0.1)`` is
    ``Decimal('0.1000000000000000055511151231257827021181583404541015625')``
    while ``Decimal(str(0.1))`` is the clean ``Decimal('0.1')``. A
    holding with ``average_price: 0.1`` must produce
    ``Decimal('0.1')`` (clean), not the long binary-float junk."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "FLOAT-BUG",
                "trading_symbol": "FLOAT-BUG",
                "quantity": 1,
                "average_price": 0.1,    # the classic float-rounding trap
                "last_price": 0.1,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "FB",
                "company_name": "Float Bug Test",
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 1
    h = holdings[0]
    # Clean Decimal('0.1'), not the binary-float junk
    assert h.average_price == Decimal("0.1")
    assert str(h.average_price) == "0.1"
    # Belt-and-braces: explicitly NOT the long binary-float form
    assert h.average_price != Decimal(
        "0.1000000000000000055511151231257827021181583404541015625"
    )
    assert h.last_price == Decimal("0.1")
    assert str(h.last_price) == "0.1"


def test_qa_missing_numeric_keys_become_none():
    """QA's own AC: numeric fields (quantity/average_price/last_price)
    are ``None`` when the key is absent OR when the value is explicitly
    null. We test BOTH branches in one holding: average_price absent,
    last_price: null, quantity: 5 (present)."""
    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "NULLS",
                "trading_symbol": "NULLS",
                # quantity: present, must be Decimal
                "quantity": 5,
                # average_price: key absent, must be None
                # last_price: value null, must be None
                "last_price": None,
                "exchange": "NSE",
                "product": "CNC",
                "instrument_token": "NS",
                "company_name": "Nulls Test Co",
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    assert len(holdings) == 1
    h = holdings[0]
    assert h.quantity == Decimal("5")
    assert h.average_price is None, (
        "average_price absent from the source element must be None"
    )
    assert h.last_price is None, (
        "last_price: null in the source element must be None"
    )


# ---------------------------------------------------------------------------
# NEW QA tests for STORY-7 acceptance criteria not yet independently
# covered by any existing test in this file.
# ---------------------------------------------------------------------------


def test_qa_warning_log_includes_error_code_in_extra_dict(caplog):
    """AC: the skip for a trading_symbol-less element emits a warning
    log whose extra dict carries error_code=UPSTOX_HOLDING_ELEMENT_SKIPPED
    AND other identifying fields. This tests the extra= dict specifically,
    which the existing test_fetch_holdings_logs_warning_for_missing_trading_symbol
    only covers via string-in-message assertions. Both are verified here:
    (a) the error_code string appears in the message itself, AND
    (b) the extra dict on the record carries the structured fields."""
    caplog.set_level(logging.WARNING)

    http_mock = Mock(spec=["get"])
    http_mock.get.return_value = {
        "status": "success",
        "data": [
            {
                "isin": "UPSTOX_ORPHAN_ISIN",
                # trading_symbol deliberately absent to trigger skip
                "quantity": 99,
                "average_price": 0,
                "last_price": 0,
                "exchange": "BSE",
                "product": "CNC",
                "instrument_token": "BSE_ORPHAN_TOKEN",
                "company_name": "Orphaned BSE Entry",
            }
        ],
    }

    connector = _connector(http_mock)
    holdings = connector.fetch_holdings(credentials=_VALID_CREDS)

    # The skipped element returns nothing
    assert holdings == []

    # Find the warning records for this skip event
    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    assert len(warning_records) >= 1, (
        f"expected at least one WARNING log record for the skipped "
        f"element; got {[(r.levelname, r.message) for r in caplog.records]}"
    )

    # Verify (a): error code appears in the message string
    skip_messages = [
        r.message for r in warning_records
        if "trading_symbol" in r.message
    ]
    assert any(
        "UPSTOX_HOLDING_ELEMENT_SKIPPED" in msg
        for msg in skip_messages
    ), (
        f"error code UPSTOX_HOLDING_ELEMENT_SKIPPED must appear in "
        f"the warning message string; got {[r.message for r in warning_records]}"
    )

    # Verify (b): the extra dict on the record carries the structured fields
    skip_record = next(
        r for r in warning_records
        if "trading_symbol" in r.message
    )
    # Python's logging captures extra= keys as attributes on the LogRecord
    assert hasattr(skip_record, "error_code"), (
        "the warning log record must carry error_code from extra={}; "
        f"record.__dict__ = {skip_record.__dict__}"
    )
    assert skip_record.error_code == "UPSTOX_HOLDING_ELEMENT_SKIPPED", (
        f"error_code must be UPSTOX_HOLDING_ELEMENT_SKIPPED; "
        f"got {skip_record.error_code!r}"
    )
    # isin and instrument_token from the skipped element
    assert hasattr(skip_record, "isin"), (
        "the warning log record must carry isin from extra={}"
    )
    assert skip_record.isin == "UPSTOX_ORPHAN_ISIN"
    assert hasattr(skip_record, "instrument_token"), (
        "the warning log record must carry instrument_token from extra={}"
    )
    assert skip_record.instrument_token == "BSE_ORPHAN_TOKEN"
    assert hasattr(skip_record, "company_name"), (
        "the warning log record must carry company_name from extra={}"
    )
    assert skip_record.company_name == "Orphaned BSE Entry"
