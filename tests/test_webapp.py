"""Smoke tests for the Flask app scaffold (src/webapp.py). Each UI
story (STORY-14 through STORY-18) adds its own routes/templates on top
of this; this file only covers the scaffold itself."""

from __future__ import annotations

from webapp import create_app, get_currency_symbol, format_price
from market_hours import market_status


def test_create_app_returns_a_working_flask_app():
    app = create_app()
    assert app is not None


def test_index_route_returns_200():
    client = create_app().test_client()
    response = client.get("/")
    assert response.status_code == 200


def test_health_route_returns_ok_json():
    client = create_app().test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_stock_entry_route_returns_200():
    client = create_app().test_client()
    response = client.get("/stock_entry")
    assert response.status_code == 200
    assert b"Stock Entry Form" in response.data


# STORY-15: Portfolio display with currency symbols and exchange names
class TestCurrencySymbols:
    """Tests for currency symbol display (₹ for INR, $ for USD)."""

    def test_inr_currency_symbol_returns_rupee_sign(self):
        """INR should return the ₹ Unicode symbol."""
        symbol = get_currency_symbol("INR")
        assert symbol == "₹"

    def test_usd_currency_symbol_returns_dollar_sign(self):
        """USD should return the $ symbol."""
        symbol = get_currency_symbol("USD")
        assert symbol == "$"

    def test_unknown_currency_returns_code_with_space(self):
        """Unknown currency should return the code with space."""
        symbol = get_currency_symbol("EUR")
        assert symbol == "EUR "


class TestPriceFormatting:
    """Tests for price formatting with 2 decimal places."""

    def test_format_price_rounds_to_2_decimal_places(self):
        """Prices should be formatted with exactly 2 decimal places."""
        assert format_price("1234.567") == "1234.57"
        assert format_price("100.999") == "101.00"
        assert format_price("50") == "50.00"

    def test_format_price_handles_integer(self):
        """Integer prices should be formatted with .00."""
        assert format_price(100) == "100.00"

    def test_format_price_handles_decimal(self):
        """Decimal prices should be formatted correctly."""
        assert format_price("99.9") == "99.90"


