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
from pathlib import Path

from flask import Flask, jsonify, request, render_template
from yahoo_finance_client import fetch_yahoo_finance_quote, YahooFinanceError

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _REPO_ROOT / "templates"
_STATIC_DIR = _REPO_ROOT / "static"


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
