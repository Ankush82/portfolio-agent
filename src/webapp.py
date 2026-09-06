"""Minimal Flask web app scaffold for the portfolio agent's UI stories
(STORY-14 through STORY-18). This project had no frontend at all
before this scaffold -- server-rendered Jinja2 templates (no Node/npm
build step) is the natural fit for a pure-Python project, so each UI
story's routes/templates are added under this same app factory rather
than introducing a separate JS toolchain.

Run locally with:

    FLASK_APP=src.webapp flask run

or, since `pythonpath = ["src", "scripts"]` is already configured for
pytest, simply:

    cd src && flask --app webapp run
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, render_template
from yahoo_finance_client import fetch_yahoo_finance_quote, YahooFinanceError

# Currency symbol constants for Unicode with ASCII fallback
_CURRENCY_SYMBOLS = {
    "INR": "₹",  # Unicode Indian Rupee sign
    "USD": "$",
}
_CURRENCY_ASCII_FALLBACK = {
    "INR": "INR ",  # ASCII fallback for INR
    "USD": "$",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _REPO_ROOT / "templates"
_STATIC_DIR = _REPO_ROOT / "static"

# Quantum for currency formatting: 2 decimal places
_PRICE_QUANTUM = Decimal("0.01")


def get_currency_symbol(currency: str) -> str:
    """Get currency symbol for display.
    
    Uses Unicode symbols (₹ for INR, $ for USD) with ASCII fallback
    for currencies that don't have a Unicode representation.
    
    Args:
        currency: Currency code (INR, USD, etc.)
        
    Returns:
        Currency symbol string
    """
    return _CURRENCY_SYMBOLS.get(currency, _CURRENCY_ASCII_FALLBACK.get(currency, currency + " "))


def format_price(value: Any) -> str:
    """Format a price/value to 2 decimal places.
    
    Args:
        value: Numeric value (int, float, Decimal, or string)
        
    Returns:
        String formatted to 2 decimal places
    """
    try:
        decimal_value = Decimal(str(value))
        return str(decimal_value.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP))
    except (ValueError, TypeError):
        return str(value)


def format_quantity(value: Any) -> str:
    """Format a quantity value.
    
    Args:
        value: Numeric value (int, float, Decimal, or string)
        
    Returns:
        String formatted to 4 decimal places
    """
    try:
        decimal_value = Decimal(str(value))
        return str(decimal_value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    except (ValueError, TypeError):
        return str(value)


def _group_holdings_by_currency(holdings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group holdings by currency (INR vs USD).
    
    Args:
        holdings: List of holding dictionaries with 'currency' key
        
    Returns:
        Tuple of (inr_holdings, usd_holdings)
    """
    inr_holdings = [h for h in holdings if h.get("currency") == "INR"]
    usd_holdings = [h for h in holdings if h.get("currency") == "USD"]
    return inr_holdings, usd_holdings


