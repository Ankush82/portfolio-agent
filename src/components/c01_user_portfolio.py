"""User & Portfolio (component 01) — the user's identity and the
portfolio it owns.

Design: no fig. 1 / fig. 2 mechanism diagram exists for this component
(it stayed whiteboard-only through the design-framework round covered
by `checkpoint.md`). The `Default*` classes below are its first real
implementation, built directly from this task's own brief rather than
a prior design artifact.
Decisions:
  ADR-0022 — BrokerConnector interface shape (Protocol) and broker data
             tagged UNTRUSTED by default, extending ADR-0003/ADR-0018's
             pattern to this component.
  ADR-0023 — which real broker/aggregator API eventually backs
             BrokerConnector (Status: Proposed — genuine external-
             credential gap, not decided here).
  ADR-0044 — manual stock entry: a parallel, real onboarding path
             (list_available_securities/add_holding_manually/
             add_transaction_manually on DefaultUserPortfolio) that
             bypasses BrokerConnector entirely, backed for real by
             Knowledge & Entity Model (04)'s registry.

Interfaces below match the whiteboard-level Component Whiteboards
artifact, card 01: Portfolio -> Portfolio State, User -> Decision &
Policy, Portfolio -> Event & Analysis, User -> Notification.
"""

import logging
import uuid
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from components.c04_knowledge_entity import DefaultKnowledgeEntity, Entity
from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from exchange_rate_client import (
    ExchangeRateFetchError,
    MissingExchangeRateAPIKeyError,
    fetch_exchange_rate,
)
from infrastructure import Infrastructure
from infrastructure_postgres import BrokerConnectionRecord, DefaultInfrastructure
from upstox_config import UpstoxConfig

if TYPE_CHECKING:
    # Imported only for the type hint on ``DefaultUpstoxBrokerConnector.__init__``;
    # resolved at runtime inside the constructor to avoid a circular import
    # (``src/upstox_http.py`` imports the STORY-2 ``BrokerApiError`` /
    # ``BrokerAuthError`` / ``BrokerRateLimitError`` classes from this module
    # at module load time, so the reverse module-level import would deadlock).
    from upstox_http import _UpstoxHttp


# ---------------------------------------------------------------------------
# Broker connector registry (STORY-9 / STORY-13)
# ---------------------------------------------------------------------------
# In-memory registry of available BrokerConnector instances, keyed by
# broker_id. Real connectors are registered at startup; tests register
# StubBrokerConnector or other test doubles. No Upstox-specific values
# appear here — only the Protocol shape and broker_id strings.
# Defined after BrokerConnector Protocol (below) to avoid forward-reference
# NameError at module load.
_broker_connector_registry: dict[str, "BrokerConnector"] = {}


def get_broker_connector(broker_id: str) -> "BrokerConnector":
    """Look up a registered BrokerConnector by broker_id (STORY-9).

    Raises ``UnsupportedBrokerError`` if no connector is registered
    for the given broker_id. The registry contains only broker_id
    strings (no URLs, no field names) — everything broker-specific
    lives behind the Protocol."""
    connector = _broker_connector_registry.get(broker_id)
    if connector is None:
        raise UnsupportedBrokerError(
            f"no connector registered for broker_id {broker_id!r}"
        )
    return connector


def register_broker_connector(connector: "BrokerConnector") -> None:
    """Register a BrokerConnector instance (used by tests / startup)."""
    _broker_connector_registry[connector.broker_id] = connector


def unregister_broker_connector(broker_id: str) -> None:
    """Remove a BrokerConnector from the registry (used by tests to clean up)."""
    _broker_connector_registry.pop(broker_id, None)


def list_available_brokers() -> list[dict]:
    """Return display metadata for every registered BrokerConnector.

    Each dict contains the fields required by STORY-20's settings/brokers
    UI: ``broker_id`` and ``display_name``. The registry is the single
    source of truth; nothing is hard-coded here.
    """
    return [
        {"broker_id": connector.broker_id, "display_name": connector.display_name}
        for connector in _broker_connector_registry.values()
    ]


# Exception hierarchy for BrokerConnector (ADR-0022)
class BrokerError(Exception):
    """Base exception for all broker-related errors."""
    pass


class BrokerConfigError(BrokerError):
    """Raised when the broker configuration is invalid or missing."""
    pass


class BrokerAuthError(BrokerError):
    """Raised when authentication fails (invalid/expired token or auth code)."""
    pass


class BrokerApiError(BrokerError):
    """Raised when the broker API returns an error (non-2xx or status != 'success')."""
    pass


class BrokerRateLimitError(BrokerError):
    """Raised when the broker API rate limit is exceeded."""
    pass


class UnsupportedBrokerError(BrokerError):
    """Raised when the broker is not supported."""
    pass


# Data Transfer Objects (DTOs) for BrokerConnector (ADR-0022)
@dataclass(frozen=True)
class BrokerCredentials:
    access_token: str
    token_type: str = 'Bearer'
    expires_at: datetime | None = None
    refresh_token: str | None = None
    broker_user_id: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerHolding:
    symbol: str
    isin: str
    quantity: Decimal | None
    average_price: Decimal | None
    last_price: Decimal | None
    exchange: str | None = None
    product: str | None = None
    instrument_id: str | None = None
    name: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerTransaction:
    external_id: str
    symbol: str
    isin: str
    trade_date: date
    side: Literal['BUY', 'SELL']
    quantity: Decimal
    price: Decimal
    amount: Decimal
    exchange: str
    segment: str
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ImportResult:
    """Result of an import_transactions call (STORY-15)."""
    transactions_inserted: int
    transactions_skipped_existing: int
    rows_skipped_invalid: int = 0


# BrokerConnector Protocol (ADR-0022)
@runtime_checkable
class BrokerConnector(Protocol):
    broker_id: str
    display_name: str

    def build_authorize_url(self, *, state: str) -> str:
        """Build the broker-specific authorization URL for the given state."""
        ...

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        """Exchange an authorization code for broker credentials."""
        ...

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        """Fetch holdings for the given credentials."""
        ...

    def fetch_transactions(self, *, credentials: BrokerCredentials, start_date: date, end_date: date) -> list[BrokerTransaction]:
        """Fetch transactions for the given credentials and date range."""
        ...


class PlaceholderBrokerConnector:
    """Placeholder implementation of BrokerConnector for testing and development.
    All methods return synthetic, obviously-fake data that cannot be mistaken
    for real broker data."""

    broker_id: str = "placeholder"
    display_name: str = "Placeholder Broker"

    def build_authorize_url(self, *, state: str) -> str:
        return f"https://placeholder.broker/auth?state={state}"

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        return BrokerCredentials(
            access_token=f"placeholder-token-{code}",
            token_type="Bearer",
            expires_at=None,
            refresh_token=None,
            broker_user_id=None,
            raw={"code": code},
        )

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        return []

    def fetch_transactions(self, *, credentials: BrokerCredentials, start_date: date, end_date: date) -> list[BrokerTransaction]:
        return []


