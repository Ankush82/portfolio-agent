"""Smoke test for DefaultUpstoxBrokerConnector skeleton (STORY-5)."""

from urllib.parse import urlparse, parse_qs

from components.c01_user_portfolio import (
    BrokerConnector,
    DefaultUpstoxBrokerConnector,
)
from upstox_config import UpstoxConfig
from upstox_http import _UpstoxHttp


def _http():
    return _UpstoxHttp(token_provider=lambda: "t")


def test_isinstance_broker_connector():
    cfg = UpstoxConfig(client_id="cid", client_secret="sec",
                        redirect_uri="https://example.com/cb")
    conn = DefaultUpstoxBrokerConnector(config=cfg, http=_http())
    assert isinstance(conn, BrokerConnector)


def test_identifiers():
    cfg = UpstoxConfig(client_id="cid", client_secret="sec",
                        redirect_uri="https://example.com/cb")
    conn = DefaultUpstoxBrokerConnector(config=cfg, http=_http())
    assert conn.broker_id == "upstox"
    assert conn.display_name == "Upstox"


def test_url_shape_and_keys():
    cfg = UpstoxConfig(client_id="cid", client_secret="sec",
                        redirect_uri="https://example.com/cb")
    conn = DefaultUpstoxBrokerConnector(config=cfg, http=_http())
    url = conn.build_authorize_url(state="abc-123")
    parts = urlparse(url)
    assert parts.scheme == "https"
    assert parts.netloc == "api.upstox.com"
    assert parts.path == "/v2/login/authorization/dialog"
    qs = parse_qs(urlparse(url).query)
    assert set(qs.keys()) == {"response_type", "client_id", "redirect_uri", "state"}
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid"]
    assert qs["redirect_uri"] == ["https://example.com/cb"]
    assert qs["state"] == ["abc-123"]


def test_redirect_uri_with_special_chars_roundtrips():
    redirect = "https://example.com/cb?next=/foo&x=1"
    cfg = UpstoxConfig(client_id="cid", client_secret="sec",
                        redirect_uri=redirect)
    conn = DefaultUpstoxBrokerConnector(config=cfg, http=_http())
    url = conn.build_authorize_url(state="s")
    qs = parse_qs(urlparse(url).query)
    assert qs["redirect_uri"] == [redirect]


def test_empty_state_raises():
    cfg = UpstoxConfig(client_id="cid", client_secret="sec",
                        redirect_uri="https://example.com/cb")
    conn = DefaultUpstoxBrokerConnector(config=cfg, http=_http())
    for s in ["", "   ", "\t\n"]:
        try:
            conn.build_authorize_url(state=s)
        except ValueError:
            pass
        else:
            raise AssertionError(f"empty/whitespace state {s!r} should raise")