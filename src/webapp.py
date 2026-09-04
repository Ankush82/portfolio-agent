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

from pathlib import Path

from flask import Flask

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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