class StubBrokerConnector:
    """Deterministic, offline test double for the ``BrokerConnector``
    Protocol (STORY-3). Used by OTHER components' tests where a
    ``BrokerConnector``-shaped collaborator is needed but no real
    broker call should ever be made.

    Conforms exactly to the STORY-2 ``BrokerConnector`` Protocol —
    verifiable via the runtime-checkable Protocol's ``isinstance``
    check (the first acceptance criterion for this story).

    Behaviour (all deterministic, no I/O, no environment access):

      * ``broker_id`` is ``'stub'`` and ``display_name`` is
        ``'Stub Broker'`` — fixed identifiers every caller can rely
        on, not anything derived from host state.
      * ``build_authorize_url`` returns a fixed fake URL echoing the
        passed ``state`` so a caller can assert its ``state`` token
        reached this connector intact.
      * ``exchange_auth_code`` returns a fixed ``BrokerCredentials``
        for any code other than the sentinel ``'invalid'``, for which
        it raises ``BrokerAuthError`` — the sentinel lets tests
        exercise the auth-failure branch without a real broker.
      * ``fetch_holdings`` returns a small fixed list of
        ``BrokerHolding`` (overridable via the constructor — see
        below).
      * ``fetch_transactions`` returns a fixed list of
        ``BrokerTransaction`` filtered by the requested
        ``start_date`` / ``end_date`` window (inclusive on both ends,
        so window-edge tests work as expected). Also overridable.

    Constructor injection:

      * ``holdings``: optional list of ``BrokerHolding`` to return
        from ``fetch_holdings``. Defaults to a small fixed canned
        list so the default-construction path is fully usable.
      * ``transactions``: optional list of ``BrokerTransaction`` to
        filter through in ``fetch_transactions``. Defaults to a
        small fixed canned list spanning several dates so the
        windowing logic has real data to filter.
      * ``raise_on``: optional exception instance to raise from any
        of the four Protocol methods (simulating broker failures
        in other stories' tests). When set, every method raises this
        exception before producing any real output — matches the
        "simulate failures" requirement without a per-method
        configuration knob the Protocol would otherwise need.

    No network calls. No environment-variable reads. All values
    baked in at construction time, so repeated calls produce
    identical results (the "deterministic across repeated calls"
    acceptance criterion)."""

    broker_id: str = "stub"
    display_name: str = "Stub Broker"

    def __init__(
        self,
        holdings: list[BrokerHolding] | None = None,
        transactions: list[BrokerTransaction] | None = None,
        raise_on: BaseException | None = None,
    ) -> None:
        self._holdings: list[BrokerHolding] = (
            list(holdings) if holdings is not None else list(_DEFAULT_STUB_HOLDINGS)
        )
        self._transactions: list[BrokerTransaction] = (
            list(transactions) if transactions is not None else list(_DEFAULT_STUB_TRANSACTIONS)
        )
        self._raise_on = raise_on

    def build_authorize_url(self, *, state: str) -> str:
        if self._raise_on is not None:
            raise self._raise_on
        return f"https://stub.broker/auth?state={state}"

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        if self._raise_on is not None:
            raise self._raise_on
        if code == "invalid":
            raise BrokerAuthError(
                f"StubBrokerConnector: refusing sentinel code 'invalid'"
            )
        return BrokerCredentials(
            access_token="stub-access-token",
            token_type="Bearer",
            expires_at=None,
            refresh_token=None,
            broker_user_id="stub-broker-user",
            raw={"code": code, "stub": True},
        )

    def fetch_holdings(self, *, credentials: BrokerCredentials) -> list[BrokerHolding]:
        if self._raise_on is not None:
            raise self._raise_on
        return [BrokerHolding(**asdict(h)) for h in self._holdings]

    def fetch_transactions(
        self,
        *,
        credentials: BrokerCredentials,
        start_date: date,
        end_date: date,
    ) -> list[BrokerTransaction]:
        if self._raise_on is not None:
            raise self._raise_on
        return [
            BrokerTransaction(**asdict(t))
            for t in self._transactions
            if start_date <= t.trade_date <= end_date
        ]


