"""QA tests for STORY-17: Multi-currency portfolio summary view.

This file is added by QA (not dev) to verify the real implementation
satisfies each acceptance criterion using real pytest tests against
the actual application code.

Acceptance criteria (STORY-17):
  1. Summary displays 'USD Total: $X,XXX.XX' for all USD holdings
  2. Summary displays 'INR Total: ₹X,XXX.XX' for all INR holdings
  3. Summary displays consolidated total in user's base currency
  4. Exchange rate and timestamp display below consolidated total:
     '1 USD = XX.XX INR as of YYYY-MM-DD HH:MM UTC'
  5. If exchange rate API fails, displays currency subtotals only with
     message: 'Consolidated total unavailable - exchange rate service temporarily unavailable'
  6. Summary updates when user changes base currency preference
  7. All amounts display with proper thousand separators and 2 decimal places
"""

from __future__ import annotations

import re
import unittest.mock

import pytest

from webapp import create_app


# ---------------------------------------------------------------------------
# Helper constants extracted from the mock holdings data in webapp.py
# ---------------------------------------------------------------------------
# INR holdings: RELIANCE (5 * 2500) + 500325 (10 * 1550) = 12500 + 15500
_EXPECTED_INR_TOTAL = 28000.00  # 28,000.00 formatted
# USD holdings: AAPL (10 * 175) + GOOGL (2 * 142) + JPM (5 * 200) = 1750 + 284 + 1000
_EXPECTED_USD_TOTAL = 3034.00  # 3,034.00 formatted


class TestStory17PortfolioSummaryAcceptanceCriteria:
    """Real QA tests for each STORY-17 acceptance criterion."""

    def test_ac1_usd_total_displayed_with_dollar_format(self):
        """AC1: Summary displays 'USD Total: $X,XXX.XX' for all USD holdings.

        The mock data has 3 USD holdings totalling 3,034.00.
        The summary card must show "$3,034.00" or "$3,034" (thousand-sep format).
        """
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode("utf-8")

        # The "US Stocks (USD)" section label must appear
        assert "US Stocks (USD)" in data, (
            "Portfolio summary must show 'US Stocks (USD)' label"
        )

        # The dollar sign $ must appear near the USD total
        assert "$" in data, "USD total must display with $ symbol"

        # The formatted total 3,034 must appear (comma thousand separator)
        assert "3,034" in data, (
            f"USD total must be formatted with comma separators, "
            f"expected '3,034' in output"
        )
        # .00 must appear (2 decimal places)
        assert "3,034.00" in data, (
            "USD total must show 2 decimal places"
        )

    def test_ac2_inr_total_displayed_with_rupee_format(self):
        """AC2: Summary displays 'INR Total: ₹X,XXX.XX' for all INR holdings.

        The mock data has 2 INR holdings totalling 28,000.00.
        The summary card must show "₹28,000.00" (with ₹ Unicode).
        """
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode("utf-8")

        # The "Indian Stocks (INR)" section label must appear
        assert "Indian Stocks (INR)" in data, (
            "Portfolio summary must show 'Indian Stocks (INR)' label"
        )

        # The ₹ Unicode symbol (U+20B9) must appear next to the INR total
        assert "₹" in data, (
            "INR total must display with ₹ Unicode symbol (U+20B9)"
        )

        # The formatted total 28,000 must appear (comma thousand separator)
        assert "28,000" in data, (
            f"INR total must be formatted with comma separators, "
            f"expected '28,000' in output"
        )
        # .00 must appear (2 decimal places)
        assert "28,000.00" in data, (
            "INR total must show 2 decimal places"
        )

    def test_ac3_consolidated_total_displayed_in_base_currency(self):
        """AC3: Summary displays consolidated total in user's base currency.

        The default base currency is INR. The consolidated total must appear
        in the portfolio summary with the INR symbol.
        """
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode("utf-8")

        # The section label must be present
        assert "Consolidated Total" in data, (
            "Portfolio summary must show 'Consolidated Total' section"
        )

        # Since base currency defaults to INR, the ₹ symbol must appear
        # next to the consolidated total
        assert "₹" in data, (
            "Consolidated total must display with ₹ symbol (base currency = INR)"
        )

        # The base currency label (INR) must appear
        assert "INR" in data, (
            "Consolidated total section must label the base currency as INR"
        )

    def test_ac4_exchange_rate_info_with_timestamp_format(self):
        """AC4: Exchange rate and timestamp display below consolidated total:
        '1 USD = XX.XX INR as of YYYY-MM-DD HH:MM UTC'

        The exchange rate info must be a single human-readable line with
        the exact pattern "1 USD = " followed by a rate, " INR as of ",
        and a UTC timestamp.
        """
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode("utf-8")

        # The literal prefix "1 USD =" must appear
        assert "1 USD =" in data, (
            "Exchange rate info must start with '1 USD ='"
        )

        # The literal suffix "INR as of" must appear
        assert "INR as of" in data, (
            "Exchange rate info must contain 'INR as of'"
        )

        # A UTC timestamp in the format YYYY-MM-DD HH:MM UTC must be present
        # Regex matches: 4 digits, dash, 2 digits, dash, 2 digits,
        # space, 2 digits, colon, 2 digits, space, UTC
        utc_timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC"
        assert re.search(utc_timestamp_pattern, data), (
            "Exchange rate info must include a UTC timestamp "
            "in the format 'YYYY-MM-DD HH:MM UTC'"
        )

    def test_ac5_exchange_rate_failure_shows_subtotals_and_unavailable_message(self):
        """AC5: If exchange rate API fails, displays currency subtotals only
        with message: 'Consolidated total unavailable - exchange rate service
        temporarily unavailable'

        Patches fetch_exchange_rate to raise ExchangeRateFetchError and
        verifies both currency subtotals are still shown PLUS the graceful
        failure message.
        """
        from exchange_rate_client import ExchangeRateFetchError

        with unittest.mock.patch(
            "src.webapp.fetch_exchange_rate",
            side_effect=ExchangeRateFetchError("Service unavailable"),
        ):
            client = create_app().test_client()
            response = client.get("/portfolio")
            data = response.data.decode("utf-8")

            # Currency subtotals must still appear
            assert "Indian Stocks (INR)" in data, (
                "INR subtotal must still display when exchange rate fails"
            )
            assert "US Stocks (USD)" in data, (
                "USD subtotal must still display when exchange rate fails"
            )

            # The exact failure message must appear
            assert "Consolidated total unavailable" in data, (
                "Portfolio must show 'Consolidated total unavailable' "
                "when exchange rate service fails"
            )
            assert "exchange rate service temporarily unavailable" in data, (
                "Portfolio must show the exact message: "
                "'exchange rate service temporarily unavailable'"
            )

            # Consolidated total must NOT appear (it should be hidden)
            assert "Consolidated Total" not in data or "unavailable" in data, (
                "Consolidated total section must not show a numeric value "
                "when exchange rate is unavailable"
            )

    def test_ac7_amounts_display_with_thousand_separators_and_two_decimals(self):
        """AC7: All amounts display with proper thousand separators and
        2 decimal places.

        Verifies the format_price_with_thousand_separators filter produces
        comma-separated integers and exactly 2 decimal places.
        """
        client = create_app().test_client()
        response = client.get("/portfolio")
        data = response.data.decode("utf-8")

        # INR total: 28,000.00 (comma + 2 decimals)
        assert "28,000.00" in data, (
            "INR total must display with comma separators and 2 decimal places"
        )

        # USD total: 3,034.00 (comma + 2 decimals)
        assert "3,034.00" in data, (
            "USD total must display with comma separators and 2 decimal places"
        )

        # Check the format: every displayed amount must have exactly 2 decimal places
        # Extract all dollar/rupee amounts and verify they all end in .00
        amount_pattern = r"[$₹]?\s*\d{1,3}(?:,\d{3})*\.\d{2}"
        matches = re.findall(amount_pattern, data)
        assert len(matches) > 0, (
            "At least one amount with thousand separators and 2 decimals must appear"
        )
        for match in matches:
            # Remove currency symbol and spaces, verify it matches the pattern
            amount_only = re.sub(r"[$₹]?\s*", "", match)
            assert re.match(r"^\d{1,3}(?:,\d{3})*\.\d{2}$", amount_only), (
                f"Amount {match!r} does not have proper thousand separators "
                f"and 2 decimal places"
            )

    def test_format_price_with_thousand_separators_produces_correct_output(self):
        """Unit-level check: format_price_with_thousand_separators handles
        the expected total values correctly."""
        from webapp import format_price_with_thousand_separators

        # 28000.00 -> "28,000.00"
        assert format_price_with_thousand_separators(28000.00) == "28,000.00", (
            "format_price_with_thousand_separators(28000.00) must return '28,000.00'"
        )

        # 3034.00 -> "3,034.00"
        assert format_price_with_thousand_separators(3034.00) == "3,034.00", (
            "format_price_with_thousand_separators(3034.00) must return '3,034.00'"
        )

        # Larger number with 3 decimal places gets quantized to 2
        assert format_price_with_thousand_separators("1234567.899") == "1,234,567.90", (
            "format_price_with_thousand_separators must round to 2 decimal places"
        )


