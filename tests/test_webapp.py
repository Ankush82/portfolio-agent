"""Smoke tests for the Flask app scaffold (src/webapp.py). Each UI
story (STORY-14 through STORY-18) adds its own routes/templates on top
of this; this file only covers the scaffold itself."""

from __future__ import annotations

from webapp import create_app


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