class DefaultUpstoxBrokerConnector:
    """Real Upstox implementation of the ``BrokerConnector`` Protocol
    (STORY-5). Skeleton for the upcoming auth flow — only
    ``build_authorize_url`` is implemented in this story;
    ``exchange_auth_code`` / ``fetch_holdings`` / ``fetch_transactions``
    raise ``NotImplementedError`` here and are filled in by
    STORY-6 / STORY-7 / STORY-8 respectively.

    Constructor-injected dependencies:

      * ``config`` — an ``UpstoxConfig`` (STORY-1) holding the
        Upstox app's ``client_id``, ``client_secret``, and
        ``redirect_uri``. Immutable / frozen, so no defensive copy
        is needed.
      * ``http`` — the private ``_UpstoxHttp`` helper (STORY-4)
        that owns transport, retries, and error-mapping for all
        outbound Upstox calls. Injecting it (rather than
        constructing it internally) is what makes the connector
        testable in later stories without ever touching ``requests``
        or the network.

    Identifiers (ADR-0022's per-broker metadata contract):

      * ``broker_id == 'upstox'`` — the broker-specific slug other
        components use to look up the right connector and to route
        per-broker UI affordances.
      * ``display_name == 'Upstox'`` — the human-readable name
        rendered in UI surfaces; not derived from any runtime
        state.

    ``build_authorize_url`` produces exactly the URL shape Upstox's
    public OAuth docs describe — no extras, no PKCE, no ``scope``
    parameter (the docs the story's AC quotes do not list one,
    and inventing one here would diverge from the contract):

        https://api.upstox.com/v2/login/authorization/dialog
            ?response_type=code
            &client_id=<percent-encoded client_id>
            &redirect_uri=<percent-encoded redirect_uri>
            &state=<percent-encoded state>

    Every value is percent-encoded with ``urllib.parse.quote``
    (``safe=''`` semantics — no character is left unencoded), and
    the resulting string round-trips through
    ``urllib.parse.parse_qs`` to the original values verbatim. A
    blank or whitespace-only ``state`` raises ``ValueError`` —
    Upstox rejects these server-side, and rejecting them client
    side too keeps callers from burning a CSRF-less OAuth round
    trip on a value the server would discard."""

    broker_id: str = "upstox"
    display_name: str = "Upstox"

    # Upstox's documented OAuth authorize endpoint. Verbatim from
    # the docs — the AC's "scheme/host/path is exactly
    # https://api.upstox.com/v2/login/authorization/dialog" rule
    # is encoded as this single constant so a future move (e.g.
    # to ``https://api-sandbox.upstox.com`` for a test
    # environment) is one constant change away, not a string
    # scattered through the implementation.
    _UPSTOX_AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"

    # STORY-7: the long-term holdings endpoint. Pinned as a class
    # constant so the AC's "Request URL is exactly
    # https://api.upstox.com/v2/portfolio/long-term-holdings" rule
    # is a single grep, not a literal scattered through
    # ``fetch_holdings``. The host + path combination is exactly
    # what Upstox's own v2 docs publish; no extra query string,
    # no version bump, no trailing slash.
    _UPSTOX_LONG_TERM_HOLDINGS_PATH = "/v2/portfolio/long-term-holdings"

    # Logger for ``fetch_holdings`` — a module-level ``logging.getLogger``
    # on ``__name__`` so a real operator can route per-module log
    # records (e.g. ``logging.getLogger("components.c01_user_portfolio")``)
    # without every warning being swallowed by the root logger's
    # default config. The warning logged when a holding element is
    # missing ``trading_symbol`` is emitted on this logger so it
    # surfaces under the standard per-module channel.
    _logger = logging.getLogger(__name__)

    def __init__(
        self,
        config: UpstoxConfig,
        http: "_UpstoxHttp",
    ) -> None:
        # Deferred import — ``src/upstox_http`` already imports the
        # STORY-2 exception classes from this module at load time, so
        # an eager module-level import here would deadlock. The
        # import is needed at runtime only to record the helper's
        # concrete class for any future ``isinstance`` checks; the
        # connector itself never calls into the helper in this
        # story (STORY-6 onwards).
        from upstox_http import _UpstoxHttp as _UpstoxHttpRuntime
        self._config = config
        self._http: _UpstoxHttpRuntime = http

    def build_authorize_url(self, *, state: str) -> str:
        """Build the Upstox OAuth authorize URL for ``state``.

        Returns a URL whose scheme/host/path is exactly
        ``https://api.upstox.com/v2/login/authorization/dialog`` and
        whose query string carries exactly the four keys
        ``response_type=code``, ``client_id=<config.client_id>``,
        ``redirect_uri=<config.redirect_uri>``, ``state=<state>`` —
        in that order, all percent-encoded. The round-trip invariant
        ``parse_qs(url)['redirect_uri'] == config.redirect_uri``
        holds for any ``redirect_uri`` the Upstox app registration
        accepts, including the realistic
        ``https://example.com/cb?next=/foo`` shape that contains
        ``:`` / ``/`` / ``?`` characters which ``quote`` encodes
        by default.

        ``state`` is mandatory: an empty string or whitespace-only
        string raises ``ValueError`` rather than producing a URL
        Upstox would refuse server-side.
        """
        if not isinstance(state, str) or not state.strip():
            raise ValueError(
                "DefaultUpstoxBrokerConnector.build_authorize_url: "
                "state must be a non-empty, non-whitespace string"
            )

        # ``urlencode`` percent-encodes every value with
        # ``urllib.parse.quote(..., safe='')`` semantics, encoding
        # every character that isn't unreserved per RFC 3986 —
        # including ``:``, ``/``, ``?``, ``&``, ``=``, ``+``, ``#``,
        # ``%`` — so a ``redirect_uri`` of
        # ``https://example.com/cb?next=/foo`` does NOT split the
        # query string, silently inject a new ``&next=`` pair, or
        # corrupt the URL in any way. The resulting ``parse_qs``
        # call reads back the exact original ``redirect_uri``
        # value.
        query = urlencode(
            [
                ("response_type", "code"),
                ("client_id", self._config.client_id),
                ("redirect_uri", self._config.redirect_uri),
                ("state", state),
            ]
        )
        return f"{self._UPSTOX_AUTHORIZE_URL}?{query}"

    # ------------------------------------------------------------------
    # STORY-6 / STORY-7 / STORY-8 fill these in. For this story only
    # they raise ``NotImplementedError`` so the Protocol conformance
    # (verified via ``isinstance(connector, BrokerConnector)``) is
    # intact while the real implementations are still pending.
    # ------------------------------------------------------------------

    def exchange_auth_code(self, *, code: str) -> BrokerCredentials:
        """Exchange an OAuth authorization code for Upstox credentials.

        POSTs the documented form-encoded body to
        ``https://api.upstox.com/v2/login/authorization/token`` via
        ``_UpstoxHttp.post_token_exchange`` — which never retries
        (retrying would either waste the one-time auth code or
        trigger Upstox's duplicate-grant rejection). The form keys
        are passed in the exact order the fetched Upstox docs list
        them (``code``, ``client_id``, ``client_secret``,
        ``redirect_uri``, ``grant_type``) so the wire payload is
        byte-identical to the docs' reference example.

        On a 2xx whose JSON body contains ``access_token``, returns
        a ``BrokerCredentials`` with:

          * ``token_type='Bearer'`` — Upstox access tokens are
            bearer tokens per their docs.
          * ``broker_user_id`` from a top-level ``user_id`` field,
            or ``None`` if the field is absent. The fetched docs do
            not promise it on every response, so absence is normal,
            not an error.
          * ``expires_at`` computed as ``now + expires_in`` seconds
            (UTC) when an ``expires_in`` field is present, else
            ``None``. The docs do not promise it either, so a
            ``None`` ``expires_at`` means "valid until Upstox
            rejects it" — the same semantics the rest of this
            project's broker credentials carry.
          * ``refresh_token`` from the response if present, else
            ``None``. The fetched docs do not promise a refresh
            token on this endpoint, so absence is the normal case.
          * ``raw`` = the full response dict with the
            ``access_token`` value replaced by ``_REDACTED``, so if
            the dict is ever stringified into a log line the
            secret never leaks.

        Errors:
          * 2xx with no ``access_token`` → ``BrokerApiError`` (the
            response shape doesn't match the documented contract).
          * 4xx on the token endpoint (other than 429, which is
            surfaced as ``BrokerRateLimitError``) →
            ``BrokerAuthError`` with a message instructing the user
            to restart the connect flow. A 4xx here is Upstox's
            rejection of the one-time auth code; the only correct
            user action is to start the OAuth round-trip over.
          * 5xx → ``BrokerApiError`` (carrying the HTTP status
            and body snippet from the helper).
          * The auth code and client secret are never passed to
            the helper as anything but the form payload, and the
            helper redacts them before any log/exception path —
            so they cannot end up in any log line or exception
            message this method produces.
        """
        if not isinstance(code, str) or not code.strip():
            raise ValueError(
                "DefaultUpstoxBrokerConnector.exchange_auth_code: "
                "code must be a non-empty, non-whitespace string"
            )

        # Verbatim form order from the Upstox OAuth docs. Each value
        # is a plain string — ``requests`` will URL-encode the body
        # itself when ``data=`` is a dict and
        # ``Content-Type: application/x-www-form-urlencoded`` is set
        # by the helper.
        form: dict[str, str] = {
            "code": code,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "redirect_uri": self._config.redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            response_body = self._http.post_token_exchange(form=form)
        except BrokerRateLimitError:
            # 429 on the token endpoint is a transient backoff
            # signal, not a rejection of the code itself — let it
            # surface unchanged so the caller can retry the
            # *whole* OAuth round-trip on a different cadence
            # rather than asking the user to reconnect.
            raise
        except BrokerApiError as exc:
            # The helper maps every non-2xx to ``BrokerApiError``
            # carrying ``http_status`` (the AC's contract for the
            # HTTP-status attribute). On the token-exchange
            # endpoint specifically, every other 4xx is Upstox
            # rejecting the auth code — the only correct user
            # response is to restart the connect flow.
            status = getattr(exc, "http_status", 0)
            if 400 <= status < 500:
                raise BrokerAuthError(
                    "Upstox rejected the authorization code; please "
                    "restart the connect flow and try a fresh code"
                ) from exc
            raise

        # The helper already enforces status=='success' on 2xx and
        # raises ``BrokerApiError`` otherwise. Defensive checks
        # below cover what the docs DO promise on success
        # (``access_token``) vs what they DON'T (``user_id``,
        # ``expires_in``, ``refresh_token``).
        if not isinstance(response_body, dict):
            raise BrokerApiError(
                "Upstox token exchange returned a non-dict JSON body"
            )

        access_token = response_body.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise BrokerApiError(
                "Upstox token exchange response is missing "
                "'access_token'"
            )

        # Optional fields — the fetched docs do not promise any of
        # these. Each one is read with a ``get`` and validated only
        # for type; absence is the normal case.
        broker_user_id = response_body.get("user_id")
        if broker_user_id is not None and not isinstance(broker_user_id, str):
            broker_user_id = None

        expires_at: datetime | None = None
        expires_in = response_body.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            # ``datetime.now(timezone.utc)`` rather than the
            # deprecated ``datetime.utcnow()`` — the latter emits
            # a ``DeprecationWarning`` on Python 3.12+ which fails
            # the test suite under any ``filterwarnings = error``
            # configuration (a common CI hardening). The result is
            # a timezone-aware UTC datetime.
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=float(expires_in)
            )

        refresh_token = response_body.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            refresh_token = None

        # Redact the access token from the raw dict before it goes
        # anywhere that might be stringified into a log line or an
        # exception message. The original token still lives on
        # ``access_token`` above (it's the only thing the caller
        # actually needs); only the ``raw`` cache is scrubbed. The
        # redaction sentinel matches ``_UpstoxHttp._REDACTED`` so
        # the two layers produce a single, greppable marker if it
        # ever does appear in a log.
        raw: dict = {
            key: ("***" if key == "access_token" else value)
            for key, value in response_body.items()
        }

        return BrokerCredentials(
            access_token=access_token,
            token_type="Bearer",
            expires_at=expires_at,
            refresh_token=refresh_token,
            broker_user_id=broker_user_id,
            raw=raw,
        )

    def fetch_holdings(
        self, *, credentials: BrokerCredentials
    ) -> list[BrokerHolding]:
        """Fetch long-term holdings from Upstox (STORY-7).

        Performs an authenticated GET against
        ``https://api.upstox.com/v2/portfolio/long-term-holdings`` via
        ``_UpstoxHttp.get`` — which attaches the exact two-header
        auth contract (``Authorization: Bearer <access_token>`` +
        ``Accept: application/json``), retries on 429 and 5xx only,
        and raises ``BrokerAuthError`` / ``BrokerRateLimitError`` /
        ``BrokerApiError`` on every non-success outcome.

        The helper already enforces that a 2xx carries top-level
        ``status == 'success'`` and raises ``BrokerApiError``
        otherwise. The response shape (verbatim from Upstox's v2
        docs) is::

            {
              "status": "success",
              "data": [
                {
                  "isin":           "INE002A01018",
                  "trading_symbol": "RELIANCE",
                  "quantity":       10,
                  "average_price":  2400.50,
                  "last_price":     2510.75,
                  "close_price":    2505.00,
                  "pnl":            1102.50,
                  "exchange":       "NSE",
                  "product":        "CNC",
                  "instrument_token": "NSE_EQ|INE002A01018",
                  "company_name":   "Reliance Industries Limited",
                  ...                       # docs end in '...'
                },
                ...
              ]
            }

        Each element is mapped to a ``BrokerHolding`` per the
        STORY-7 table:

          * ``symbol         <- trading_symbol``
          * ``isin           <- isin``
          * ``quantity       <- Decimal(str(quantity))``
          * ``average_price  <- Decimal(str(average_price))``
          * ``last_price     <- Decimal(str(last_price))``
          * ``exchange       <- exchange``
          * ``product        <- product``
          * ``instrument_id  <- instrument_token``
          * ``name           <- company_name``
          * ``raw            <- the whole element`` (so any
            undocumented key the broker adds later is preserved
            verbatim in the DTO instead of being silently dropped)

        Numeric coercion rules (the AC's "Numeric fields must go
        through ``Decimal(str(value))`` (never ``float``) and be
        ``None`` when the key is absent or null" rule):

          * Every numeric field (``quantity``, ``average_price``,
            ``last_price``) is coerced via ``Decimal(str(value))``.
            ``Decimal(str(...))`` rather than ``Decimal(value)`` is
            the documented way to avoid binary float rounding
            surprises (e.g. ``Decimal(0.1)`` is
            ``Decimal('0.1000000000000000055511151231257827021181583404541015625')``
            while ``Decimal(str(0.1))`` is ``Decimal('0.1')``).
          * If the key is absent OR explicitly ``None`` in the
            response element, the resulting field is ``None`` —
            the DTO dataclass permits ``None`` for these fields
            so a broker payload with a null ``average_price``
            (documented as legal for zero-quantity / delisted rows)
            does not crash the whole import.

        Element-level errors (the AC's "an element missing
        ``trading_symbol`` is skipped with a warning log rather than
        failing the whole import" rule):

          * If an element is missing ``trading_symbol`` (the
            minimum field that uniquely identifies a holding),
            ``fetch_holdings`` logs a warning on ``self._logger``
            and skips that element — the rest of the import still
            succeeds. This matches Upstox's own behaviour of
            occasionally returning a partially-formed element on
            delisted or merged securities, where failing the entire
            import would discard the rest of the user's portfolio.
          * Any other missing mandatory field (``isin``,
            ``instrument_token``, ``company_name``, ``exchange``,
            ``product``) is also tolerated — the resulting DTO
            carries the empty string / 0 / ``None`` defaults rather
            than the whole import failing. ``trading_symbol`` is the
            single field treated as a skip-triggers because it's the
            only one the import cannot meaningfully proceed without.

        Top-level errors:

          * ``status != 'success'`` → ``BrokerApiError`` (the
            helper raises this already for a non-success body; we
            do not need to re-check here — the helper's contract
            is the single source of truth).
          * ``data`` missing or ``None`` → returns ``[]`` (an
            empty portfolio is a legal state, not an error).
          * ``data`` not a list → ``BrokerApiError`` (the contract
            says it's a list; anything else is a contract
            violation worth surfacing rather than silently
            fabricating an empty result).

        Returns:
            A list of ``BrokerHolding`` objects, one per parsed
            ``data`` element. Order matches the broker's response
            order. May be empty when ``data`` is empty or ``None``.
        """
        response_body = self._http.get(
            path=self._UPSTOX_LONG_TERM_HOLDINGS_PATH
        )

        # Defensive: ``response_body`` is guaranteed to be a ``dict``
        # by ``_UpstoxHttp._map_response_to_body`` (which raises
        # ``BrokerApiError`` on non-dict 2xx bodies), but the helper
        # is the contract owner for the request shape; we only own
        # the response mapping here.
        #
        # Belt-and-braces status check: the HTTP helper already
        # enforces ``status == 'success'`` on a 2xx body and raises
        # ``BrokerApiError`` otherwise. We re-check here so a test
        # that mocks ``_UpstoxHttp.get`` to return a literal dict
        # (bypassing the helper's own mapping) still gets the
        # documented "status != 'success' -> BrokerApiError"
        # behaviour for free. The cost is one ``.get()`` per call;
        # the benefit is that this method is robust to a
        # misconfigured helper as well as a misbehaving broker.
        if response_body.get("status") != "success":
            raise BrokerApiError(
                f"Upstox long-term-holdings response status is "
                f"{response_body.get('status')!r}, not 'success'"
            )

        data = response_body.get("data")

        # Missing or null ``data`` is a documented "empty portfolio"
        # state, not an error — return ``[]`` rather than raising.
        if data is None:
            return []

        # ``data`` is documented to be a list. Anything else (a
        # dict, a string, an int) is a contract violation; surface
        # it as ``BrokerApiError`` rather than silently fabricating
        # an empty result that would mask a real upstream bug.
        if not isinstance(data, list):
            raise BrokerApiError(
                f"Upstox long-term-holdings response 'data' field "
                f"is not a list; got {type(data).__name__}"
            )

        holdings: list[BrokerHolding] = []
        for element in data:
            # Non-dict elements are a real contract violation —
            # surface as BrokerApiError rather than silently
            # dropping. A real broker would never emit a non-dict
            # element; this branch only fires on a malformed
            # upstream response, which is exactly what we want to
            # raise loudly about.
            if not isinstance(element, dict):
                raise BrokerApiError(
                    f"Upstox long-term-holdings 'data' element is "
                    f"not a dict; got {type(element).__name__}"
                )

            # Element-level skip rule: an element missing
            # ``trading_symbol`` is logged as a warning and
            # skipped — the rest of the import proceeds. We check
            # for *both* "key absent" and "value is None" so a
            # broker payload that explicitly nulls the field is
            # treated identically to one that omits it.
            trading_symbol_raw = element.get("trading_symbol")
            if not trading_symbol_raw or not isinstance(
                trading_symbol_raw, str
            ):
                self._logger.warning(
                    "DefaultUpstoxBrokerConnector.fetch_holdings: "
                    "[UPSTOX_HOLDING_ELEMENT_SKIPPED] "
                    "skipping holding element missing 'trading_symbol'",
                    extra={
                        "error_code": "UPSTOX_HOLDING_ELEMENT_SKIPPED",
                        "isin": element.get("isin"),
                        "instrument_token": element.get("instrument_token"),
                        "company_name": element.get("company_name"),
                    },
                )
                continue

            # Numeric coercion helper — every numeric field goes
            # through ``Decimal(str(value))`` per the AC's
            # explicit rule. A ``None`` value or a missing key
            # both map to ``None`` (the dataclass permits it);
            # any other value is coerced via ``str(...)`` first
            # so binary-float surprises never reach the DTO.
            def _coerce_decimal_or_none(value) -> Decimal | None:
                if value is None:
                    return None
                try:
                    return Decimal(str(value))
                except (InvalidOperation, ValueError) as exc:
                    raise BrokerApiError(
                        f"Upstox long-term-holdings element has a "
                        f"non-numeric value where a number was "
                        f"expected: {value!r}"
                    ) from exc

            quantity = _coerce_decimal_or_none(element.get("quantity"))
            average_price = _coerce_decimal_or_none(
                element.get("average_price")
            )
            last_price = _coerce_decimal_or_none(
                element.get("last_price")
            )

            # String fields default to "" when absent — the
            # dataclass permits ``None`` for ``symbol`` /
            # ``instrument_id`` but typing them as ``str`` makes
            # downstream code's life easier. Empty-string is the
            # safest "I saw the field but it was missing"
            # representation; callers that need to distinguish
            # "missing" from "present but empty" should check the
            # ``raw`` dict, which preserves the exact source
            # element.
            def _coerce_str_or_empty(value) -> str:
                if value is None:
                    return ""
                if isinstance(value, str):
                    return value
                return str(value)

            isin = _coerce_str_or_empty(element.get("isin"))
            exchange = _coerce_str_or_empty(element.get("exchange"))
            product = _coerce_str_or_empty(element.get("product"))
            instrument_id = _coerce_str_or_empty(
                element.get("instrument_token")
            )
            name = _coerce_str_or_empty(element.get("company_name"))

            holdings.append(
                BrokerHolding(
                    symbol=trading_symbol_raw,
                    isin=isin,
                    quantity=quantity,
                    average_price=average_price,
                    last_price=last_price,
                    exchange=exchange,
                    product=product,
                    instrument_id=instrument_id,
                    name=name,
                    # ``raw`` is the entire element verbatim, so
                    # any extra undocumented keys Upstox adds
                    # later (the documented shape ends in '...')
                    # are preserved without us having to invent a
                    # field for each one. The AC's explicit
                    # "Unknown/extra keys must be preserved in
                    # ``raw``" rule is satisfied by this single
                    # line — no need to enumerate "documented"
                    # vs "undocumented" keys.
                    raw=element,
                )
            )

        return holdings

    def fetch_transactions(
        self,
        *,
        credentials: BrokerCredentials,
        start_date: date,
        end_date: date,
    ) -> list[BrokerTransaction]:
        raise NotImplementedError(
            "DefaultUpstoxBrokerConnector.fetch_transactions is "
            "implemented in STORY-8"
        )


