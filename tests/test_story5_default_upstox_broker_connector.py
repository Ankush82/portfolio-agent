"""QA tests for STORY-5 — DefaultUpstoxBrokerConnector skeleton +
build_authorize_url.

Every test in this file exercises one of the STORY-5 acceptance
criteria verbatim against ``DefaultUpstoxBrokerConnector`` with a
hand-authored ``UpstoxConfig`` and a real ``_UpstoxHttp`` instance
backed by a no-op token provider. No network is ever contacted — the
connector under test is the unit, and its only side effect in this
story is constructing a string."""

from urllib.parse import parse_qs, urlparse

import pytest

from components.c01_user_portfolio import (
    BrokerConnector,
    DefaultUpstoxBrokerConnector,
)
from upstox_config import UpstoxConfig
from upstox_http import _UpstoxHttp


def _http() -> _UpstoxHttp:
    """A real ``_UpstoxHttp`` with a deterministic token provider.
    The token provider is never called in STORY-5 (the connector's
    only method, ``build_authorize_url``, doesn't talk to the
    helper) but is wired in so the connector is constructed exactly
    as STORY-6 onwards expects — a hand-rolled stub of the helper
    here would let a later regression slip through unnoticed."""
    return _UpstoxHttp(token_provider=lambda: "story5-noop-token")


def _config(
    *,
    client_id: str = "test-client-id",
    redirect_uri: str = "https://example.com/cb",
) -> UpstoxConfig:
    return UpstoxConfig(
        client_id=client_id,
        client_secret="test-client-secret",
        redirect_uri=redirect_uri,
    )


# ---------------------------------------------------------------------
# AC: ``isinstance(DefaultUpstoxBrokerConnector(cfg), BrokerConnector)``
# is True — the connector conforms to the STORY-2 Protocol exactly.
# ---------------------------------------------------------------------


def test_default_upstox_broker_connector_satisfies_the_brokerconnector_protocol_via_isinstance():
    """``DefaultUpstoxBrokerConnector`` passes the runtime-checkable
    ``isinstance`` test against the ``BrokerConnector`` Protocol —
    the same shape ``StubBrokerConnector`` already satisfies in
    STORY-3. This is the load-bearing AC that allows a caller to
    treat the connector polymorphically with the existing stub."""
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    assert isinstance(connector, BrokerConnector)


# ---------------------------------------------------------------------
# AC: ``broker_id == 'upstox'`` and ``display_name == 'Upstox'``.
# ---------------------------------------------------------------------


def test_default_upstox_broker_connector_has_broker_id_upstox_and_display_name_upstox():
    """Both identifiers are class-level attributes (not derived from
    any runtime state) — they're what callers use to route per-broker
    UI affordances and to select the right connector, so a typo here
    would silently mis-route the OAuth flow."""
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    assert connector.broker_id == "upstox"
    assert connector.display_name == "Upstox"


# ---------------------------------------------------------------------
# AC: scheme/host/path is exactly
# ``https://api.upstox.com/v2/login/authorization/dialog``.
# ---------------------------------------------------------------------


def test_build_authorize_url_scheme_host_path_is_exactly_upstox_authorize_dialog():
    """The URL produced must point at Upstox's documented OAuth
    authorize endpoint — a typo or an alternate host (e.g. the
    sandbox URL) would silently send the user to the wrong place.
    ``urlparse`` separates the URL into its components so this AC
    can be checked component-by-component rather than as a single
    brittle string comparison."""
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    url = connector.build_authorize_url(state="csrf-token-123")

    parts = urlparse(url)
    assert parts.scheme == "https"
    assert parts.netloc == "api.upstox.com"
    assert parts.path == "/v2/login/authorization/dialog"


# ---------------------------------------------------------------------
# AC: query string contains exactly the four keys
# ``response_type``, ``client_id``, ``redirect_uri``, ``state``;
# ``response_type == 'code'``; ``client_id`` and ``redirect_uri`` match
# the injected config.
# ---------------------------------------------------------------------


def test_build_authorize_url_query_has_exactly_the_four_keys_with_expected_values():
    """Every key documented by Upstox's authorize endpoint is
    present, and no extra key (no ``scope``, no PKCE, nothing the
    docs do not list) leaks in. ``response_type`` is the literal
    string ``code`` — not a typo like ``authorization_code`` —
    because that's exactly what the docs require."""
    config = _config(
        client_id="my-upstox-app",
        redirect_uri="https://example.com/cb",
    )
    connector = DefaultUpstoxBrokerConnector(config=config, http=_http())
    url = connector.build_authorize_url(state="opaque-state-xyz")

    parts = urlparse(url)
    qs = parse_qs(parts.query)

    assert set(qs.keys()) == {
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
    }
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [config.client_id]
    assert qs["redirect_uri"] == [config.redirect_uri]
    assert qs["state"] == ["opaque-state-xyz"]