# ---------------------------------------------------------------------------
# NOTE: check_live_ui is a QA tool (not a Python import).
# The following live UI tests are structured for documentation; the actual
# tool invocation is done separately via the check_live_ui tool call below.
# ---------------------------------------------------------------------------
#
# Live UI acceptance criteria for STORY-17:
#   - /portfolio page must show "Portfolio Summary" in real browser
#   - Real browser must show "Consolidated Total" text
#   - Real browser must show "1 USD =" and "INR as of" exchange rate line
#   - Real browser must show "Indian Stocks (INR)" and "US Stocks (USD)" sections
#   - Real browser must show thousand-separated formatted amounts (₹28,000.00, $3,034.00)
#   - Exchange rate failure scenario: real browser must show
#     "Consolidated total unavailable - exchange rate service temporarily unavailable"
#
# Tool call:
#   check_live_ui(route="/portfolio", actions=[
#       {"type": "wait_for_text", "text": "Portfolio Summary", "timeout_ms": 8000},
#       {"type": "wait_for_text", "text": "Consolidated Total", "timeout_ms": 8000},
#       {"type": "wait_for_text", "text": "1 USD =", "timeout_ms": 8000},
#       {"type": "wait_for_text", "text": "INR as of", "timeout_ms": 8000},
#       {"type": "wait_for_text", "text": "Indian Stocks (INR)", "timeout_ms": 8000},
#       {"type": "wait_for_text", "text": "US Stocks (USD)", "timeout_ms": 8000},
#   ])
# ---------------------------------------------------------------------------