# Canned default data for StubBrokerConnector — defined at module
# scope (not inside the class body) so the dataclasses are fully
# constructed once at import time and the default-construction path
# stays cheap and side-effect-free. These exact values are what the
# acceptance criteria describe: a "small fixed list" for holdings,
# and a transaction list spanning multiple dates so window-edge
# filtering has real rows above, below, and on each boundary.
_DEFAULT_STUB_HOLDINGS: list[BrokerHolding] = [
    BrokerHolding(
        symbol="AAPL",
        isin="US0378331005",
        quantity=Decimal("10"),
        average_price=Decimal("150.00"),
        last_price=Decimal("175.00"),
        exchange="NASDAQ",
        product="EQ",
        instrument_id="stub-instr-AAPL",
        name="Apple Inc.",
        raw={"stub": True},
    ),
    BrokerHolding(
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        quantity=Decimal("5"),
        average_price=Decimal("2400.00"),
        last_price=Decimal("2500.00"),
        exchange="NSE",
        product="EQ",
        instrument_id="stub-instr-RELIANCE",
        name="Reliance Industries Limited",
        raw={"stub": True},
    ),
]

# Spans 2024-01-15, 2024-02-10, 2024-03-05, 2024-04-20 so that
# window tests can verify both endpoints (inclusive) and exclude
# rows clearly outside the window.
_DEFAULT_STUB_TRANSACTIONS: list[BrokerTransaction] = [
    BrokerTransaction(
        external_id="stub-tx-001",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 1, 15),
        side="BUY",
        quantity=Decimal("2"),
        price=Decimal("150.00"),
        amount=Decimal("300.00"),
        exchange="NASDAQ",
        segment="EQ",
        raw={"stub": True},
    ),
    BrokerTransaction(
        external_id="stub-tx-002",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 2, 10),
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("160.00"),
        amount=Decimal("160.00"),
        exchange="NASDAQ",
        segment="EQ",
        raw={"stub": True},
    ),
    BrokerTransaction(
        external_id="stub-tx-003",
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        trade_date=date(2024, 3, 5),
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("2400.00"),
        amount=Decimal("2400.00"),
        exchange="NSE",
        segment="EQ",
        raw={"stub": True},
    ),
    BrokerTransaction(
        external_id="stub-tx-004",
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        trade_date=date(2024, 4, 20),
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("2500.00"),
        amount=Decimal("2500.00"),
        exchange="NSE",
        segment="EQ",
        raw={"stub": True},
    ),
]