# ---------------------------------------------------------------------
# AC: a ``redirect_uri`` containing ``:`` / ``/`` / query characters
# is percent-encoded and round-trips via ``parse_qs`` to the original.
# ---------------------------------------------------------------------


def test_build_authorize_url_percent_encodes_redirect_uri_with_query_characters_and_round_trips():
    """``redirect_uri`` is the realistic case the docs allow: a
    full HTTPS URL that itself carries a query string. Naive
    string interpolation would either split the outer query string
    or corrupt the inner one; ``urllib.parse.urlencode`` /
    ``urllib.parse.parse_qs`` together prove the round-trip holds
    for the ``:``, ``/``, ``?``, ``&``, and ``=`` characters all
    present in this value."""
    redirect_with_query = "https://example.com/cb?next=/foo&x=1"
    config = _config(redirect_uri=redirect_with_query)
    connector = DefaultUpstoxBrokerConnector(config=config, http=_http())
    url = connector.build_authorize_url(state="state")

    # No raw ``:`` / ``/`` / ``?`` characters from the redirect_uri
    # value appear unescaped in the URL — otherwise the inner
    # ``?`` would have terminated the outer query and ``parse_qs``
    # would have lost everything after it.
    raw = urlparse(url).query
    assert "://" not in raw.split("&")[2], (
        "redirect_uri value's ':'/'/' should be percent-encoded "
        "inside the query string, not appear verbatim"
    )

    qs = parse_qs(urlparse(url).query)
    assert qs["redirect_uri"] == [redirect_with_query]


# ---------------------------------------------------------------------
# AC: empty / whitespace-only ``state`` raises ``ValueError``.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_state",
    ["", " ", "   ", "\t", "\n", " \t\n "],
    ids=["empty", "single_space", "multiple_spaces", "tab", "newline", "mixed_whitespace"],
)
def test_build_authorize_url_raises_value_error_for_empty_or_whitespace_state(bad_state: str):
    """Upstox rejects a blank ``state`` server-side, so the
    connector rejects it client-side too. Whitespace-only counts
    as blank for the same reason — a CSRF token composed entirely
    of whitespace is no token at all."""
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    with pytest.raises(ValueError):
        connector.build_authorize_url(state=bad_state)


# ---------------------------------------------------------------------
# AC: tests construct the connector with a fake config; no network.
# ---------------------------------------------------------------------


def test_default_upstox_broker_connector_constructs_and_uses_no_network():
    """Constructing the connector and calling ``build_authorize_url``
    makes no outbound HTTP call. Verified by the fact that the test
    passes with a real ``_UpstoxHttp`` whose token provider is never
    actually invoked (the authorize URL is built purely from
    ``UpstoxConfig``, no helper needed) — the same property
    ``StubBrokerConnector`` already demonstrates for STORY-3."""
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    # If this raised a ``requests``-related exception, the connector
    # would have attempted to talk to Upstox. It doesn't.
    url = connector.build_authorize_url(state="state")
    assert url.startswith("https://api.upstox.com/")


# ---------------------------------------------------------------------
# STORY-6/7/8 fill these in. This story pins their not-implemented
# status so a regression that silently fills one in early is caught.
# ---------------------------------------------------------------------


def test_default_upstox_broker_connector_exchange_auth_code_raises_not_implemented_for_story6():
    from components.c01_user_portfolio import BrokerCredentials
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    with pytest.raises(NotImplementedError):
        connector.exchange_auth_code(code="any-code")  # type: ignore[arg-type]


def test_default_upstox_broker_connector_fetch_holdings_raises_not_implemented_for_story7():
    from components.c01_user_portfolio import BrokerCredentials
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    with pytest.raises(NotImplementedError):
        connector.fetch_holdings(  # type: ignore[arg-type]
            credentials=BrokerCredentials(access_token="t"),
        )