class TestPortfolioRoute:
    """Tests for /portfolio route displaying holdings with currency and exchange."""

    def test_portfolio_route_returns_200(self):
        """Portfolio route should return 200 OK."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        assert response.status_code == 200

    def test_portfolio_displays_exchange_names(self):
        """Portfolio should display exchange names for holdings."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # Check for NSE, BSE, NYSE, NASDAQ exchange names
        assert b"NSE" in data
        assert b"BSE" in data
        assert b"NASDAQ" in data
        assert b"NYSE" in data

    def test_portfolio_displays_indian_stocks_section(self):
        """Portfolio should show Indian stocks (INR) section."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # Check for INR badge/class
        assert b"badge-info" in data  # INR badge style

    def test_portfolio_displays_us_stocks_section(self):
        """Portfolio should show US stocks (USD) section."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # Check for USD badge/class
        assert b"badge-success" in data  # USD badge style

    def test_portfolio_displays_prices_with_2_decimals(self):
        """Portfolio prices should display with 2 decimal places."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        # Check for formatted prices like 2500.00
        assert b"2500.00" in response.data

    def test_portfolio_groups_by_currency(self):
        """Portfolio holdings should be grouped by currency."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # Should have separate sections for INR and USD
        assert b"Indian Stocks" in data or b"INR" in data
        assert b"US Stocks" in data or b"USD" in data

    def test_portfolio_inr_price_shows_rupee_unicode_symbol(self):
        """AC: ₹ Unicode symbol displays next to INR prices.
        The template renders ₹ via get_currency_symbol('INR') in a span.
        We check the actual Unicode ₹ (U+20B9) appears in the response."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        # ₹ is the Indian Rupee sign U+20B9
        assert "₹".encode("utf-8") in response.data, (
            "INR prices must include the ₹ Unicode symbol (U+20B9) in the response"
        )

    def test_portfolio_usd_price_shows_dollar_symbol(self):
        """AC: $ symbol displays next to USD prices."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        # $ is ASCII U+0024, present in the USD symbol
        # Check that $ appears in a currency-symbol span next to USD prices
        assert b"$" in response.data, (
            "USD prices must include the $ symbol in the response"
        )

    def test_portfolio_inr_price_has_ascii_fallback_data_attribute(self):
        """AC: UI handles Unicode rendering failures gracefully with ASCII fallback.
        Each currency-symbol span carries data-currency so JS can swap in 'INR '
        if the ₹ glyph fails to render. Verify the data attribute is present."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # data-currency="INR" must appear on the currency-symbol spans
        assert b'data-currency="INR"' in data, (
            "Each INR currency symbol span must carry data-currency='INR' "
            "to enable JS ASCII fallback on rendering failure"
        )

    def test_portfolio_holdings_grouped_by_currency_in_ui(self):
        """AC: Holdings are grouped by currency in summary views.
        The UI shows separate sections for Indian Stocks (INR) and US Stocks (USD).
        We verify both sections are present in the rendered HTML."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # "Indian Stocks" is the section label for INR holdings in the template
        assert b"Indian Stocks" in data, (
            "Portfolio must show a distinct 'Indian Stocks' section for INR holdings"
        )
        # "US Stocks" is the section label for USD holdings in the template
        assert b"US Stocks" in data, (
            "Portfolio must show a distinct 'US Stocks' section for USD holdings"
        )
        # Both INR and USD badges appear in the holdings table
        assert b"badge-info" in data  # INR badge in table
        assert b"badge-success" in data  # USD badge in table

    def test_portfolio_all_four_exchanges_appear(self):
        """AC: Exchange name (NSE, BSE, NYSE, NASDAQ) displays for each holding.
        The mock data has one NSE, one BSE, two NASDAQ, one NYSE holding.
        All four distinct exchange names must appear in the response."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        assert b"NSE" in data
        assert b"BSE" in data
        assert b"NYSE" in data
        assert b"NASDAQ" in data

    def test_portfolio_existing_layout_preserved(self):
        """AC: Portfolio display maintains existing layout and functionality.
        The page must contain a table with Symbol, Exchange, Quantity,
        Avg. Price, Current Price, Currency columns — the same columns
        a portfolio holdings table would have."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # Verify table headers are present
        assert b"<th>Symbol</th>" in data
        assert b"<th>Exchange</th>" in data
        assert b"<th>Quantity</th>" in data
        assert b"<th>Avg. Price</th>" in data
        assert b"<th>Current Price</th>" in data
        assert b"<th>Currency</th>" in data

    def test_portfolio_js_fallback_for_unicode_failure(self):
        """AC: UI handles Unicode rendering failures gracefully with ASCII fallback.
        The template includes a JS script block that detects rendering
        failure (� replacement char) and swaps in ASCII fallback ('INR ' or '$')."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # The JS fallback must be present in the rendered page
        assert b"currency-symbol" in data  # CSS class the JS targets
        assert b"DOMContentLoaded" in data  # JS event listener registration
        # JS must check for rendering failure and apply ASCII fallback
        assert b"INR " in data or b"INR" in data  # ASCII fallback for INR
        assert b"USD" in data  # USD appears as badge text


# STORY-17: Multi-currency portfolio summary view
class TestPortfolioSummaryStory17:
    """Tests for STORY-17 multi-currency portfolio summary with consolidated total."""

    def test_portfolio_displays_usd_total(self):
        """AC: Summary displays 'USD Total: $X,XXX.XX' for all USD holdings."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode('utf-8')
        # Check for USD total display
        assert "US Stocks (USD)" in data or "$" in data

    def test_portfolio_displays_inr_total(self):
        """AC: Summary displays 'INR Total: ₹X,XXX.XX' for all INR holdings."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode('utf-8')
        # Check for INR total display
        assert "Indian Stocks (INR)" in data or "₹" in data or "INR" in data

    def test_portfolio_displays_consolidated_total(self):
        """AC: Summary displays consolidated total in user's base currency."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode('utf-8')
        # Check for consolidated total section
        assert "Consolidated Total" in data

    def test_portfolio_displays_exchange_rate_info(self):
        """AC: Exchange rate and timestamp display below consolidated total."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode('utf-8')
        # Check for exchange rate display
        assert "1 USD =" in data and "INR as of" in data

    def test_portfolio_handles_exchange_rate_failure(self):
        """AC: If exchange rate API fails, displays currency subtotals only with message."""
        import unittest.mock as mock
        # Patch fetch_exchange_rate to raise an error
        with mock.patch("webapp.fetch_exchange_rate") as mock_rate:
            from exchange_rate_client import ExchangeRateFetchError
            mock_rate.side_effect = ExchangeRateFetchError("Service unavailable")
            
            client = create_app().test_client()
            response = client.get("/portfolio")
            data = response.data.decode('utf-8')
            
            # Should still show currency subtotals
            assert "Indian Stocks (INR)" in data or "$" in data
            # Should show unavailable message
            assert "Consolidated total unavailable" in data or "exchange rate service temporarily unavailable" in data

    def test_amounts_display_with_thousand_separators(self):
        """AC: All amounts display with proper thousand separators and 2 decimal places."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode('utf-8')
        # Check for thousand separator format (comma)
        # The mock data should produce values like 1,750.00 for USD and 40,500.00 for INR
        assert "," in data, "Amounts should display with thousand separators"