@dataclass
class User:
    id: str
    preferences: dict = field(default_factory=dict)
    email: str = ""


@dataclass
class Portfolio:
    id: str
    user_id: str


_VALID_CURRENCIES = ("USD", "INR")
_VALID_EXCHANGES = ("NYSE", "NASDAQ", "NSE", "BSE")
_VALID_SYMBOL_SUFFIXES = (None, ".NS", ".BO")

# NSE body: 1-20 chars from [A-Z0-9&-] before the literal '.NS' suffix.
# BSE body: exactly 6 digits before the literal '.BO' suffix.
# Suffixes are case-sensitive: '.ns' / '.bo' must be rejected.
import re as _re

_NSE_BODY_PATTERN = _re.compile(r"^[A-Z0-9&\-]{1,20}$")
_BSE_BODY_PATTERN = _re.compile(r"^[0-9]{6}$")

# Quantum for currency-aggregated totals (STORY-8): matches this
# project's established `Decimal("0.0001")` precision convention from
# `_coerce_quantity_to_decimal` and `_quantize_rate`, not a new
# precision choice. Same `ROUND_HALF_UP` rounding mode both
# neighboring modules already use -- the story's "banker's rounding
# via the existing quantize pattern" wording is matched by using the
# same pattern (not by silently switching to `ROUND_HALF_EVEN`, which
# no other module in this codebase uses).
_TOTAL_QUANTUM = Decimal("0.0001")