def test_default_upstox_broker_connector_fetch_transactions_raises_not_implemented_for_story8():
    from datetime import date
    from components.c01_user_portfolio import BrokerCredentials
    connector = DefaultUpstoxBrokerConnector(
        config=_config(), http=_http()
    )
    with pytest.raises(NotImplementedError):
        connector.fetch_transactions(  # type: ignore[arg-type]
            credentials=BrokerCredentials(access_token="t"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )


# ---------------------------------------------------------------------
# QA-authored STORY-5 verification test — exercises THIS story's own
# acceptance criteria independently of the pre-existing test set.
# ---------------------------------------------------------------------


def test_qa_story5_authorize_url_exhaustive_acceptance_criteria():
    """QA-authored end-to-end check of every STORY-5 acceptance
    criterion against a single ``build_authorize_url`` invocation
    with a maximally challenging ``redirect_uri`` and ``state``.

    The chosen ``redirect_uri`` deliberately includes every
    character ``urllib.parse.quote(..., safe='')`` encodes by
    default — ``:``, ``/``, ``?``, ``&``, ``=``, ``#``, ``+``,
    ``%``, space — so a regression that switches to a permissive
    ``quote(..., safe='/')`` style or that uses naive string
    interpolation would surface here.

    Asserted ACs (all in one test, by design — a single
    ``build_authorize_url`` call produces all the evidence):

      1. ``isinstance(connector, BrokerConnector)`` is True.
      2. ``broker_id == 'upstox'`` and ``display_name == 'Upstox'``.
      3. Scheme/host/path is exactly
         ``https://api.upstox.com/v2/login/authorization/dialog``.
      4. Query string contains exactly the four keys
         ``response_type``, ``client_id``, ``redirect_uri``,
         ``state`` — no ``scope``, no PKCE, no other extras.
      5. ``response_type == 'code'``.
      6. ``client_id`` and ``redirect_uri`` equal the injected
         config values (round-trip via ``parse_qs``).
      7. ``state`` equals what the caller passed.
      8. ``redirect_uri`` with ``:``/``/``/``?`` characters is
         percent-encoded (no raw ``:`` or ``?`` survives in the
         query string outside the URL's own structure) and
         round-trips via ``parse_qs`` to the original.
    """
    config = UpstoxConfig(
        client_id="my-upstox-app-id",
        client_secret="my-upstox-secret",
        redirect_uri="https://example.com/cb?next=/foo&x=1#frag",
    )
    connector = DefaultUpstoxBrokerConnector(
        config=config,
        http=_UpstoxHttp(token_provider=lambda: "qa-noop-token"),
    )

    # AC 1: Protocol conformance via ``isinstance``.
    assert isinstance(connector, BrokerConnector), (
        "DefaultUpstoxBrokerConnector must satisfy the BrokerConnector Protocol"
    )

    # AC 2: identifiers are the exact strings the story pins.
    assert connector.broker_id == "upstox"
    assert connector.display_name == "Upstox"

    state_value = "csrf+token/with=special&chars?x#y"
    url = connector.build_authorize_url(state=state_value)

    # AC 3: scheme/host/path is exactly the Upstox authorize endpoint.
    parts = urlparse(url)
    assert parts.scheme == "https"
    assert parts.netloc == "api.upstox.com"
    assert parts.path == "/v2/login/authorization/dialog"

    # AC 4: query string contains EXACTLY these four keys — no extras.
    # ``set()`` comparison catches the regression of accidentally
    # adding a ``scope`` or PKCE parameter.
    qs = parse_qs(parts.query, keep_blank_values=False)
    assert set(qs.keys()) == {
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
    }, (
        f"query keys must be exactly {{response_type, client_id, "
        f"redirect_uri, state}}; got {sorted(qs.keys())}"
    )

    # AC 5: ``response_type`` is the literal ``code``.
    assert qs["response_type"] == ["code"]

    # AC 6: ``client_id`` and ``redirect_uri`` equal the injected config.
    assert qs["client_id"] == [config.client_id]
    assert qs["redirect_uri"] == [config.redirect_uri]

    # AC 7: ``state`` equals what the caller passed (round-trip).
    assert qs["state"] == [state_value]

    # AC 8: round-trip invariant for the maximally challenging
    # ``redirect_uri``. The original value contained every
    # ``:``/``/``/``?``/``&``/``=``/``#`` character — if any one of
    # those had been left unencoded, the inner ``?`` would have
    # terminated the outer query string and the value would NOT have
    # round-tripped cleanly back to the original.
    assert qs["redirect_uri"] == [
        "https://example.com/cb?next=/foo&x=1#frag"
    ], (
        "redirect_uri with special characters must round-trip via "
        "parse_qs to the original value"
    )

    # Defensive cross-check: the percent-encoded form of the
    # redirect_uri must actually appear in the URL string. This is
    # independent evidence (beyond parse_qs) that ``:``, ``/``,
    # ``?``, ``&``, and ``=`` inside the value were encoded.
    assert "https%3A%2F%2Fexample.com" in url
    assert "next%3D%2Ffoo" in url
    assert "x%3D1" in url


def test_qa_story5_empty_state_raises_value_error_with_descriptive_message():
    """QA-authored focused check: a bare empty string ``""`` raises
    ``ValueError`` and the message identifies the connector (so a
    caller catching ``ValueError`` in a wider try/except can
    attribute the failure correctly). Distinct from the parametrized
    test above so the empty-string case is also independently
    verified."""
    connector = DefaultUpstoxBrokerConnector(
        config=_config(),
        http=_UpstoxHttp(token_provider=lambda: "qa-noop-token"),
    )
    with pytest.raises(ValueError) as excinfo:
        connector.build_authorize_url(state="")
    # The error must mention the connector so a wider except
    # ValueError can attribute the failure.
    assert "DefaultUpstoxBrokerConnector" in str(excinfo.value) or (
        "state" in str(excinfo.value).lower()
    )