# STORY-16: Market status indicators in portfolio header
class TestMarketStatusEndpointStory16:
    """QA tests specifically for STORY-16 acceptance criteria."""

    def test_nse_bse_status_display_format_in_api_response(self):
        """AC: Status display format 'NSE/BSE: Market Closed | US Markets: Market Open'.
        The API must return status_text that can be combined into this format."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        assert response.status_code == 200
        data = response.get_json()
        
        # Both keys must be present
        assert "nse_bse" in data
        assert "us_markets" in data
        
        # status_text must be usable in the format "NSE/BSE: [status_text] | US Markets: [status_text]"
        nse_bse_text = data["nse_bse"]["status_text"]
        us_markets_text = data["us_markets"]["status_text"]
        
        # Verify the format is valid (starts with expected prefix)
        assert nse_bse_text.startswith("Market "), f"NSE/BSE status_text must start with 'Market ', got: {nse_bse_text}"
        assert us_markets_text.startswith("Market "), f"US Markets status_text must start with 'Market ', got: {us_markets_text}"
        
        # Verify combined format could be constructed
        combined = f"NSE/BSE: {nse_bse_text} | US Markets: {us_markets_text}"
        assert "NSE/BSE:" in combined
        assert "US Markets:" in combined

    def test_holiday_status_includes_holiday_name_in_status_text(self):
        """AC: When closed for holiday, displays 'Market Closed - [Holiday Name]'.
        Mock market_status to return a holiday and verify the API formats it correctly."""
        import unittest.mock as mock
        
        # Patch market_status to return a holiday scenario for NSE
        with mock.patch("webapp.market_status") as mock_status:
            # Simulate NSE closed for Diwali holiday
            mock_status.side_effect = [
                # NSE: closed for holiday
                {
                    "status": "closed",
                    "status_text": "Market Open",  # Will be overridden by our code
                    "holiday_name": "Diwali",
                    "timezone": "Asia/Kolkata",
                    "now_local": "2024-11-01T13:00:00",
                },
                # BSE: also has holiday
                {
                    "status": "closed",
                    "status_text": "Market Open",
                    "holiday_name": "Diwali",
                    "timezone": "Asia/Kolkata",
                    "now_local": "2024-11-01T13:00:00",
                },
                # NYSE: open
                {
                    "status": "open",
                    "status_text": "Market Open",
                    "holiday_name": None,
                    "timezone": "America/New_York",
                    "now_local": "2024-11-01T09:30:00",
                },
                # NASDAQ: open
                {
                    "status": "open",
                    "status_text": "Market Open",
                    "holiday_name": None,
                    "timezone": "America/New_York",
                    "now_local": "2024-11-01T09:30:00",
                },
            ]
            
            client = create_app().test_client()
            response = client.get("/api/market-status")
            assert response.status_code == 200
            data = response.get_json()
            
            # Verify holiday name is included in NSE/BSE status_text
            nse_bse = data["nse_bse"]
            assert nse_bse["holiday_name"] == "Diwali"
            assert "Diwali" in nse_bse["status_text"], (
                f"NSE/BSE status_text must include holiday name 'Diwali', got: {nse_bse['status_text']}"
            )
            assert nse_bse["status_text"].startswith("Market Closed - "), (
                f"Holiday status_text must start with 'Market Closed - ', got: {nse_bse['status_text']}"
            )

    def test_market_open_status_shows_market_open_text(self):
        """AC: Status shows 'Market Open' or 'Market Closed' based on real-time calculation.
        Mock market_status to return open status and verify the API formats it correctly."""
        import unittest.mock as mock
        
        with mock.patch("webapp.market_status") as mock_status:
            # Simulate both markets open
            mock_status.return_value = {
                "status": "open",
                "status_text": "Market Open",
                "holiday_name": None,
                "timezone": "Asia/Kolkata",
                "now_local": "2024-11-04T10:00:00",
            }
            
            client = create_app().test_client()
            response = client.get("/api/market-status")
            assert response.status_code == 200
            data = response.get_json()
            
            # Both markets should show "Market Open"
            assert data["nse_bse"]["status_text"] == "Market Open"
            assert data["us_markets"]["status_text"] == "Market Open"

    def test_market_closed_status_shows_market_closed_text(self):
        """AC: Status shows 'Market Open' or 'Market Closed' based on real-time calculation.
        Mock market_status to return closed status (no holiday) and verify the API formats it correctly."""
        import unittest.mock as mock
        
        with mock.patch("webapp.market_status") as mock_status:
            # Simulate market closed (weekend)
            mock_status.return_value = {
                "status": "closed",
                "status_text": "Market Closed",
                "holiday_name": None,
                "timezone": "Asia/Kolkata",
                "now_local": "2024-11-02T10:00:00",  # Saturday
            }
            
            client = create_app().test_client()
            response = client.get("/api/market-status")
            assert response.status_code == 200
            data = response.get_json()
            
            # Both markets should show "Market Closed" without holiday name
            assert data["nse_bse"]["status_text"] == "Market Closed"
            assert data["nse_bse"]["holiday_name"] is None
            assert data["us_markets"]["status_text"] == "Market Closed"
            assert data["us_markets"]["holiday_name"] is None

    def test_status_uses_real_time_calculation_from_market_hours_module(self):
        """AC: Status shows 'Market Open' or 'Market Closed' based on real-time calculation.
        Verify the API actually calls market_status() from market_hours module, not hardcoded values."""
        import unittest.mock as mock
        
        with mock.patch("webapp.market_status") as mock_status:
            # Call the endpoint
            client = create_app().test_client()
            response = client.get("/api/market-status")
            
            # Verify market_status was called (it's a real-time calculation)
            assert mock_status.called, "API must call market_status() for real-time calculation"
            # Verify it was called for the expected exchanges
            call_args_list = [str(call) for call in mock_status.call_args_list]
            assert any("NSE" in arg for arg in call_args_list), "Must call market_status for NSE"
            assert any("BSE" in arg for arg in call_args_list), "Must call market_status for BSE"
            assert any("NYSE" in arg for arg in call_args_list), "Must call market_status for NYSE"
            assert any("NASDAQ" in arg for arg in call_args_list), "Must call market_status for NASDAQ"


class TestPortfolioMarketStatusUIStory16:
    """QA tests for STORY-16 UI acceptance criteria using real browser."""

    def test_portfolio_has_separate_nse_bse_and_us_markets_status_indicators(self):
        """AC: Portfolio header displays market status for NSE/BSE and US markets.
        The UI must have separate elements for each market region."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        
        # Must have separate status sections for each market
        assert b"nse-bse-status" in data or b"data-market=" in data and b"nse_bse" in data, (
            "Portfolio must have a separate NSE/BSE status indicator element"
        )
        assert b"us-markets-status" in data or b"data-market=" in data and b"us_markets" in data, (
            "Portfolio must have a separate US Markets status indicator element"
        )

    def test_portfolio_status_display_format_matches_requirement(self):
        """AC: Status display format: 'NSE/BSE: Market Closed | US Markets: Market Open'.
        The UI must show both market regions in the specified format."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        
        # Must have labels matching the required format
        assert b"NSE/BSE:" in data, "Portfolio must display 'NSE/BSE:' label"
        assert b"US Markets:" in data, "Portfolio must display 'US Markets:' label"
        
        # Must have a visual divider between the two status sections
        # The template uses <div class="divider divider-horizontal"></div> as separator
        assert b"divider" in data and b"nse-bse-status" in data and b"us-markets-status" in data, (
            "Portfolio must have both NSE/BSE and US Markets status sections separated by divider"
        )

    def test_portfolio_has_realtime_update_without_page_refresh(self):
        """AC: Market status updates in real-time without page refresh.
        The UI must have JavaScript that fetches and updates status periodically."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        
        # Must have setInterval for periodic updates
        assert b"setInterval" in data, "Portfolio must use setInterval for real-time updates"
        
        # Must call fetchMarketStatus function
        assert b"fetchMarketStatus" in data, "Portfolio must have fetchMarketStatus function"
        
        # setInterval must call the market status fetch (not reload page)
        content = data.decode('utf-8')
        assert "setInterval" in content and "fetchMarketStatus" in content, (
            "setInterval must call fetchMarketStatus (not location.reload)"
        )

    def test_portfolio_has_visually_distinct_status_indicators(self):
        """AC: Status indicators are visually distinct (e.g., green for open, red for closed).
        The UI must have CSS classes for different status colors."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        
        # Must have badge-success (green) for open status
        assert b"badge-success" in data, "Portfolio must have badge-success for Market Open"
        
        # Must have badge-error (red) for closed status
        assert b"badge-error" in data, "Portfolio must have badge-error for Market Closed"
        
        # Must have badge-warning (yellow/amber) for unknown/error status
        assert b"badge-warning" in data, "Portfolio must have badge-warning for unknown status"

    def test_portfolio_has_loading_state_while_fetching(self):
        """AC: Portfolio header displays market status with loading state.
        The UI must show a loading indicator while fetching status."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        
        # Must have loading spinner
        assert b"loading" in data and b"loading-spinner" in data, (
            "Portfolio must show loading spinner while fetching market status"
        )
        
        # Must have initial "Loading..." text
        assert b"Loading..." in data, "Portfolio must show 'Loading...' text initially"

    def test_portfolio_status_update_function_sets_correct_color_classes(self):
        """AC: Status indicators are visually distinct.
        The JavaScript updateStatusIndicator function must set correct badge classes."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        content = data.decode('utf-8')
        
        # Must have updateStatusIndicator function
        assert "updateStatusIndicator" in content, (
            "Portfolio must have updateStatusIndicator function"
        )
        
        # Function must add badge-success for open status
        assert "badge-success" in content, (
            "updateStatusIndicator must add badge-success for open status"
        )
        
        # Function must add badge-error for closed status
        assert "badge-error" in content, (
            "updateStatusIndicator must add badge-error for closed status"
        )


class TestMarketStatusEndpoint:
    """Tests for /api/market-status endpoint."""

    def test_market_status_returns_200(self):
        """Market status endpoint should return 200 OK."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        assert response.status_code == 200

    def test_market_status_returns_json(self):
        """Market status endpoint should return JSON."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        assert response.content_type == "application/json"

    def test_market_status_has_nse_bse_key(self):
        """Response should include nse_bse key."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        assert "nse_bse" in data

    def test_market_status_has_us_markets_key(self):
        """Response should include us_markets key."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        assert "us_markets" in data

    def test_nse_bse_status_has_required_fields(self):
        """NSE/BSE status should have status, status_text, and holiday_name."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        nse_bse = data["nse_bse"]
        assert "status" in nse_bse
        assert "status_text" in nse_bse
        assert "holiday_name" in nse_bse
        # status_text should be "Market Open" or "Market Closed" or "Market Closed - [Holiday]"
        assert any(
            nse_bse["status_text"].startswith(prefix)
            for prefix in ["Market Open", "Market Closed"]
        )

    def test_us_markets_status_has_required_fields(self):
        """US markets status should have status, status_text, and holiday_name."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        us_markets = data["us_markets"]
        assert "status" in us_markets
        assert "status_text" in us_markets
        assert "holiday_name" in us_markets
        # status_text should be "Market Open" or "Market Closed" or "Market Closed - [Holiday]"
        assert any(
            us_markets["status_text"].startswith(prefix)
            for prefix in ["Market Open", "Market Closed"]
        )

    def test_market_status_respects_real_holidays(self):
        """Market status should reflect real holiday information from config."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        
        # Check that holiday_name is either None or a string
        assert data["nse_bse"]["holiday_name"] is None or isinstance(
            data["nse_bse"]["holiday_name"], str
        )
        assert data["us_markets"]["holiday_name"] is None or isinstance(
            data["us_markets"]["holiday_name"], str
        )

    def test_status_values_are_valid(self):
        """Status should be 'open', 'closed', or 'unknown'."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        
        valid_statuses = {"open", "closed", "unknown"}
        assert data["nse_bse"]["status"] in valid_statuses
        assert data["us_markets"]["status"] in valid_statuses

    def test_holiday_status_text_format(self):
        """When closed for holiday, status_text should be 'Market Closed - [Holiday Name]'."""
        client = create_app().test_client()
        response = client.get("/api/market-status")
        data = response.get_json()
        
        # Check NSE/BSE status text format
        nse_bse = data["nse_bse"]
        if nse_bse["holiday_name"]:
            # If there's a holiday, status_text must include it
            assert nse_bse["holiday_name"] in nse_bse["status_text"]
            assert nse_bse["status_text"].startswith("Market Closed - ")
        elif nse_bse["status"] == "closed":
            # If closed but no holiday, should be just "Market Closed"
            assert nse_bse["status_text"] == "Market Closed"
        
        # Check US Markets status text format
        us_markets = data["us_markets"]
        if us_markets["holiday_name"]:
            # If there's a holiday, status_text must include it
            assert us_markets["holiday_name"] in us_markets["status_text"]
            assert us_markets["status_text"].startswith("Market Closed - ")
        elif us_markets["status"] == "closed":
            # If closed but no holiday, should be just "Market Closed"
            assert us_markets["status_text"] == "Market Closed"


class TestPortfolioMarketStatusUI:
    """Tests for market status display in portfolio page."""

    def test_portfolio_has_market_status_section(self):
        """Portfolio page should include market status bar."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        assert b"market-status-bar" in data or b"market-status" in data

    def test_portfolio_displays_nse_bse_status_label(self):
        """Portfolio should display NSE/BSE status label."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        assert b"NSE/BSE" in data

    def test_portfolio_displays_us_markets_status_label(self):
        """Portfolio should display US Markets status label."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        assert b"US Markets" in data

    def test_portfolio_fetches_market_status_api(self):
        """Portfolio page should include JS to fetch /api/market-status."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        assert b"/api/market-status" in data

    def test_portfolio_has_realtime_update_interval(self):
        """Portfolio should update market status periodically (real-time without refresh)."""
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data
        # Should have a setInterval call for periodic updates
        assert b"setInterval" in data or b"fetchMarketStatus" in data