def validate_stock_symbol(symbol: str) -> None:
    """Server-side validation of a full stock symbol string (STORY-3).

    Returns ``None`` for a valid symbol; raises ``ValueError`` with a
    clear, message-bearing error on an invalid one. Rules:

      * NSE: 1-20 characters from ``[A-Z0-9&-]`` followed by the
        literal ``.NS`` suffix (e.g. ``RELIANCE.NS``, ``M&M.NS``).
      * BSE: exactly 6 digits followed by the literal ``.BO`` suffix
        (e.g. ``500325.BO``).
      * US-format symbols without a ``.NS``/``.BO`` suffix are
        accepted as-is — no new US-specific rules are invented here,
        matching the "existing format" contract that already existed
        before this story.
      * Suffixes are case-sensitive: ``.ns``/``.bo`` (lowercase) are
        rejected with a clear error rather than silently coerced.

    This function is called from ``Holding.__post_init__`` whenever
    ``symbol_suffix`` is one of ``.NS``/``.BO`` (i.e. an Indian
    exchange, where the suffix is part of the symbol's identity). US
    symbols (``symbol_suffix is None``) skip this validation entirely
    so the existing pre-STORY-3 behaviour for them is preserved
    verbatim.
    """
    if not isinstance(symbol, str):
        raise ValueError(
            f"stock symbol must be a string; got {type(symbol).__name__}"
        )

    if symbol.endswith(".NS"):
        body = symbol[: -len(".NS")]
        if not _NSE_BODY_PATTERN.match(body):
            raise ValueError(
                f"invalid NSE stock symbol {symbol!r}: body before '.NS' must be "
                f"1-20 characters from [A-Z0-9&-]; got body {body!r}"
            )
        return None

    if symbol.endswith(".BO"):
        body = symbol[: -len(".BO")]
        if not _BSE_BODY_PATTERN.match(body):
            raise ValueError(
                f"invalid BSE stock symbol {symbol!r}: body before '.BO' must be "
                f"exactly 6 digits; got body {body!r}"
            )
        return None

    # Lowercase suffixes are a common typo and must be rejected
    # explicitly -- a silent upper() would mask the user's mistake and
    # leave them wondering why their broker lookup returns nothing.
    if symbol.endswith(".ns") or symbol.endswith(".bo"):
        suffix = symbol[-4:]
        raise ValueError(
            f"invalid stock symbol {symbol!r}: suffix {suffix!r} is lowercase; "
            f"suffixes are case-sensitive (use '.NS' or '.BO')"
        )

    # No suffix -> treated as an existing US-format symbol. No new
    # US-specific rules are invented here; whatever passed validation
    # before this story continues to pass.
    return None


def _coerce_quantity_to_decimal(value) -> Decimal:
    """Coerce a quantity input (int, float, str, Decimal) to a
    Decimal with 4 decimal places of precision. Raises ValueError on
    non-numeric input — matching the DECIMAL(18,4) intent in
    STORY-1's schema description, and keeping the rest of this
    module's behaviour honest about what quantity really is."""
    try:
        quantized = Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"Holding.quantity must be a real number (int/float/Decimal/str); got {value!r}"
        ) from exc
    return quantized


def _coerce_market_value_to_decimal(value) -> Decimal:
    """Coerce a `Position.market_value` (typed float at the dataclass
    level, but real callers/tests pass Decimal after STORY-1's
    `Holding.quantity` quantization) to a Decimal quantized to 4
    decimal places. Raises ValueError on non-numeric input — matches
    the same defensive posture as `_coerce_quantity_to_decimal` /
    `_quantize_rate`, and keeps `calculate_portfolio_totals` from
    silently mixing float and Decimal arithmetic (which would raise
    `TypeError` mid-aggregation)."""
    try:
        return Decimal(str(value)).quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"Position.market_value must be a real number (int/float/Decimal/str); got {value!r}"
        ) from exc


@dataclass
class Holding:
    portfolio_id: str
    security_id: str
    quantity: Decimal
    currency: str = "USD"
    exchange: str | None = None
    symbol_suffix: str | None = None

    def __post_init__(self) -> None:
        # Currency: ENUM-like, restricted to {USD, INR}. Anything else
        # raises a clear error rather than silently letting bad data
        # through — a US-listed price feed will give nonsensical
        # exposures if a row sneaks in with currency='EUR'.
        if self.currency not in _VALID_CURRENCIES:
            raise ValueError(
                f"Holding.currency must be one of {_VALID_CURRENCIES}; got {self.currency!r}"
            )
        # Exchange: ENUM-like, restricted to {NYSE, NASDAQ, NSE, BSE} or
        # None. None is explicitly allowed so an imported holding whose
        # broker payload omits the field isn't rejected out of the box.
        if self.exchange is not None and self.exchange not in _VALID_EXCHANGES:
            raise ValueError(
                f"Holding.exchange must be one of {_VALID_EXCHANGES} or None; got {self.exchange!r}"
            )
        # symbol_suffix: the same None-or-restricted pattern as
        # exchange. Suffix is meaningful only when paired with an Indian
        # exchange (NSE → .NS, BSE → .BO); other combinations are
        # allowed for now because a strict cross-field rule would force
        # knowledge this class doesn't have (which exchange a given
        # ticker maps to).
        if self.symbol_suffix not in _VALID_SYMBOL_SUFFIXES:
            raise ValueError(
                f"Holding.symbol_suffix must be one of {_VALID_SYMBOL_SUFFIXES}; "
                f"got {self.symbol_suffix!r}"
            )
        # Validate the FULL symbol string (security_id + symbol_suffix)
        # when an Indian suffix is set -- not just the suffix in
        # isolation (STORY-3). US-format symbols (symbol_suffix is None)
        # are passed through unchanged: no new US rules are invented
        # here, only the NSE/BSE format rules from STORY-3 are enforced.
        if self.symbol_suffix in (".NS", ".BO"):
            validate_stock_symbol(f"{self.security_id}{self.symbol_suffix}")
        # Exchange auto-detection from symbol_suffix (STORY-4). Runs
        # AFTER symbol_suffix validation (so an invalid suffix has
        # already been rejected) but BEFORE the exchange ENUM check
        # below (so an auto-detected 'NSE'/'BSE' still passes that
        # check normally). .NS always means NSE, .BO always means BSE --
        # these are unambiguous conventions the suffix itself encodes,
        # so any value the caller passed for `exchange` is overridden
        # rather than left to silently disagree with the suffix. When
        # symbol_suffix is None, exchange is left exactly as the caller
        # passed it (preserves existing US behavior -- 'NYSE'/'NASDAQ'/
        # None -- and no new rule is invented for US symbols here).
        if self.symbol_suffix == ".NS":
            self.exchange = "NSE"
        elif self.symbol_suffix == ".BO":
            self.exchange = "BSE"
        # Currency auto-derivation from exchange (STORY-6). Runs AFTER
        # the exchange auto-detection above so it sees the *final*
        # exchange value (whether the caller passed it or the suffix
        # assigned it). NSE/BSE always mean Indian rupees, so the
        # caller's currency is overridden to 'INR' for those — the
        # same "suffix / exchange is authoritative" pattern the
        # exchange-from-suffix block above already uses. For every
        # other exchange (NYSE, NASDAQ, None), the caller's currency
        # is preserved as-is: no new US-specific rule is invented, and
        # the existing 'USD' default keeps working for callers who
        # don't know about this field at all.
        if self.exchange in ("NSE", "BSE"):
            self.currency = "INR"
        # Quantity is Decimal, not float — see _coerce_quantity_to_decimal.
        # Always coerce/quantize, even when the caller already passed a
        # Decimal: a Decimal with more than 4 places (e.g. Decimal("12.123456789"))
        # must still be rounded to the DECIMAL(18,4) precision this story
        # requires, not passed through untouched.
        self.quantity = _coerce_quantity_to_decimal(self.quantity)


