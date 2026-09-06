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
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from yahoo_finance_client import fetch_yahoo_finance_quote, YahooFinanceError

from components.c01_user_portfolio import (
    BrokerAuthError,
    BrokerConfigError,
    DefaultUserPortfolio,
    get_broker_connector,
)
from oauth_state import (
    InvalidOAuthStateError,
    OAuthStateExpiredError,
    OAuthStateReplayError,
    consume_state,
    issue_state,
)
from exchange_rate_client import (
    ExchangeRateFetchError,
    MissingExchangeRateAPIKeyError,
    fetch_exchange_rate,
)
from market_hours import market_status, UnknownMarketError, MarketHoursConfigError
from infrastructure_postgres import DefaultInfrastructure

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

# Default base currency (can be overridden by user preference)
_DEFAULT_BASE_CURRENCY = "USD"


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


def format_price_with_thousand_separators(value: Any) -> str:
    """Format a price/value with thousand separators and 2 decimal places.
    
    Args:
        value: Numeric value (int, float, Decimal, or string)
        
    Returns:
        String formatted with thousand separators and 2 decimal places
        (e.g., '1,234,567.89')
    """
    try:
        decimal_value = Decimal(str(value)).quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        # Format with thousand separators
        integer_part, decimal_part = str(decimal_value).split('.')
        # Add thousand separators to integer part
        integer_with_separators = f"{int(integer_part):,}"
        return f"{integer_with_separators}.{decimal_part}"
    except (ValueError, TypeError):
        return str(value)


