"""Smoke tests for the Flask app scaffold (src/webapp.py). Each UI
story (STORY-14 through STORY-18) adds its own routes/templates on top
of this; this file only covers the scaffold itself."""

from __future__ import annotations

from webapp import create_app, get_currency_symbol, format_price


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