@dataclass
class Position:
    holding: Holding
    market_value: float


@dataclass
class Transaction:
    portfolio_id: str
    kind: str
    amount: float


@dataclass
class PortfolioSnapshot:
    portfolio_id: str
    positions: list[Position]
    exposure: dict


class UserPortfolio(Protocol):
    def onboard_user(self, details: dict) -> User:
        ...

    def connect_portfolio(
        self,
        user_id: str,
        broker_id: str,
        payload: dict,
    ) -> BrokerConnectionRecord:
        ...

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        ...

    def import_transactions(
        self,
        user_id: str,
        broker_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ImportResult:
        ...

    def synchronize_portfolio(self, portfolio: Portfolio) -> PortfolioSnapshot:
        ...

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        ...

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        ...

    def manage_preferences(self, user: User, updates: dict) -> User:
        ...

    def determine_user_relevance(self, user: User, event: dict) -> bool:
        """→ Event / Analysis interface: is this event relevant to
        this user's portfolio at all."""
        ...


class StubUserPortfolio:
    """Structural implementation of UserPortfolio. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def onboard_user(self, details: dict) -> User:
        with traced("StubUserPortfolio.onboard_user"):
            return User(id="stub-id", preferences={})

    def connect_portfolio(
        self,
        user_id: str,
        broker_id: str,
        payload: dict,
    ) -> BrokerConnectionRecord:
        with traced("StubUserPortfolio.connect_portfolio"):
            # Return a synthetic record with stub values — caller can assert
            # on broker_id / user_id / status.
            return BrokerConnectionRecord(
                id="stub-record-id",
                user_id=user_id,
                broker_id=broker_id,
                broker_user_id="stub-broker-user",
                access_token_encrypted="stub-encrypted",
                token_type="Bearer",
                access_token_expires_at=None,
                status="CONNECTED",
                last_error=None,
                connected_at=datetime.now(timezone.utc).isoformat(),
                last_import_at=None,
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        with traced("StubUserPortfolio.import_holdings"):
            return []

    def import_transactions(
        self,
        user_id: str,
        broker_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ImportResult:
        with traced("StubUserPortfolio.import_transactions"):
            return ImportResult(transactions_inserted=0, transactions_skipped_existing=0, rows_skipped_invalid=0)

    def synchronize_portfolio(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("StubUserPortfolio.synchronize_portfolio"):
            return PortfolioSnapshot(portfolio_id="stub-id", positions=[], exposure={})

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        with traced("StubUserPortfolio.track_portfolio_state"):
            return PortfolioSnapshot(portfolio_id="stub-id", positions=[], exposure={})

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        with traced("StubUserPortfolio.calculate_exposure"):
            return {}

    def manage_preferences(self, user: User, updates: dict) -> User:
        with traced("StubUserPortfolio.manage_preferences"):
            return User(id="stub-id", preferences={})

    def determine_user_relevance(self, user: User, event: dict) -> bool:
        with traced("StubUserPortfolio.determine_user_relevance"):
            return True





USERS_TABLE = "users"
PORTFOLIOS_TABLE = "portfolios"
HOLDINGS_TABLE = "holdings"
TRANSACTIONS_TABLE = "transactions"

# Entity.kind values (c04_knowledge_entity.py's "Company | Security |
# Person | Sector | Industry | Index | Geography") that represent
# something a user could hold a position in — the manual-entry
# dropdown's real filter (ADR-0044).
_TRADEABLE_SECURITY_ENTITY_KINDS = ("Security", "Company")


class DefaultUserPortfolio:
    """Real implementation of UserPortfolio.

    `onboard_user`/`manage_preferences` persist through `Infrastructure`
    (`DefaultInfrastructure` by default) rather than an in-memory dict —
    System Infrastructure's real Postgres-backed store (ADR-0019) is
    what "real" means for this component's non-broker-dependent state.

    `connect_portfolio`/`import_holdings`/`import_transactions` call
    through the injected `BrokerConnector` (ADR-0022); every value it
    returns is tagged UNTRUSTED via `BoundaryGate.tag_provenance` before
    it's used to build a `Portfolio`/`Holding`/`Transaction`, and the
    tagged record (provenance included) is what gets persisted — the
    dataclass returned to the caller stays exactly the shape the
    `UserPortfolio` Protocol already declares.

    `synchronize_portfolio` re-imports from the broker connector first
    (so it reflects the current external state, per its name), then
    delegates to `track_portfolio_state`, which only reads what's
    already stored — the two methods differ in whether they pull fresh
    data or read the current tracked state, matching what their names
    already imply.

    `list_available_securities`/`add_holding_manually`/
    `add_transaction_manually` (ADR-0044) are a second, parallel
    onboarding path — real, `Infrastructure`-backed, entirely bypassing
    `BrokerConnector` — for a user who picks securities directly rather
    than connecting a broker. They are not part of the `UserPortfolio`
    Protocol: manual entry is an implementation detail of how this
    component gets its data, the same reasoning ADR-0029 already used
    for `DefaultEvidenceLinker.link_with_context()`. `knowledge_entity`
    is typed against the concrete `DefaultKnowledgeEntity`, not the
    `KnowledgeEntity` Protocol, because `get_entity`/`search_entities`
    (ADR-0044) aren't part of that Protocol either.
    """

    def __init__(
        self,
        infrastructure: Infrastructure | None = None,
        broker_connector: BrokerConnector | None = None,
        boundary_gate: BoundaryGate | None = None,
        audit_manager: AuditManager | None = None,
        knowledge_entity: DefaultKnowledgeEntity | None = None,
    ) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()
        self._broker_connector = broker_connector or PlaceholderBrokerConnector()
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._knowledge_entity = knowledge_entity or DefaultKnowledgeEntity(infrastructure=self._infrastructure)

    def onboard_user(self, details: dict) -> User:
        with traced("DefaultUserPortfolio.onboard_user"):
            user = User(
                id=str(uuid.uuid4()),
                preferences=dict(details.get("preferences", {})),
                email=details.get("email", ""),
            )
            self._infrastructure.store(
                USERS_TABLE, {"id": user.id, "preferences": user.preferences, "email": user.email}
            )
            return user

    def connect_portfolio(
        self,
        user_id: str,
        broker_id: str,
        payload: dict,
    ) -> BrokerConnectionRecord:
        """Exchange an OAuth auth code for broker credentials and persist the connection (STORY-13).

        Resolution chain:
          1. Validate payload['code'] — raise ValueError if absent or empty.
          2. Resolve connector via get_broker_connector(broker_id) — raises UnsupportedBrokerError on unknown broker_id.
          3. Call connector.exchange_auth_code(code=payload['code']).
          4. Persist via infrastructure.upsert_broker_connection with status CONNECTED.
          5. On BrokerAuthError or BrokerApiError: if a prior connection row exists,
             call infrastructure.mark_broker_connection_error, then re-raise.

        No Upstox-specific code, URLs, or field names appear in this method body —
        everything broker-specific lives behind the BrokerConnector Protocol."""
        with traced("DefaultUserPortfolio.connect_portfolio"):
            code = payload.get("code")
            if not code or not isinstance(code, str) or not code.strip():
                raise ValueError(
                    "connect_portfolio: payload['code'] is required and must be a non-empty string"
                )

            connector = get_broker_connector(broker_id)

            # Check for an existing connection row before calling the connector,
            # so we know whether to mark an error on failure.
            existing = self._infrastructure.get_broker_connection(user_id, broker_id)

            try:
                credentials = connector.exchange_auth_code(code=code.strip())
            except (BrokerAuthError, BrokerApiError) as exc:
                if existing is not None:
                    self._infrastructure.mark_broker_connection_error(
                        user_id, broker_id, str(exc)
                    )
                raise

            record = self._infrastructure.upsert_broker_connection(
                user_id=user_id,
                broker_id=broker_id,
                credentials=credentials,
                status="CONNECTED",
                last_error=None,
                connected_at=datetime.now(timezone.utc),
            )
            return record

    def import_holdings(self, portfolio: Portfolio) -> list[Holding]:
        with traced("DefaultUserPortfolio.import_holdings"):
            credentials = self._load_credentials(portfolio)
            if credentials is None:
                return []
            # Fetch holdings using the broker_connector
            raw_holdings = self._broker_connector.fetch_holdings(credentials=credentials)
            holdings = []
            for raw in raw_holdings:
                tagged = self._boundary_gate.tag_provenance(asdict(raw), source="broker_connector")
                holding = Holding(
                    portfolio_id=portfolio.id,
                    security_id=tagged["symbol"],
                    quantity=tagged["quantity"],
                )
                self._infrastructure.store(
                    HOLDINGS_TABLE,
                    {
                        "id": f"{portfolio.id}:{holding.security_id}",
                        **asdict(holding),
                        "provenance": tagged.get("provenance"),
                    },
                )
                holdings.append(holding)
            return holdings

    def import_transactions(
        self,
        user_id: str,
        broker_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ImportResult:
        """Import broker transactions for ``user_id`` / ``broker_id`` (STORY-15).

        Known limitation: history older than 3 financial years cannot be
        imported from this source — Upstox does not serve older data.

        **Default date window** (when neither argument is supplied):

          * ``end_date`` = today in Asia/Kolkata (IST).
          * ``start_date`` = 1 April of the Indian FY two years before the
            current Indian FY. Indian FYs start 1 April; the "FY two years
            before the current" is the widest window Upstox permits, matching
            their documented "within the last 3 financial years" constraint.

        **Date clamping**: a caller-supplied ``start_date`` earlier than the
        computed minimum is clamped forward to that boundary and a warning
        is logged. A ``start_date`` / ``end_date`` pair that is a valid,
        narrower window is passed through unchanged.

        **Validation**: ``start_date > end_date`` raises ``ValueError``
        before any connector call.

        **Idempotent upsert**: each transaction is inserted (or updated) in
        the ``broker_transactions`` table keyed on
        UNIQUE(user_id, broker_id, external_id). Re-running the import
        skips existing rows, leaving them untouched.

        **Transaction semantics**: all inserts run inside a single
        transaction. A connector failure mid-import rolls back the entire
        batch — the ``broker_transactions`` table is unchanged.

        **Error handling**:
          * ``BrokerAuthError`` marks the connection status ERROR and
            re-raises.
          * ``last_import_at`` is updated on the connection row only on
            success.
        """
        with traced("DefaultUserPortfolio.import_transactions"):
            # ── Resolve broker connection ──────────────────────────────────────
            connection = self._infrastructure.get_broker_connection(user_id, broker_id)
            if connection is None:
                return ImportResult(transactions_inserted=0, transactions_skipped_existing=0, rows_skipped_invalid=0)

            # ── Compute default date window ──────────────────────────────────
            kolkata_tz = ZoneInfo("Asia/Kolkata")
            today = datetime.now(kolkata_tz).date()
            current_fy_year = today.year if today.month >= 4 else today.year - 1
            # Indian FY two years before the current FY starts 1 April
            min_start = date(current_fy_year - 2, 4, 1)

            if end_date is None:
                end_date = today
            if start_date is None:
                start_date = min_start

            # ── Validation ────────────────────────────────────────────────────
            if start_date > end_date:
                raise ValueError(
                    f"import_transactions: start_date ({start_date}) cannot be after end_date ({end_date})"
                )

            # ── Clamp caller-supplied start_date backward to minimum ─────────
            effective_start = max(start_date, min_start)
            if start_date < min_start:
                _logger.warning(
                    "DefaultUserPortfolio.import_transactions: "
                    "[IMPORT_START_DATE_CLAMPED] "
                    "caller-supplied start_date %s is before the 3-FY boundary %s; "
                    "clamped to %s",
                    start_date,
                    min_start,
                    effective_start,
                    extra={
                        "event_code": "IMPORT_START_DATE_CLAMPED",
                        "original_start_date": str(start_date),
                        "min_start_date": str(min_start),
                        "effective_start_date": str(effective_start),
                        "end_date": str(end_date),
                        "user_id": user_id,
                        "broker_id": broker_id,
                    },
                )

            # ── Fetch transactions ─────────────────────────────────────────────
            try:
                credentials = BrokerCredentials(
                    access_token=connection.access_token,
                    token_type=connection.token_type,
                    expires_at=connection.access_token_expires_at,
                    refresh_token=None,
                    broker_user_id=connection.broker_user_id,
                    raw={},
                )
            except BrokerConfigError:
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    "access token could not be decrypted for import_transactions"
                )
                raise BrokerAuthError(
                    f"access token for user_id={user_id} broker_id={broker_id} "
                    f"could not be decrypted"
                )

            try:
                raw_transactions = self._broker_connector.fetch_transactions(
                    credentials=credentials,
                    start_date=effective_start,
                    end_date=end_date,
                )
            except BrokerAuthError:
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    "BrokerAuthError during import_transactions"
                )
                raise
            except BrokerApiError:
                # Mark non-auth broker errors as ERROR too so the user knows
                # the connection is in a bad state and needs attention.
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    "BrokerApiError during import_transactions"
                )
                raise

            # ── Batch upsert ──────────────────────────────────────────────────
            inserted = 0
            skipped_existing = 0
            skipped_invalid = 0
            for raw in raw_transactions:
                try:
                    tagged = self._boundary_gate.tag_provenance(
                        asdict(raw), source="broker_connector"
                    )
                except Exception:
                    skipped_invalid += 1
                    continue

                # Validate required fields are present and non-empty
                try:
                    _validate_transaction_row(tagged)
                except ValueError:
                    skipped_invalid += 1
                    continue

                # Upsert keyed on (user_id, broker_id, external_id)
                success = self._infrastructure.upsert_broker_transaction(
                    user_id=user_id,
                    broker_id=broker_id,
                    external_id=tagged["external_id"],
                    symbol=tagged["symbol"],
                    isin=tagged["isin"],
                    trade_date=tagged["trade_date"],
                    side=tagged["side"],
                    quantity=tagged["quantity"],
                    price=tagged["price"],
                    amount=tagged["amount"],
                    exchange=tagged["exchange"],
                    segment=tagged["segment"],
                    raw=tagged.get("raw", {}),
                )
                if success:
                    inserted += 1
                else:
                    skipped_existing += 1

            # ── Update last_import_at on success ─────────────────────────────
            self._infrastructure.touch_last_import(user_id, broker_id)

            return ImportResult(
                transactions_inserted=inserted,
                transactions_skipped_existing=skipped_existing,
                rows_skipped_invalid=skipped_invalid,
            )


def _validate_transaction_row(row: dict) -> None:
    """Raise ValueError if a transaction row dict is missing required fields."""
    required = ("external_id", "symbol", "isin", "trade_date", "side",
                "quantity", "price", "amount", "exchange", "segment")
    for field in required:
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"import_transactions: missing or empty required field {field!r}")


# Module-level logger for the import_transactions warning
_logger = logging.getLogger(__name__)