def get_user_base_currency(user_id: str | None = None) -> str:
    """Get the user's base currency preference.
    
    Reads from the database if user_id is provided, otherwise
    falls back to the default.
    
    Args:
        user_id: Optional user ID to look up the preference for.
                 If None, returns the default base currency.
        
    Returns:
        Base currency code (INR or USD)
    """
    # Use default for anonymous/unauthenticated users
    if user_id is None:
        return _DEFAULT_BASE_CURRENCY
    
    # Try to read from database
    try:
        infra = DefaultInfrastructure()
        setting = infra.get_user_setting(user_id, "base_currency")
        if setting in ("INR", "USD"):
            return setting
    except Exception:
        # If database is unavailable, fall back to default
        pass
    
    return _DEFAULT_BASE_CURRENCY


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
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

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

    @app.template_filter("format_price_with_thousand_separators")
    def _format_price_with_thousand_separators(value):
        return format_price_with_thousand_separators(value)

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
        
        STORY-17: Also displays consolidated total in user's base currency
        with exchange rate information.
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

        # Format totals to 2 decimal places with thousand separators
        inr_total_str = format_price_with_thousand_separators(inr_total) if inr_total else None
        usd_total_str = format_price_with_thousand_separators(usd_total) if usd_total else None

        # STORY-17: Calculate consolidated total in user's base currency
        user_id = session.get("user_id", "default-user")
        base_currency = get_user_base_currency(user_id)
        consolidated_total = None
        exchange_rate_info = None
        exchange_rate_unavailable = False

        # Try to fetch exchange rate for consolidated total calculation
        try:
            rate = fetch_exchange_rate()
            # Get the timestamp from the rate (defaults to now if not available)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            
            # Format exchange rate display
            rate_formatted = format_price(rate)
            exchange_rate_info = f"1 USD = {rate_formatted} INR as of {timestamp}"
            
            # Calculate consolidated total based on base currency
            inr_val = inr_total if inr_total else Decimal("0")
            usd_val = usd_total if usd_total else Decimal("0")
            
            if base_currency == "INR":
                # Convert USD to INR
                consolidated_total = inr_val + (usd_val * rate)
            else:
                # Convert INR to USD
                if rate > 0:
                    consolidated_total = usd_val + (inr_val / rate)
                else:
                    consolidated_total = None
        except (ExchangeRateFetchError, MissingExchangeRateAPIKeyError):
            # Exchange rate unavailable - show currency subtotals only
            exchange_rate_unavailable = True
            consolidated_total = None

        # Format consolidated total with thousand separators
        consolidated_total_str = (
            format_price_with_thousand_separators(consolidated_total) 
            if consolidated_total is not None else None
        )
        base_currency_symbol = get_currency_symbol(base_currency)

        return render_template(
            "portfolio.html",
            holdings=holdings_serializable,
            inr_holdings=inr_holdings,
            usd_holdings=usd_holdings,
            inr_total=inr_total_str,
            usd_total=usd_total_str,
            inr_symbol=inr_symbol,
            usd_symbol=usd_symbol,
            # STORY-17: Consolidated total in base currency
            consolidated_total=consolidated_total_str,
            base_currency=base_currency,
            base_currency_symbol=base_currency_symbol,
            exchange_rate_info=exchange_rate_info,
            exchange_rate_unavailable=exchange_rate_unavailable,
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

    @app.get("/api/market-status")
    def api_market_status():
        """Return market status for NSE/BSE and US markets.
        
        Returns JSON with status for both Indian and US markets,
        including holiday information when markets are closed for holidays.
        """
        result = {}
        
        # NSE/BSE status (use NSE as the representative)
        try:
            nse_status = market_status("NSE")
            bse_status = market_status("BSE")
            
            # Use NSE's status but check if BSE has a holiday when NSE doesn't
            nse_bse_info = {
                "status": nse_status["status"],
                "status_text": "Market Open" if nse_status["status"] == "open" else "Market Closed",
                "holiday_name": nse_status.get("holiday_name") or bse_status.get("holiday_name"),
                "timezone": nse_status.get("timezone"),
                "now_local": nse_status.get("now_local"),
            }
            if nse_bse_info["holiday_name"]:
                nse_bse_info["status_text"] = f"Market Closed - {nse_bse_info['holiday_name']}"
            result["nse_bse"] = nse_bse_info
        except (UnknownMarketError, MarketHoursConfigError) as e:
            result["nse_bse"] = {
                "status": "unknown",
                "status_text": "Status Unavailable",
                "holiday_name": None,
                "error": str(e),
            }
        
        # US markets status (use NYSE as the representative)
        try:
            nyse_status = market_status("NYSE")
            nasdaq_status = market_status("NASDAQ")
            
            us_markets_info = {
                "status": nyse_status["status"],
                "status_text": "Market Open" if nyse_status["status"] == "open" else "Market Closed",
                "holiday_name": nyse_status.get("holiday_name") or nasdaq_status.get("holiday_name"),
                "timezone": nyse_status.get("timezone"),
                "now_local": nyse_status.get("now_local"),
            }
            if us_markets_info["holiday_name"]:
                us_markets_info["status_text"] = f"Market Closed - {us_markets_info['holiday_name']}"
            result["us_markets"] = us_markets_info
        except (UnknownMarketError, MarketHoursConfigError) as e:
            result["us_markets"] = {
                "status": "unknown",
                "status_text": "Status Unavailable",
                "holiday_name": None,
                "error": str(e),
            }
        
        return jsonify(result)

    # STORY-18: User settings routes
    @app.get("/settings")
    def settings():
        """Display user settings page with base currency preference."""
        # For demo purposes, use a default user ID
        # In production, this would come from authentication
        user_id = session.get("user_id", "default-user")
        
        # Get current base currency setting
        current_base_currency = get_user_base_currency(user_id)
        
        return render_template(
            "settings.html",
            current_base_currency=current_base_currency,
            base_currency_options=["USD", "INR"],
        )

    @app.post("/settings/base-currency")
    def update_base_currency():
        """Update the user's base currency preference."""
        data = request.get_json() if request.is_json else request.form.to_dict()
        
        base_currency = data.get("base_currency")
        if base_currency not in ("USD", "INR"):
            return jsonify({"error": "Invalid currency. Must be USD or INR."}), 400
        
        # For demo purposes, use a default user ID
        # In production, this would come from authentication
        user_id = session.get("user_id", "default-user")
        
        try:
            infra = DefaultInfrastructure()
            infra.set_user_setting(user_id, "base_currency", base_currency)
            return jsonify({"success": True, "base_currency": base_currency})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/api/user/base-currency")
    def api_get_base_currency():
        """Get the user's current base currency preference (API endpoint)."""
        user_id = session.get("user_id", "default-user")
        base_currency = get_user_base_currency(user_id)
        return jsonify({"base_currency": base_currency})

    # -------------------------------------------------------------------------
    # STORY-16: Upstox OAuth connect flow — initiate
    # -------------------------------------------------------------------------

    @app.post("/api/brokers/upstox/connect")
    def api_brokers_upstox_connect():
        """POST /api/brokers/upstox/connect — start the Upstox OAuth flow.

        Authenticated users only (session must carry a user_id).
        Returns 200 with ``{"authorize_url": "...", "state": "..."}`` on
        success, where ``authorize_url`` is the Upstox authorization URL
        the frontend navigates to (302-redirect is deliberately omitted so
        the caller controls navigation).

        Returns 401 when no user is authenticated.

        Returns 503 with ``{"error": "broker_not_configured", "message": "..."}``
        when ``UPSTOX_CLIENT_ID``, ``UPSTOX_CLIENT_SECRET``, or
        ``UPSTOX_REDIRECT_URI`` is not set in the environment — the
        message names the missing variables so the operator can fix them
        without guesswork. No state token is created in this case.

        No network call to Upstox is made by this endpoint.
        """
        # Auth guard — unauthenticated users cannot initiate OAuth
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        # Validate the Upstox config (reads env vars, raises BrokerConfigError
        # on missing/empty values) BEFORE issuing a state token so that
        # the "no state row created" AC is satisfied when the env is not set.
        try:
            from upstox_config import UpstoxConfig

            UpstoxConfig.from_env()
        except BrokerConfigError as exc:
            return jsonify({
                "error": "broker_not_configured",
                "message": str(exc),
            }), 503

        # Issue a single-use CSRF state token for this user + broker
        state = issue_state(user_id, "upstox")

        # Build the authorization URL via the registered Upstox connector.
        # BrokerConfigError is already handled above, but we keep this in a
        # try/except for robustness in case any other caller-level error
        # surfaces.
        try:
            connector = get_broker_connector("upstox")
            authorize_url = connector.build_authorize_url(state=state)
        except BrokerConfigError as exc:
            return jsonify({
                "error": "broker_not_configured",
                "message": str(exc),
            }), 503

        return jsonify({"authorize_url": authorize_url, "state": state}), 200

    # -------------------------------------------------------------------------
    # STORY-17: Upstox OAuth callback — code exchange + persist + redirect
    # -------------------------------------------------------------------------

    @app.get("/api/brokers/upstox/callback")
    def api_brokers_upstox_callback():
        """GET /api/brokers/upstox/callback — Upstox OAuth redirect URI.

        No session cookie is required; identity comes solely from the
        single-use ``state`` parameter. ``user_id`` in the query string
        is IGNORED (never trusted). On success the user is redirected
        (302) to the frontend's settings page with a success marker. On
        failure a failure marker and a machine-readable ``reason`` slug are
        included in the redirect query string.

        All error paths log the failure reason server-side with the state
        value redacted so sensitive data never appears in logs.
        """
        import logging as _logging
        _logger = _logging.getLogger(__name__)

        _FRONTEND_SUCCESS = "/settings/brokers?connect=success&broker=upstox"
        _FRONTEND_ERROR_BASE = "/settings/brokers?connect=error&broker=upstox"

        def _error_redirect(slug: str) -> tuple:
            _logger.warning(
                "[UPSTOX_CALLBACK_ERROR] redirect reason=%s",
                slug,
                extra={"callback_reason": slug},
            )
            return redirect(f"{_FRONTEND_ERROR_BASE}&reason={slug}", code=302)

        # ── 1. Parse query params ─────────────────────────────────────────────
        state = request.args.get("state", "")
        code = request.args.get("code")

        # Upstox may include an `error` param instead of `code` when the
        # user denies the request or Upstox returns an OAuth error.
        upstox_error = request.args.get("error")

        # Ignore any user_id in the query string — identity comes from state.
        _ = request.args.get("user_id")

        # ── 2. Consume single-use state ──────────────────────────────────────
        if not state:
            return _error_redirect("invalid_state")

        try:
            user_id, broker_id = consume_state(state)
        except InvalidOAuthStateError:
            return _error_redirect("invalid_state")
        except OAuthStateExpiredError:
            return _error_redirect("state_expired")
        except OAuthStateReplayError:
            return _error_redirect("state_replayed")

        # ── 3. Validate code presence / detect Upstox denial ─────────────────
        if upstox_error:
            # Upstox explicitly returned an error (e.g. user denied access).
            return _error_redirect("access_denied")

        if not code:
            return _error_redirect("missing_code")

        # ── 4. Exchange code for credentials via connector ─────────────────────
        try:
            portfolio = DefaultUserPortfolio()
            portfolio.connect_portfolio(
                user_id=user_id,
                broker_id=broker_id,
                payload={"code": code},
            )
        except BrokerAuthError:
            return _error_redirect("token_exchange_failed")
        except BrokerConfigError:
            return _error_redirect("broker_not_configured")
        except Exception:
            _logger.exception("[UPSTOX_CALLBACK_ERROR] unexpected error during connect")
            return _error_redirect("unexpected")

        # ── 5. Success ────────────────────────────────────────────────────────
        _logger.info(
            "[UPSTOX_CALLBACK_SUCCESS] user_id=%s broker_id=%s",
            user_id,
            broker_id,
        )
        return redirect(_FRONTEND_SUCCESS, code=302)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