def _calculate_totals(holdings: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    """Calculate totals for each currency.
    
    Args:
        holdings: List of holding dictionaries
        
    Returns:
        Tuple of (inr_total, usd_total) as Decimal or None
    """
    inr_total = Decimal("0")
    usd_total = Decimal("0")
    has_inr = False
    has_usd = False
    
    for holding in holdings:
        currency = holding.get("currency", "USD")
        # Use last_price if available, otherwise average_price
        price = holding.get("last_price") or holding.get("average_price") or Decimal("0")
        quantity = holding.get("quantity") or Decimal("0")
        
        try:
            value = Decimal(str(price)) * Decimal(str(quantity))
        except (ValueError, TypeError):
            value = Decimal("0")
        
        if currency == "INR":
            inr_total += value
            has_inr = True
        else:  # Default to USD
            usd_total += value
            has_usd = True
    
    return (inr_total if has_inr else None), (usd_total if has_usd else None)


def create_app() -> Flask:
    """App-factory pattern (the real, standard Flask idiom) so tests
    can create isolated app instances rather than importing a single
    module-level global app object."""
    app = Flask(
        __name__,
        template_folder=str(_TEMPLATES_DIR),
        static_folder=str(_STATIC_DIR),
    )

    @app.get("/")
    def index():
        return "Portfolio Agent is running.", 200

    @app.get("/health")
    def health():
        return {"status": "ok"}, 200

    @app.get("/stock_entry")
    def stock_entry():
        return render_template("stock_entry.html")

    # Register custom template filters and functions for portfolio display
    @app.template_filter("format_price")
    def _format_price(value):
        return format_price(value)

    @app.template_filter("format_quantity")
    def _format_quantity(value):
        return format_quantity(value)

    @app.context_processor
    def _inject_currency_helpers():
        """Inject currency helpers into template context."""
        return {
            "get_currency_symbol": get_currency_symbol,
        }

    @app.route("/portfolio")
    def portfolio():
        """Display the user's portfolio with currency symbols and exchange names.
        
        This endpoint displays holdings grouped by currency (INR/USD),
        showing appropriate currency symbols (₹ for INR, $ for USD)
        next to prices and displaying exchange names for each holding.
        """
        # Mock holdings data for demonstration - in production this
        # would come from the user's connected broker or manual entries
        holdings = [
            {
                "symbol": "RELIANCE.NS",
                "exchange": "NSE",
                "quantity": Decimal("5"),
                "average_price": Decimal("2400.00"),
                "last_price": Decimal("2500.00"),
                "currency": "INR",
            },
            {
                "symbol": "500325.BO",
                "exchange": "BSE",
                "quantity": Decimal("10"),
                "average_price": Decimal("1500.00"),
                "last_price": Decimal("1550.00"),
                "currency": "INR",
            },
            {
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "quantity": Decimal("10"),
                "average_price": Decimal("150.00"),
                "last_price": Decimal("175.00"),
                "currency": "USD",
            },
            {
                "symbol": "GOOGL",
                "exchange": "NASDAQ",
                "quantity": Decimal("2"),
                "average_price": Decimal("140.00"),
                "last_price": Decimal("142.00"),
                "currency": "USD",
            },
            {
                "symbol": "JPM",
                "exchange": "NYSE",
                "quantity": Decimal("5"),
                "average_price": Decimal("195.00"),
                "last_price": Decimal("200.00"),
                "currency": "USD",
            },
        ]

        # Convert Decimal to string for JSON serialization in templates
        holdings_serializable = []
        for h in holdings:
            holding_dict = dict(h)
            for key in ["quantity", "average_price", "last_price"]:
                if isinstance(holding_dict.get(key), Decimal):
                    holding_dict[key] = str(holding_dict[key])
            holdings_serializable.append(holding_dict)

        # Group holdings by currency
        inr_holdings, usd_holdings = _group_holdings_by_currency(holdings_serializable)

        # Calculate totals for each currency
        inr_total, usd_total = _calculate_totals(holdings_serializable)

        # Get currency symbols with ASCII fallback
        inr_symbol = get_currency_symbol("INR")
        usd_symbol = get_currency_symbol("USD")

        # Format totals to 2 decimal places
        inr_total_str = format_price(inr_total) if inr_total else None
        usd_total_str = format_price(usd_total) if usd_total else None

        return render_template(
            "portfolio.html",
            holdings=holdings_serializable,
            inr_holdings=inr_holdings,
            usd_holdings=usd_holdings,
            inr_total=inr_total_str,
            usd_total=usd_total_str,
            inr_symbol=inr_symbol,
            usd_symbol=usd_symbol,
        )

    def _validate_indian_stock_symbol_format(symbol: str) -> tuple[bool, str]:
        """Validate Indian stock symbol format (NSE/BSE).
        Returns (is_valid, error_message)."""
        if symbol.endswith('.NS'):
            body = symbol[:-3]
            if not body or not re.match(r'^[A-Z0-9&\-]{1,20}$', body):
                return False, f"invalid NSE stock symbol: body before '.NS' must be 1-20 characters from [A-Z0-9&-]; got body {json.dumps(body)}"
            return True, ""
        elif symbol.endswith('.BO'):
            body = symbol[:-3]
            if not body or not re.match(r'^[0-9]{6}$', body):
                return False, f"invalid BSE stock symbol: body before '.BO' must be exactly 6 digits; got body {json.dumps(body)}"
            return True, ""
        elif symbol.endswith('.ns') or symbol.endswith('.bo'):
            return False, "invalid stock symbol: suffix is case-sensitive (use '.NS' or '.BO')"
        # For non-Indian suffixes, we consider format validation passed (no error)
        return True, ""

    @app.post("/validate_symbol")
    def validate_symbol():
        data = request.get_json()
        if not data or "symbol" not in data:
            return jsonify({"error": "Missing symbol"}), 400

        symbol = data["symbol"].strip()
        if not symbol:
            return jsonify({"error": "Symbol cannot be empty"}), 400

        # Client-side format validation for Indian stock symbols (mirrored server-side)
        is_valid, error_msg = _validate_indian_stock_symbol_format(symbol)
        if not is_valid:
            return jsonify({"error": error_msg}), 400

        try:
            quote = fetch_yahoo_finance_quote(symbol)
            # Extract exchange and currency from the quote
            exchange = quote.get("exchange_name")
            currency = quote.get("currency")
            # If exchange_name is not available, try to derive from symbol suffix
            if not exchange:
                if symbol.endswith(".NS"):
                    exchange = "NSE"
                elif symbol.endswith(".BO"):
                    exchange = "BSE"
            return jsonify({"exchange": exchange, "currency": currency})
        except YahooFinanceError as e:
            # Return a user-friendly error message
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            # Catch-all for unexpected errors
            return jsonify({"error": "An unexpected error occurred"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
