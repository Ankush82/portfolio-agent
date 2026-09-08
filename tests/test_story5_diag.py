"""Diagnostic: print the exact URL the connector produces and assert
each AC verbatim."""
from urllib.parse import urlparse, parse_qs

from components.c01_user_portfolio import DefaultUpstoxBrokerConnector
from upstox_config import UpstoxConfig
from upstox_http import _UpstoxHttp


cfg = UpstoxConfig(
    client_id="abc123",
    client_secret="secret",
    redirect_uri="https://example.com/cb?next=/foo&x=1",
)
http = _UpstoxHttp(token_provider=lambda: "t")
conn = DefaultUpstoxBrokerConnector(config=cfg, http=http)

url = conn.build_authorize_url(state="csrf-token-123")
print("FULL URL:", url)
print()

parts = urlparse(url)
print("scheme:", parts.scheme)
print("netloc:", parts.netloc)
print("path:", parts.path)
print("query:", parts.query)
print()

qs = parse_qs(parts.query)
print("query keys:", sorted(qs.keys()))
print("client_id:", qs.get("client_id"))
print("redirect_uri:", qs.get("redirect_uri"))
print("state:", qs.get("state"))
print("response_type:", qs.get("response_type"))
print()

# Per AC: round-trip redirect_uri via parse_qs back to original
print("redirect_uri roundtrip OK:",
      qs.get("redirect_uri") == [cfg.redirect_uri])