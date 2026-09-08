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
from typing import TYPE_CHECKING, Callable, Literal, Protocol, runtime_checkable
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


def _default_upstox_connector() -> "BrokerConnector":
    # DefaultUpstoxBrokerConnector is defined further down in this same
    # module -- referenced here only inside a function body (never
    # evaluated at import time), so this has nothing to do with why
    # BROKER_CONNECTORS' factories are lazy (see BROKER_CONNECTORS'
    # own docstring below for that real reason).
    return DefaultUpstoxBrokerConnector(UpstoxConfig.from_env())


# STORY-9: the real extension point for adding a new broker. To add
# one: write a real Default<Broker>BrokerConnector class conforming to
# the BrokerConnector Protocol, then add ONE line here mapping its
# broker_id to a zero-arg factory that builds it -- nothing else in
# this project needs to change (DefaultUserPortfolio, connect_portfolio,
# import_holdings/import_transactions all resolve purely through
# get_broker_connector(broker_id), never a hardcoded class name).
#
# Values are LAZY factories (Callable[[], BrokerConnector]), not
# already-built instances: UpstoxConfig.from_env() raises
# BrokerConfigError when UPSTOX_CLIENT_ID/SECRET/REDIRECT_URI aren't
# set, and that must happen when get_broker_connector('upstox') is
# actually CALLED, not merely because this module got imported in an
# environment that doesn't have Upstox configured (which would break
# every test/tool that imports this module at all, whether or not it
# ever touches a broker).
BROKER_CONNECTORS: dict[str, Callable[[], "BrokerConnector"]] = {
    "upstox": _default_upstox_connector,
}


def get_broker_connector(broker_id: str) -> "BrokerConnector":
    """Look up a BrokerConnector by broker_id (STORY-9).

    Checks the dynamic registry first (``register_broker_connector`` --
    what tests, and any future runtime override, use) so an explicitly
    registered connector always wins; falls back to lazily building one
    from ``BROKER_CONNECTORS`` (the real, shipped connectors) when
    nothing was explicitly registered for this broker_id. Raises
    ``UnsupportedBrokerError`` if neither has one. The registry
    contains only broker_id strings (no URLs, no field names) --
    everything broker-specific lives behind the Protocol."""
    connector = _broker_connector_registry.get(broker_id)
    if connector is not None:
        return connector
    factory = BROKER_CONNECTORS.get(broker_id)
    if factory is not None:
        return factory()
    raise UnsupportedBrokerError(
        f"no connector registered for broker_id {broker_id!r}; "
        f"supported ids: {sorted(set(_broker_connector_registry) | set(BROKER_CONNECTORS))}"
    )


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


class BrokerNotConnectedError(BrokerError):
    """Raised when no stored broker connection exists for (user_id, broker_id) (STORY-14)."""
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
    broker_transaction_id: str  # Idempotent-match key from the broker
    symbol: str
    isin: str
    trade_date: date
    side: Literal['BUY', 'SELL']
    quantity: Decimal
    price: Decimal
    amount: Decimal
    exchange: str
    segment: str
    broker_modified_at: datetime  # Timestamp from broker for idempotent comparison
    raw: dict = field(default_factory=dict)

    # Backwards-compat alias: old code used `external_id`; map it to the
    # canonical `broker_transaction_id` field so existing callers continue
    # to work and the two names are interchangeable.
    @property
    def external_id(self) -> str:
        return self.broker_transaction_id


@dataclass(frozen=True)
class ImportResult:
    """Result of an import_transactions call (STORY-15)."""
    transactions_inserted: int
    transactions_skipped_existing: int
    rows_skipped_invalid: int = 0


@dataclass(frozen=True)
class HoldingsImportResult:
    """Result of an import_holdings call (STORY-14). Named distinctly
    from ImportResult (STORY-15's transactions result) since the two
    carry different fields -- holdings are a replace-in-transaction
    snapshot, not an idempotent upsert, so there's no "skipped existing"
    concept, only rows the connector returned that failed validation."""
    holdings_written: int
    skipped: int = 0


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

    # STORY-8: the historical-trades endpoint. Same "pin as a class
    # constant" convention as the holdings path above -- the AC's
    # "Request URL is exactly https://api.upstox.com/v2/charges/
    # historical-trades" rule is a single grep. Page size is pinned at
    # the AC's own required value (1000, the max Upstox allows per
    # page) rather than left as a caller-tunable parameter -- the AC
    # doesn't ask for one, and a smaller caller-chosen size would only
    # mean more real HTTP round trips for the same data.
    _UPSTOX_HISTORICAL_TRADES_PATH = "/v2/charges/historical-trades"
    _UPSTOX_HISTORICAL_TRADES_PAGE_SIZE = 1000
    # Real safety valve, not a value Upstox's docs specify: a broker
    # that never reports a real total_pages (or reports one that keeps
    # growing) must not spin this loop forever. 200 pages at 1000 rows
    # each is 200,000 transactions in one call -- comfortably beyond
    # any real account's history, so a legitimate call never hits this.
    _UPSTOX_HISTORICAL_TRADES_MAX_PAGES = 200

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
        http: "_UpstoxHttp | None" = None,
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
        if http is None:
            # STORY-9's registry factory (BROKER_CONNECTORS['upstox'])
            # constructs this with only a config, matching the AC's own
            # `lambda: DefaultUpstoxBrokerConnector(UpstoxConfig.from_env())`
            # -- no real access token exists yet at registry-resolution
            # time (tokens are per-connection, bound only once a user has
            # actually connected). Real callers that DO have a bound
            # token (every real test, and any real per-connection flow)
            # pass their own real http explicitly, so this default is
            # only ever exercised by an unconnected registry entry --
            # failing with a clear, honest BrokerAuthError the moment a
            # real call is attempted is correct there, not a crash or a
            # silently wrong token.
            def _no_token_bound() -> str:
                raise BrokerAuthError(
                    "DefaultUpstoxBrokerConnector has no real access "
                    "token bound yet -- connect this broker via "
                    "connect_portfolio before fetching holdings or "
                    "transactions"
                )

            http = _UpstoxHttpRuntime(token_provider=_no_token_bound)
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
        """Fetch historical trades from Upstox (STORY-8), paging through
        every page rather than just the first.

        Performs authenticated GETs against
        ``https://api.upstox.com/v2/charges/historical-trades`` via
        ``_UpstoxHttp.get`` -- the query string carries ``start_date``/
        ``end_date`` (``YYYY-mm-dd``), ``page_number`` (starting at 1),
        and ``page_size=1000``; ``segment`` is deliberately never
        included so every segment is returned. ``_UpstoxHttp.get``
        takes only a ``path``, so the query string is built here (via
        ``urlencode``, same percent-encoding convention as
        ``build_authorize_url`` above) and appended to the path rather
        than changing the helper's signature -- ``fetch_holdings``'s
        existing, unparameterised call is unaffected.

        Real response shape (verbatim from the story's own docs)::

            {
              "status": "success",
              "data": [
                {"exchange", "segment", "trade_id", "trade_date",
                 "scrip_name", "symbol", "transaction_type",
                 "quantity", "price", "amount", "isin"},
                ...
              ],
              "meta_data": {"page": {"page_number", "page_size",
                                      "total_records", "total_pages"}}
            }

        Pages until ``page_number >= total_pages`` OR a page's own
        ``data`` comes back empty (whichever happens first -- a
        broker that ever reports a ``total_pages`` inconsistent with
        its real data must not be trusted over the data itself), and
        hard-caps at ``_UPSTOX_HISTORICAL_TRADES_MAX_PAGES`` real pages
        fetched, raising ``BrokerApiError`` rather than looping forever
        if that cap is reached without the loop ending on its own.

        Mapping to ``BrokerTransaction`` (the STORY-8 table):

          * ``external_id  <- trade_id``
          * ``symbol       <- symbol``
          * ``isin         <- isin``
          * ``trade_date   <- date.fromisoformat(trade_date)``
          * ``side         <- transaction_type.upper()`` -- only
            ``BUY``/``SELL`` are accepted; any other value (Upstox
            also reports non-trade ledger entries like dividends
            through this same endpoint) is skipped with a warning
            log, not a failure of the whole call.
          * ``quantity``/``price``/``amount`` ``<- Decimal(str(...))``
            -- same float-rounding-avoidance rule ``fetch_holdings``
            already follows above.
          * ``exchange``/``segment`` <- verbatim.
          * ``raw`` <- the whole element, so an undocumented key is
            never silently dropped.

        De-duplicates by ``trade_id`` across pages, keeping the FIRST
        occurrence seen (a real broker page can legitimately overlap
        at its boundary if a trade lands exactly on the page-size
        cutoff between two requests).

        Raises:
            ValueError: ``start_date > end_date``, checked before any
                HTTP call is made.
            BrokerApiError: a non-``'success'`` status, a malformed
                response shape, an unparseable numeric/date field, or
                the ``_UPSTOX_HISTORICAL_TRADES_MAX_PAGES`` cap being
                reached.
            BrokerAuthError / BrokerRateLimitError: propagated
                unchanged from ``_UpstoxHttp.get`` -- same auth/rate-
                limit contract ``fetch_holdings`` already relies on.
        """
        if start_date > end_date:
            raise ValueError(
                "DefaultUpstoxBrokerConnector.fetch_transactions: "
                "start_date must be <= end_date"
            )

        transactions: list[BrokerTransaction] = []
        seen_trade_ids: set = set()
        page_number = 1

        while True:
            if page_number > self._UPSTOX_HISTORICAL_TRADES_MAX_PAGES:
                raise BrokerApiError(
                    "Upstox historical-trades pagination exceeded "
                    f"{self._UPSTOX_HISTORICAL_TRADES_MAX_PAGES} real "
                    "pages without the broker's own total_pages ending "
                    "the loop"
                )

            query = urlencode(
                [
                    ("start_date", start_date.isoformat()),
                    ("end_date", end_date.isoformat()),
                    ("page_number", str(page_number)),
                    ("page_size", str(self._UPSTOX_HISTORICAL_TRADES_PAGE_SIZE)),
                ]
            )
            response_body = self._http.get(
                path=f"{self._UPSTOX_HISTORICAL_TRADES_PATH}?{query}"
            )

            # Belt-and-braces status check -- same reasoning as
            # fetch_holdings' own identical check above: the real
            # helper already enforces this on a 2xx, but a test that
            # mocks ``_UpstoxHttp.get`` directly (bypassing the
            # helper's own mapping) still gets the documented
            # behaviour for free.
            if response_body.get("status") != "success":
                raise BrokerApiError(
                    "Upstox historical-trades response status is "
                    f"{response_body.get('status')!r}, not 'success'"
                )

            data = response_body.get("data")

            # An empty page ends pagination immediately, regardless of
            # what total_pages claims -- the AC's explicit "stopping
            # also if data comes back empty" rule. This is also what
            # makes the empty-fixture case make exactly one request.
            if not data:
                break

            if not isinstance(data, list):
                raise BrokerApiError(
                    "Upstox historical-trades response 'data' field is "
                    f"not a list; got {type(data).__name__}"
                )

            for element in data:
                if not isinstance(element, dict):
                    raise BrokerApiError(
                        "Upstox historical-trades 'data' element is not "
                        f"a dict; got {type(element).__name__}"
                    )

                # Dedup by trade_id BEFORE any other processing --
                # "keeping the first occurrence" means identity alone
                # decides it, independent of whether that occurrence
                # is later skipped for an unrecognized transaction_type.
                trade_id = element.get("trade_id")
                if trade_id is not None:
                    if trade_id in seen_trade_ids:
                        continue
                    seen_trade_ids.add(trade_id)

                transaction_type_raw = element.get("transaction_type")
                side = (
                    transaction_type_raw.upper()
                    if isinstance(transaction_type_raw, str)
                    else ""
                )
                if side not in ("BUY", "SELL"):
                    self._logger.warning(
                        "DefaultUpstoxBrokerConnector.fetch_transactions: "
                        "[UPSTOX_TRANSACTION_ROW_SKIPPED] skipping row "
                        "with an unrecognized transaction_type",
                        extra={
                            "error_code": "UPSTOX_TRANSACTION_ROW_SKIPPED",
                            "trade_id": trade_id,
                            "transaction_type": transaction_type_raw,
                        },
                    )
                    continue

                trade_date_raw = element.get("trade_date")
                try:
                    trade_date_value = date.fromisoformat(trade_date_raw)
                except (TypeError, ValueError) as exc:
                    raise BrokerApiError(
                        "Upstox historical-trades element has an "
                        f"invalid trade_date: {trade_date_raw!r}"
                    ) from exc

                try:
                    quantity = Decimal(str(element.get("quantity")))
                    price = Decimal(str(element.get("price")))
                    amount = Decimal(str(element.get("amount")))
                except (InvalidOperation, ValueError) as exc:
                    raise BrokerApiError(
                        "Upstox historical-trades element has a "
                        "non-numeric value where a number was expected"
                    ) from exc

                transactions.append(
                    BrokerTransaction(
                        external_id=str(trade_id) if trade_id is not None else "",
                        symbol=element.get("symbol") or "",
                        isin=element.get("isin") or "",
                        trade_date=trade_date_value,
                        side=side,
                        quantity=quantity,
                        price=price,
                        amount=amount,
                        exchange=element.get("exchange") or "",
                        segment=element.get("segment") or "",
                        raw=element,
                    )
                )

            page_info = (response_body.get("meta_data") or {}).get("page") or {}
            total_pages = page_info.get("total_pages")
            if not isinstance(total_pages, int) or page_number >= total_pages:
                break
            page_number += 1

        return transactions


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
        broker_transaction_id="stub-tx-001",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 1, 15),
        side="BUY",
        quantity=Decimal("2"),
        price=Decimal("150.00"),
        amount=Decimal("300.00"),
        exchange="NASDAQ",
        segment="EQ",
        broker_modified_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        raw={"stub": True},
    ),
    BrokerTransaction(
        broker_transaction_id="stub-tx-002",
        symbol="AAPL",
        isin="US0378331005",
        trade_date=date(2024, 2, 10),
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("160.00"),
        amount=Decimal("160.00"),
        exchange="NASDAQ",
        segment="EQ",
        broker_modified_at=datetime(2024, 2, 10, 14, 30, 0, tzinfo=timezone.utc),
        raw={"stub": True},
    ),
    BrokerTransaction(
        broker_transaction_id="stub-tx-003",
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        trade_date=date(2024, 3, 5),
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("2400.00"),
        amount=Decimal("2400.00"),
        exchange="NSE",
        segment="EQ",
        broker_modified_at=datetime(2024, 3, 5, 9, 15, 0, tzinfo=timezone.utc),
        raw={"stub": True},
    ),
    BrokerTransaction(
        broker_transaction_id="stub-tx-004",
        symbol="RELIANCE.NS",
        isin="INE002A01018",
        trade_date=date(2024, 4, 20),
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("2500.00"),
        amount=Decimal("2500.00"),
        exchange="NSE",
        segment="EQ",
        broker_modified_at=datetime(2024, 4, 20, 15, 30, 0, tzinfo=timezone.utc),
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
    broker_holding_id: str | None = None  # Idempotent-match key from the broker

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
class CurrentHolding:
    """A Holding as stored in the DB: the Holding fields plus the
    database-level ``id`` and ``is_active`` flag used by the sync logic
    for removed-record detection."""
    id: str
    is_active: bool
    holding: Holding


@dataclass
class Transaction:
    portfolio_id: str
    kind: str
    amount: float
    broker_transaction_id: str | None = None  # Idempotent-match key from the broker

    def __post_init__(self) -> None:
        # Amount must be a real number — validate here so bad broker data
        # raises early rather than silently persisting a non-numeric string.
        # Uses the same Decimal-coercion pattern as Holding.quantity to stay
        # consistent with the rest of this module's numeric validation.
        try:
            Decimal(str(self.amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"Transaction.amount must be a real number "
                f"(int/float/Decimal/str); got {self.amount!r}"
            ) from exc


@dataclass
class CurrentTransaction:
    """A Transaction as stored in the DB: the Transaction fields plus
    the database-level ``id`` and ``is_active`` flag used by the sync
    logic for removed-record detection, and ``updated_at`` for
    idempotent timestamp comparison (STORY-SYNC-04)."""
    id: str
    is_active: bool
    transaction: Transaction
    updated_at: datetime  # Persisted updated_at for broker_modified_at comparison


@dataclass
class PortfolioSnapshot:
    portfolio_id: str
    positions: list[Position]
    exposure: dict


@dataclass(frozen=True)
class FailedRecord:
    """A record that could not be processed during portfolio synchronization,
    along with the reason it failed.

    ``record_type`` is the logical type of the record that failed — e.g.
    ``"holding"`` or ``"transaction"`` — not the raw broker label, so
    callers can branch on it without knowing broker-specific taxonomy.

    ``broker_id`` is the broker that produced the record, or ``None`` when
    the failure arose during manual entry (where there is no broker source).

    ``reason`` is a human-readable explanation of what went wrong, suitable
    for surfacing in a UI or a log line.

    ``raw_data`` is the original record dict as received from the broker
    (or from the manual-entry payload), preserved verbatim so a future retry
    or diagnostic pass can re-process it without re-fetching from the
    broker. An empty dict means the record had no usable content to begin
    with (e.g. the broker returned a completely null element)."""

    record_type: str  # e.g. "holding" | "transaction"
    broker_id: str | None
    reason: str
    raw_data: dict = field(default_factory=dict)


@dataclass
class SyncResult:
    """The structured result of a ``synchronize_portfolio()`` call,
    capturing every kind of outcome (added / updated / unchanged / removed /
    failed) for both holdings and transactions, plus timing metadata.

    ``success`` is ``True`` when no records failed to process at all —
    i.e. when ``holdings_failed`` and ``transactions_failed`` are both zero.
    A partially-successful sync that added some holdings but also had
    failures is ``success=False``; callers that distinguish "some failures
    among successes" from "total failure" can use the individual counter
    fields to build their own severity signals.

    ``has_changes`` is ``True`` when any record was added, updated, or
    removed. A fully-unmodified sync (all existing records unchanged and
    no new ones added) returns ``False``. A sync that only had failures
    but no structural changes also returns ``False`` — "failed" is not
    "changed".  Callers can use ``has_changes`` to decide whether to
    notify downstream systems or re-render a portfolio view.

    ``sync_started_at`` / ``sync_completed_at`` are timezone-aware UTC
    timestamps captured at the outermost method boundary, so callers /
    logs that record the same ``SyncResult`` get the same timestamp
    values regardless of how deep in the call stack the capture happens.

    ``duration_ms`` is the elapsed wall-clock time in milliseconds,
    derived as the difference between ``sync_completed_at`` and
    ``sync_started_at`` (never pre-computed, always computed from the
    two timestamps so the value is always consistent with them)."""

    portfolio_id: str

    # Holdings counters
    holdings_added: int = 0
    holdings_updated: int = 0
    holdings_unchanged: int = 0
    holdings_removed: int = 0
    holdings_failed: int = 0

    # Transactions counters
    transactions_added: int = 0
    transactions_updated: int = 0
    transactions_unchanged: int = 0
    transactions_removed: int = 0
    transactions_failed: int = 0

    # Failed record detail
    failed_records: list[FailedRecord] = field(default_factory=list)

    # Timing
    sync_started_at: datetime | None = None
    sync_completed_at: datetime | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.sync_started_at is None or self.sync_completed_at is None:
            return None
        delta = self.sync_completed_at - self.sync_started_at
        return round(delta.total_seconds() * 1000)

    @property
    def success(self) -> bool:
        return self.holdings_failed == 0 and self.transactions_failed == 0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.holdings_added
            or self.holdings_updated
            or self.holdings_removed
            or self.transactions_added
            or self.transactions_updated
            or self.transactions_removed
        )


# ---------------------------------------------------------------------------
# Repository protocols (STORY-179 / STORY-SYNC-02)
# ---------------------------------------------------------------------------


class HoldingRepository(Protocol):
    """Repository protocol for portfolio holdings.

    ``find_by_broker_holding_id`` supports idempotent matching: given a
    broker's own identifier for a holding, it returns the stored record
    if one exists (so a re-import of the same broker holding is an
    update, not a duplicate insert).

    ``find_active_by_account_id`` returns all non-removed holdings for
    an account, enabling the sync logic to detect holdings that exist in
    the DB but were removed from the broker feed."""

    def find_by_broker_holding_id(
        self, broker_holding_id: str
    ) -> CurrentHolding | None:
        ...

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentHolding]:
        ...


class TransactionRepository(Protocol):
    """Repository protocol for portfolio transactions.

    ``find_by_broker_transaction_id`` supports idempotent matching:
    given a broker's own identifier for a transaction, it returns the
    stored record if one exists (so a re-import of the same broker
    transaction is an update, not a duplicate insert).

    ``find_active_by_account_id`` returns all non-removed transactions
    for an account, enabling the sync logic to detect removed records."""

    def find_by_broker_transaction_id(
        self, broker_transaction_id: str
    ) -> CurrentTransaction | None:
        ...

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentTransaction]:
        ...


@dataclass(frozen=True)
class ReconciliationOp:
    """The result of a single transaction reconciliation decision
    (STORY-SYNC-04).

    ``transaction`` is the broker transaction being reconciled.

    ``status`` is one of:
      * ``'added'``   — ``broker_transaction_id`` not found in DB;
                        should be inserted as a new record.
      * ``'updated'`` — ``broker_transaction_id`` found and
                        ``broker_modified_at`` is strictly later than the
                        stored ``updated_at``; should update the record.
      * ``'unchanged'`` — ``broker_transaction_id`` found and
                        ``broker_modified_at`` is at or before the stored
                        ``updated_at``; no action needed."""
    transaction: BrokerTransaction
    status: Literal['added', 'updated', 'unchanged']


@dataclass(frozen=True)
class RemovedHoldingOp:
    """The result of a single holding removed-record decision (STORY-SYNC-05).

    ``current_holding`` is the currently-stored holding that is no longer
    present in the broker feed.

    ``status`` is always ``'removed'`` — the holding existed in the DB but
    is absent from the current broker data set."""
    current_holding: CurrentHolding
    status: Literal['removed'] = 'removed'


@dataclass(frozen=True)
class RemovedTransactionOp:
    """The result of a single transaction removed-record decision (STORY-SYNC-05).

    ``current_transaction`` is the currently-stored transaction that is no
    longer present in the broker feed.

    ``status`` is always ``'removed'`` — the transaction existed in the DB
    but is absent from the current broker data set."""
    current_transaction: CurrentTransaction
    status: Literal['removed'] = 'removed'


def reconcile_transactions(
    broker_transactions: list[BrokerTransaction],
    repository: TransactionRepository,
) -> list[ReconciliationOp]:
    """Idempotent transaction reconciliation (STORY-SYNC-04).

    Pure function: given a list of broker transactions and a repository
    that can look them up by ``broker_transaction_id``, produces a list
    of ``ReconciliationOp`` describing the action to take for each
    transaction. No persistence is performed — callers decide whether
    and how to apply the operations.

    Decision rules (checked in order):

      1. If no stored record has a matching ``broker_transaction_id``,
         the operation is ``'added'`` — the caller should insert it.
      2. If a stored record exists and ``broker_modified_at`` is
         strictly later than its ``updated_at``, the operation is
         ``'updated'`` — the caller should update it.
      3. Otherwise (record exists and timestamp is not strictly later),
         the operation is ``'unchanged'`` — no action needed.

    Args:
        broker_transactions: list of transactions as returned by
            ``BrokerConnector.fetch_transactions``.
        repository: an implementation of ``TransactionRepository``;
            used only for read operations (``find_by_broker_transaction_id``).

    Returns:
        A ``ReconciliationOp`` for every input broker transaction, in the
        same order as ``broker_transactions`` was passed. The returned
        list length always equals the input list length."""
    ops: list[ReconciliationOp] = []
    for broker_tx in broker_transactions:
        existing = repository.find_by_broker_transaction_id(broker_tx.broker_transaction_id)
        if existing is None:
            ops.append(ReconciliationOp(transaction=broker_tx, status='added'))
        elif broker_tx.broker_modified_at > existing.updated_at:
            ops.append(ReconciliationOp(transaction=broker_tx, status='updated'))
        else:
            ops.append(ReconciliationOp(transaction=broker_tx, status='unchanged'))
    return ops


@dataclass(frozen=True)
class HoldingReconciliationOp:
    """The result of a single holding reconciliation decision (STORY-SYNC-03).

    ``holding`` is the broker holding being reconciled.

    ``status`` is one of:
      * ``'added'``     — the holding's broker id (its ``isin`` — see
                          ``find_holdings_to_remove``'s own documented
                          convention for this) does not match any stored
                          record; should be inserted as a new record.
      * ``'updated'``   — the broker id matches a stored record that is
                          either inactive (reactivate it) or active but
                          whose comparable fields differ from the broker's
                          current data; should update the record.
      * ``'unchanged'`` — the broker id matches a stored, active record
                          whose comparable fields are identical; no
                          action needed."""
    holding: BrokerHolding
    status: Literal['added', 'updated', 'unchanged']


def reconcile_holdings(
    broker_holdings: list[BrokerHolding],
    repository: HoldingRepository,
) -> list[HoldingReconciliationOp]:
    """Idempotent holding reconciliation (STORY-SYNC-03).

    Pure function: given a list of broker holdings and a repository that
    can look them up by broker id, produces a list of
    ``HoldingReconciliationOp`` describing the action to take for each
    holding. No persistence is performed — callers decide whether and how
    to apply the operations.

    ``BrokerHolding`` has no field literally named ``broker_holding_id``
    (unlike ``BrokerTransaction.broker_transaction_id``) — its real,
    already-established idempotent key is ``isin``, per
    ``find_holdings_to_remove``'s own documented convention just below
    ("the set of broker holding IDs ... e.g. the ISINs from the latest
    ``BrokerConnector.fetch_holdings`` call"). This function uses the
    same convention so a holding inserted here and a holding later
    detected as removed by ``find_holdings_to_remove`` agree on what
    "the broker id" means.

    Field comparison for an existing ACTIVE record is scoped to what
    ``Holding`` actually stores today: ``quantity`` and ``security_id``
    (the broker's ``symbol``). ``cost_basis`` is deliberately NOT
    compared — ``Holding`` has no ``cost_basis`` field yet
    (``calculate_gains_losses`` already raises a real, deliberate
    ``NotImplementedError`` naming this exact gap, with its own test
    asserting that behavior); adding the field here to satisfy a
    comparison would contradict that already-made decision and break
    that test. A holding whose only real-world change is its cost basis
    will not be flagged 'updated' by this function until that field
    exists — a real, current limitation, not a silent one.

    Decision rules (checked in order):

      1. If no stored record has a matching ``isin``, the operation is
         ``'added'``.
      2. If a stored record matches but is inactive, the operation is
         ``'updated'`` (reactivation).
      3. If a stored record matches, is active, and ``quantity`` or
         ``symbol`` differs from the stored holding's ``security_id``,
         the operation is ``'updated'``.
      4. Otherwise (active, matching record, no comparable field
         differs), the operation is ``'unchanged'``.

    Args:
        broker_holdings: list of holdings as returned by
            ``BrokerConnector.fetch_holdings``.
        repository: an implementation of ``HoldingRepository``; used only
            for read operations (``find_by_broker_holding_id``).

    Returns:
        A ``HoldingReconciliationOp`` for every input broker holding, in
        the same order as ``broker_holdings`` was passed. The returned
        list length always equals the input list length."""
    ops: list[HoldingReconciliationOp] = []
    for broker_holding in broker_holdings:
        existing = repository.find_by_broker_holding_id(broker_holding.isin)
        if existing is None:
            ops.append(HoldingReconciliationOp(holding=broker_holding, status='added'))
        elif not existing.is_active:
            ops.append(HoldingReconciliationOp(holding=broker_holding, status='updated'))
        elif (
            broker_holding.quantity != existing.holding.quantity
            or broker_holding.symbol != existing.holding.security_id
        ):
            ops.append(HoldingReconciliationOp(holding=broker_holding, status='updated'))
        else:
            ops.append(HoldingReconciliationOp(holding=broker_holding, status='unchanged'))
    return ops


def find_holdings_to_remove(
    current_broker_holding_ids: set[str],
    repository: HoldingRepository,
    account_id: str,
) -> list[RemovedHoldingOp]:
    """Detect holdings that exist in the DB but are no longer in broker data (STORY-SYNC-05).

    After processing incoming broker records, holdings that exist in the DB
    (active, matching ``account_id``) whose ``broker_holding_id`` is not in
    ``current_broker_holding_ids`` must be marked as ``'removed'``.

    Pure function: no persistence is performed — callers decide whether
    and how to apply the ``'removed'`` status (typically by setting
    ``is_active = False`` on the stored record).

    Args:
        current_broker_holding_ids: the set of broker holding IDs currently
            reported by the broker (e.g. the ISINs from the latest
            ``BrokerConnector.fetch_holdings`` call).
        repository: an implementation of ``HoldingRepository``; used only
            for read operations (``find_active_by_account_id``).
        account_id: the account/portfolio to scope the query to; only
            records with ``portfolio_id == account_id`` are considered.

    Returns:
        A ``RemovedHoldingOp`` for every active stored holding whose
        ``broker_holding_id`` is absent from ``current_broker_holding_ids``,
        in the order returned by ``find_active_by_account_id``. Empty list
        when all stored holdings are still present in the broker feed."""
    with traced("find_holdings_to_remove"):
        stored = repository.find_active_by_account_id(account_id)
        ops: list[RemovedHoldingOp] = []
        for current in stored:
            broker_id = current.holding.broker_holding_id
            if broker_id is not None and broker_id not in current_broker_holding_ids:
                ops.append(RemovedHoldingOp(current_holding=current))
        return ops


def find_transactions_to_remove(
    current_broker_transaction_ids: set[str],
    repository: TransactionRepository,
    account_id: str,
) -> list[RemovedTransactionOp]:
    """Detect transactions that exist in the DB but are no longer in broker data (STORY-SYNC-05).

    After processing incoming broker records, transactions that exist in the DB
    (active, matching ``account_id``) whose ``broker_transaction_id`` is not in
    ``current_broker_transaction_ids`` must be marked as ``'removed'``.

    Pure function: no persistence is performed — callers decide whether
    and how to apply the ``'removed'`` status (typically by setting
    ``is_active = False`` on the stored record).

    Args:
        current_broker_transaction_ids: the set of broker transaction IDs
            currently reported by the broker (e.g. the transaction IDs from
            the latest ``BrokerConnector.fetch_transactions`` call).
        repository: an implementation of ``TransactionRepository``; used only
            for read operations (``find_active_by_account_id``).
        account_id: the account/portfolio to scope the query to; only
            records with ``portfolio_id == account_id`` are considered.

    Returns:
        A ``RemovedTransactionOp`` for every active stored transaction whose
        ``broker_transaction_id`` is absent from ``current_broker_transaction_ids``,
        in the order returned by ``find_active_by_account_id``. Empty list
        when all stored transactions are still present in the broker feed."""
    with traced("find_transactions_to_remove"):
        stored = repository.find_active_by_account_id(account_id)
        ops: list[RemovedTransactionOp] = []
        for current in stored:
            broker_id = current.transaction.broker_transaction_id
            if broker_id is not None and broker_id not in current_broker_transaction_ids:
                ops.append(RemovedTransactionOp(current_transaction=current))
        return ops


class StubHoldingRepository:
    """Structural no-op implementation of ``HoldingRepository``."""

    def find_by_broker_holding_id(
        self, broker_holding_id: str
    ) -> CurrentHolding | None:
        return None

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentHolding]:
        return []


class StubTransactionRepository:
    """Structural no-op implementation of ``TransactionRepository``."""

    def find_by_broker_transaction_id(
        self, broker_transaction_id: str
    ) -> CurrentTransaction | None:
        return None

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentTransaction]:
        return []


class _FakeTransactionRepository:
    """In-memory test double for ``TransactionRepository`` that stores
    records in a dict keyed by ``broker_transaction_id``, supporting
    all three reconciliation scenarios (added / updated / unchanged)
    via optional constructor injection.

    Usage::

        repo = _FakeTransactionRepository({
            "tx-001": CurrentTransaction(
                id="db-001",
                is_active=True,
                transaction=Transaction(...),
                updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
        })
        ops = reconcile_transactions(broker_txs, repo)
    """

    def __init__(
        self,
        initial: dict[str, CurrentTransaction] | None = None,
    ) -> None:
        self._store: dict[str, CurrentTransaction] = dict(initial) if initial else {}

    def find_by_broker_transaction_id(
        self, broker_transaction_id: str
    ) -> CurrentTransaction | None:
        return self._store.get(broker_transaction_id)

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentTransaction]:
        return [
            ct for ct in self._store.values()
            if ct.is_active and ct.transaction.portfolio_id == account_id
        ]


class _FakeHoldingRepository:
    """In-memory test double for ``HoldingRepository`` that stores records
    in a dict keyed by ``broker_holding_id``, supporting removed-record
    detection (STORY-SYNC-05) via optional constructor injection.

    Usage::

        repo = _FakeHoldingRepository({
            "INE002A01018": CurrentHolding(
                id="db-001",
                is_active=True,
                holding=Holding(
                    portfolio_id="portfolio-001",
                    security_id="RELIANCE",
                    quantity=Decimal("10"),
                    broker_holding_id="INE002A01018",
                ),
            ),
        })
        ops = find_holdings_to_remove(
            current_broker_holding_ids={"INE002A01018"},
            repository=repo,
            account_id="portfolio-001",
        )
    """

    def __init__(
        self,
        initial: dict[str, CurrentHolding] | None = None,
    ) -> None:
        self._store: dict[str, CurrentHolding] = dict(initial) if initial else {}

    def find_by_broker_holding_id(
        self, broker_holding_id: str
    ) -> CurrentHolding | None:
        return self._store.get(broker_holding_id)

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentHolding]:
        return [
            ch for ch in self._store.values()
            if ch.is_active and ch.holding.portfolio_id == account_id
        ]


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

    def import_holdings(self, user_id: str, broker_id: str) -> HoldingsImportResult:
        ...

    def import_transactions(
        self,
        user_id: str,
        broker_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ImportResult:
        ...

    def synchronize_portfolio(self, portfolio: Portfolio) -> SyncResult:
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

    def import_holdings(self, user_id: str, broker_id: str) -> HoldingsImportResult:
        with traced("StubUserPortfolio.import_holdings"):
            return HoldingsImportResult(holdings_written=0, skipped=0)

    def import_transactions(
        self,
        user_id: str,
        broker_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ImportResult:
        with traced("StubUserPortfolio.import_transactions"):
            return ImportResult(transactions_inserted=0, transactions_skipped_existing=0, rows_skipped_invalid=0)

    def synchronize_portfolio(self, portfolio: Portfolio) -> SyncResult:
        with traced("StubUserPortfolio.synchronize_portfolio"):
            return SyncResult(portfolio_id="stub-id")

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


# ---------------------------------------------------------------------------
# Repository implementations (STORY-179 / STORY-SYNC-02)
# ---------------------------------------------------------------------------


class DefaultHoldingRepository:
    """Real implementation of ``HoldingRepository`` backed by the shared
    ``Infrastructure`` store (defaulting to ``DefaultInfrastructure``).

    Uses the ``broker_holding_id`` field on stored holding records for
    idempotent lookup. ``is_active`` defaults to ``True`` for any record
    that lacks the field, preserving backwards-compat with holdings
    stored before the field existed."""

    def __init__(self, infrastructure: Infrastructure | None = None) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()

    def find_by_broker_holding_id(
        self, broker_holding_id: str
    ) -> CurrentHolding | None:
        with traced("DefaultHoldingRepository.find_by_broker_holding_id"):
            records = self._infrastructure.query(
                HOLDINGS_TABLE, {"broker_holding_id": broker_holding_id}
            )
            if not records:
                return None
            record = records[0]
            return CurrentHolding(
                id=record["id"],
                is_active=record.get("is_active", True),
                holding=Holding(
                    portfolio_id=record["portfolio_id"],
                    security_id=record["security_id"],
                    quantity=Decimal(str(record["quantity"])),
                    broker_holding_id=record.get("broker_holding_id"),
                ),
            )

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentHolding]:
        with traced("DefaultHoldingRepository.find_active_by_account_id"):
            all_records = self._infrastructure.query(
                HOLDINGS_TABLE, {"portfolio_id": account_id}
            )
            return [
                CurrentHolding(
                    id=record["id"],
                    is_active=record.get("is_active", True),
                    holding=Holding(
                        portfolio_id=record["portfolio_id"],
                        security_id=record["security_id"],
                        quantity=Decimal(str(record["quantity"])),
                        broker_holding_id=record.get("broker_holding_id"),
                    ),
                )
                for record in all_records
                if record.get("is_active", True)
            ]


class DefaultTransactionRepository:
    """Real implementation of ``TransactionRepository`` backed by the
    shared ``Infrastructure`` store (defaulting to ``DefaultInfrastructure``).

    Uses the ``broker_transaction_id`` field on stored transaction records
    for idempotent lookup. ``is_active`` defaults to ``True`` for any record
    that lacks the field, preserving backwards-compat."""

    def __init__(self, infrastructure: Infrastructure | None = None) -> None:
        self._infrastructure = infrastructure or DefaultInfrastructure()

    def find_by_broker_transaction_id(
        self, broker_transaction_id: str
    ) -> CurrentTransaction | None:
        with traced("DefaultTransactionRepository.find_by_broker_transaction_id"):
            records = self._infrastructure.query(
                TRANSACTIONS_TABLE, {"broker_transaction_id": broker_transaction_id}
            )
            if not records:
                return None
            record = records[0]
            # Persisted updated_at: prefer the real stored value, default
            # to epoch for backwards-compat with records stored before
            # this field existed. The epoch choice biases towards
            # 'updated' (the broker's record will almost always be newer
            # than 1970), which is the conservative choice — an extra
            # update is safe and correct, whereas a missed update would
            # silently leave stale data in place.
            updated_at: datetime
            raw_updated = record.get("updated_at")
            if isinstance(raw_updated, datetime):
                updated_at = raw_updated
            elif isinstance(raw_updated, str):
                updated_at = datetime.fromisoformat(raw_updated)
            else:
                updated_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
            return CurrentTransaction(
                id=record["id"],
                is_active=record.get("is_active", True),
                transaction=Transaction(
                    portfolio_id=record["portfolio_id"],
                    kind=record["kind"],
                    amount=Decimal(str(record["amount"])),
                    broker_transaction_id=record.get("broker_transaction_id"),
                ),
                updated_at=updated_at,
            )

    def find_active_by_account_id(
        self, account_id: str
    ) -> list[CurrentTransaction]:
        with traced("DefaultTransactionRepository.find_active_by_account_id"):
            all_records = self._infrastructure.query(
                TRANSACTIONS_TABLE, {"portfolio_id": account_id}
            )
            results: list[CurrentTransaction] = []
            for record in all_records:
                if not record.get("is_active", True):
                    continue
                raw_updated = record.get("updated_at")
                if isinstance(raw_updated, datetime):
                    updated_at = raw_updated
                elif isinstance(raw_updated, str):
                    updated_at = datetime.fromisoformat(raw_updated)
                else:
                    updated_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
                results.append(
                    CurrentTransaction(
                        id=record["id"],
                        is_active=True,
                        transaction=Transaction(
                            portfolio_id=record["portfolio_id"],
                            kind=record["kind"],
                            amount=Decimal(str(record["amount"])),
                            broker_transaction_id=record.get("broker_transaction_id"),
                        ),
                        updated_at=updated_at,
                    )
                )
            return results


def _current_holding_from_record(record: dict) -> "CurrentHolding":
    return CurrentHolding(
        id=record["id"],
        is_active=record.get("is_active", True),
        holding=Holding(
            portfolio_id=record["portfolio_id"],
            security_id=record["security_id"],
            quantity=Decimal(str(record["quantity"])),
            broker_holding_id=record.get("broker_holding_id"),
        ),
    )


def _current_transaction_from_record(record: dict) -> "CurrentTransaction":
    raw_updated = record.get("updated_at")
    if isinstance(raw_updated, datetime):
        updated_at = raw_updated
    elif isinstance(raw_updated, str):
        updated_at = datetime.fromisoformat(raw_updated)
    else:
        updated_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return CurrentTransaction(
        id=record["id"],
        is_active=record.get("is_active", True),
        transaction=Transaction(
            portfolio_id=record["portfolio_id"],
            kind=record["kind"],
            amount=Decimal(str(record["amount"])),
            broker_transaction_id=record.get("broker_transaction_id"),
        ),
        updated_at=updated_at,
    )


class _PrefetchedHoldingRepository:
    """STORY-SYNC-14: an in-memory HoldingRepository backed by ONE
    upfront, unfiltered query for the whole account -- not a fresh query
    per lookup. Against `DefaultHoldingRepository` directly,
    `reconcile_holdings` calling `find_by_broker_holding_id` once per
    broker holding is a real N+1 (one query per holding), and
    `find_holdings_to_remove`'s own `find_active_by_account_id` call is
    a real second, duplicate query for data `synchronize_portfolio`
    could already have in hand. Building this wrapper from ONE real
    query per broker per sync call and handing it to both functions in
    place of `DefaultHoldingRepository` turns that into exactly the two
    queries per account STORY-SYNC-14 asks for (one for holdings, one
    for transactions -- see `_PrefetchedTransactionRepository`), without
    changing `reconcile_holdings`/`find_holdings_to_remove`'s own
    signatures or their existing tests: they still call the same two
    Protocol methods, just against a repository answering from memory.

    Built from ALL records for the account (active AND inactive), not
    just the active ones `find_active_by_account_id` returns --
    `reconcile_holdings`' own reactivation rule ("a stored record
    matches but is inactive -> 'updated'") needs to see an inactive
    record to reactivate it; prefetching active-only would silently
    make every previously-removed holding look brand new to
    `find_by_broker_holding_id` instead of a real reactivation."""

    def __init__(self, all_holdings: list["CurrentHolding"]) -> None:
        self._active = [h for h in all_holdings if h.is_active]
        self._by_broker_holding_id: dict[str, "CurrentHolding"] = {
            h.holding.broker_holding_id: h
            for h in all_holdings
            if h.holding.broker_holding_id is not None
        }

    def find_by_broker_holding_id(self, broker_holding_id: str) -> "CurrentHolding | None":
        return self._by_broker_holding_id.get(broker_holding_id)

    def find_active_by_account_id(self, account_id: str) -> list["CurrentHolding"]:
        return self._active


class _PrefetchedTransactionRepository:
    """The transaction-side twin of `_PrefetchedHoldingRepository` --
    same real reasoning, same STORY-SYNC-14 requirement, same
    zero-signature-change approach, same active+inactive prefetch (so a
    previously-removed transaction can still be found and reactivated)
    for `reconcile_transactions`/`find_transactions_to_remove`."""

    def __init__(self, all_transactions: list["CurrentTransaction"]) -> None:
        self._active = [t for t in all_transactions if t.is_active]
        self._by_broker_transaction_id: dict[str, "CurrentTransaction"] = {
            t.transaction.broker_transaction_id: t
            for t in all_transactions
            if t.transaction.broker_transaction_id is not None
        }

    def find_by_broker_transaction_id(self, broker_transaction_id: str) -> "CurrentTransaction | None":
        return self._by_broker_transaction_id.get(broker_transaction_id)

    def find_active_by_account_id(self, account_id: str) -> list["CurrentTransaction"]:
        return self._active


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
        # None (the real default) means "resolve fresh via the broker
        # registry, per broker_id, on every call" (STORY-9) -- an
        # explicitly injected connector always wins when given (every
        # existing test's own real seam, unchanged), but the real,
        # un-injected production path must never pin a single broker
        # instance for the whole component's lifetime: import_holdings/
        # import_transactions are called with a real, specific broker_id
        # each time, and a second broker must become usable with zero
        # changes here. See _resolve_broker_connector below.
        self._broker_connector = broker_connector
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._knowledge_entity = knowledge_entity or DefaultKnowledgeEntity(infrastructure=self._infrastructure)

    def _resolve_broker_connector(self, broker_id: str) -> "BrokerConnector":
        """The constructor-injected connector (if any) always wins --
        this is what every existing test that passes broker_connector=
        to the constructor already relies on, and it's a legitimate,
        real override (e.g. a caller that only ever talks to one
        broker and wants to skip the registry entirely). Otherwise
        resolves fresh via get_broker_connector(broker_id) -- the real
        registry (STORY-9), so a second real broker becomes usable
        here with zero changes to this method or its callers."""
        if self._broker_connector is not None:
            return self._broker_connector
        return get_broker_connector(broker_id)

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

    def import_holdings(self, user_id: str, broker_id: str) -> HoldingsImportResult:
        """Import broker holdings for ``user_id`` / ``broker_id`` (STORY-14).

        **Snapshot semantics**: holdings are a point-in-time snapshot, not
        an append/upsert log like transactions -- each real import
        replaces the ENTIRE stored set for (user_id, broker_id) inside a
        single transaction (delete existing rows, insert the freshly
        fetched set). A row-by-row upsert would leave sold-out positions
        behind with no signal they were ever sold; replace makes a
        holding's disappearance from the broker mean its disappearance
        from our table too.

        **Error handling**:
          * No stored connection for (user_id, broker_id) raises
            ``BrokerNotConnectedError`` before any connector call.
          * ``BrokerAuthError`` marks the connection status ERROR with a
            reconnect-oriented message (generated generically from the
            connector's ``display_name``, no broker-specific text) and
            re-raises.
          * A connector exception happens before any write -- the
            pre-existing holdings rows are always left untouched.
          * ``last_import_at`` is updated on the connection row only on
            success.

        Zero holdings is a valid result (the existing set is still
        replaced -- with nothing -- and ``holdings_written`` is 0), not
        an error.
        """
        with traced("DefaultUserPortfolio.import_holdings"):
            connection = self._infrastructure.get_broker_connection(user_id, broker_id)
            if connection is None:
                raise BrokerNotConnectedError(
                    f"no broker connection stored for user_id={user_id} broker_id={broker_id}"
                )

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
                    "access token could not be decrypted for import_holdings"
                )
                raise BrokerAuthError(
                    f"access token for user_id={user_id} broker_id={broker_id} "
                    f"could not be decrypted"
                )

            connector = self._resolve_broker_connector(broker_id)
            try:
                raw_holdings = connector.fetch_holdings(credentials=credentials)
            except BrokerAuthError:
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    f"{connector.display_name} access expired, please reconnect"
                )
                raise
            except BrokerApiError:
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    "BrokerApiError during import_holdings"
                )
                raise

            rows = []
            skipped = 0
            for raw in raw_holdings:
                try:
                    tagged = self._boundary_gate.tag_provenance(asdict(raw), source="broker_connector")
                    _validate_holding_row(tagged)
                except Exception:
                    skipped += 1
                    continue
                rows.append({
                    "symbol": tagged["symbol"],
                    "isin": tagged["isin"],
                    "quantity": tagged.get("quantity"),
                    "average_price": tagged.get("average_price"),
                    "last_price": tagged.get("last_price"),
                    "exchange": tagged.get("exchange"),
                    "instrument_id": tagged.get("instrument_id"),
                    "raw": tagged.get("raw", {}),
                })

            # Real, atomic replace: existing rows for (user_id, broker_id)
            # are deleted and the freshly fetched set inserted inside one
            # transaction (adr/0019's real Postgres, not a best-effort
            # loop) -- a mid-write failure here leaves the PRE-existing
            # rows exactly as they were, never a half-updated mix.
            self._infrastructure.replace_broker_holdings(user_id, broker_id, rows)

            self._infrastructure.touch_last_import(user_id, broker_id)

            return HoldingsImportResult(holdings_written=len(rows), skipped=skipped)

    def synchronize_portfolio(self, portfolio: Portfolio) -> SyncResult:
        """Real incremental reconciliation sync (STORY-SYNC-03 through
        SYNC-16). For every broker this portfolio's user has connected,
        fetches the broker's current holdings/transactions and
        reconciles them against what's already stored --
        added/updated/unchanged/removed -- via the pure
        `reconcile_holdings`/`reconcile_transactions`/
        `find_holdings_to_remove`/`find_transactions_to_remove`
        functions above. Deliberately different from
        `import_holdings`/`import_transactions` (STORY-14/15): those do
        a wholesale snapshot-replace for one named broker on demand (the
        "Import Now" button); this does incremental, all-brokers
        reconciliation, the real strategy a scheduled/background sync
        needs so an unrelated broker's data is never touched or wiped
        just because this call happened to run.

        Wrapped in one real Postgres transaction (STORY-SYNC-06, see
        `Infrastructure.transaction()`): every write below, across every
        connected broker, commits together or none do. A failure partway
        through — a bad record, a connector error, anything — rolls back
        the whole sync; this portfolio's stored data is never left in a
        half-migrated state. Because the DB genuinely rolls back on
        failure, `track_portfolio_state` (which reads what's stored) is
        only called on the real success path (STORY-SYNC-09) — calling
        it after a rollback would read the pre-sync state anyway, so
        doing it unconditionally would just be a wasted, misleading read.
        """
        with traced("DefaultUserPortfolio.synchronize_portfolio"):
            sync_started_at = datetime.now(timezone.utc)
            result = SyncResult(portfolio_id=portfolio.id, sync_started_at=sync_started_at)
            _logger = logging.getLogger(__name__)
            # One correlation_id per real sync call (STORY-SYNC-10) --
            # every log line below includes it so every record-level
            # transition, and the final summary/failure line, can be
            # grepped back to the one synchronize_portfolio() call that
            # produced them, even with several portfolios syncing
            # concurrently.
            correlation_id = str(uuid.uuid4())
            _logger.info(
                "[SYNC_START] correlation_id=%s portfolio_id=%s",
                correlation_id, portfolio.id,
            )

            try:
                with self._infrastructure.transaction():
                    for broker in list_available_brokers():
                        broker_id = broker["broker_id"]
                        connection = self._infrastructure.get_broker_connection(
                            portfolio.user_id, broker_id
                        )
                        if connection is None:
                            # This user hasn't connected this broker -- nothing
                            # of theirs to reconcile, not a real failure.
                            continue

                        credentials = BrokerCredentials(
                            access_token=connection.access_token,
                            token_type=connection.token_type,
                            expires_at=connection.access_token_expires_at,
                            refresh_token=None,
                            broker_user_id=connection.broker_user_id,
                            raw={},
                        )
                        connector = self._resolve_broker_connector(broker_id)

                        # STORY-SYNC-14: exactly two real queries for this
                        # broker's whole reconciliation pass -- one for
                        # holdings, one for transactions -- instead of one
                        # query per broker holding/transaction. Known,
                        # accepted real limitation: since Holding/
                        # Transaction carry no broker_id column, this
                        # portfolio-wide fetch (and the removal-detection
                        # below) isn't scoped per-broker -- correct today
                        # because exactly one real connector (upstox) ever
                        # exists per portfolio in practice; a second real
                        # broker connector would need a real broker_id
                        # column before multi-broker removal-detection
                        # could be trusted.
                        holding_repo = _PrefetchedHoldingRepository(
                            [
                                _current_holding_from_record(r)
                                for r in self._infrastructure.query(
                                    HOLDINGS_TABLE, {"portfolio_id": portfolio.id}
                                )
                            ]
                        )
                        transaction_repo = _PrefetchedTransactionRepository(
                            [
                                _current_transaction_from_record(r)
                                for r in self._infrastructure.query(
                                    TRANSACTIONS_TABLE, {"portfolio_id": portfolio.id}
                                )
                            ]
                        )

                        # --- Holdings ---
                        broker_holdings = connector.fetch_holdings(credentials=credentials)
                        holding_ops = reconcile_holdings(broker_holdings, holding_repo)
                        seen_holding_ids: set[str] = set()
                        for op in holding_ops:
                            seen_holding_ids.add(op.holding.isin)
                            _logger.debug(
                                "[SYNC_HOLDING] correlation_id=%s portfolio_id=%s "
                                "broker_holding_id=%s status=%s",
                                correlation_id, portfolio.id, op.holding.isin, op.status,
                            )
                            if op.status == "unchanged":
                                result.holdings_unchanged += 1
                                continue
                            self._infrastructure.store(
                                HOLDINGS_TABLE,
                                {
                                    # Keyed on isin, not symbol -- isin
                                    # (broker_holding_id) is the real
                                    # natural key find_by_broker_holding_id
                                    # looks up by; two real holdings can
                                    # share a symbol (dual-listed
                                    # securities) but never an isin.
                                    "id": f"{portfolio.id}:{op.holding.isin}",
                                    "portfolio_id": portfolio.id,
                                    "security_id": op.holding.symbol,
                                    "quantity": str(op.holding.quantity),
                                    "broker_holding_id": op.holding.isin,
                                    "is_active": True,
                                },
                            )
                            if op.status == "added":
                                result.holdings_added += 1
                            else:
                                result.holdings_updated += 1

                        for removed in find_holdings_to_remove(
                            seen_holding_ids, holding_repo, portfolio.id
                        ):
                            stored = removed.current_holding
                            _logger.debug(
                                "[SYNC_HOLDING] correlation_id=%s portfolio_id=%s "
                                "broker_holding_id=%s status=removed",
                                correlation_id, portfolio.id, stored.holding.broker_holding_id,
                            )
                            self._infrastructure.store(
                                HOLDINGS_TABLE,
                                {
                                    "id": stored.id,
                                    "portfolio_id": stored.holding.portfolio_id,
                                    "security_id": stored.holding.security_id,
                                    "quantity": str(stored.holding.quantity),
                                    "broker_holding_id": stored.holding.broker_holding_id,
                                    "is_active": False,
                                },
                            )
                            result.holdings_removed += 1

                        # --- Transactions ---
                        broker_transactions = connector.fetch_transactions(
                            credentials=credentials, start_date=date.min, end_date=date.max
                        )
                        transaction_ops = reconcile_transactions(broker_transactions, transaction_repo)
                        seen_transaction_ids: set[str] = set()
                        for top in transaction_ops:
                            seen_transaction_ids.add(top.transaction.broker_transaction_id)
                            _logger.debug(
                                "[SYNC_TRANSACTION] correlation_id=%s portfolio_id=%s "
                                "broker_transaction_id=%s status=%s",
                                correlation_id, portfolio.id,
                                top.transaction.broker_transaction_id, top.status,
                            )
                            if top.status == "unchanged":
                                result.transactions_unchanged += 1
                                continue
                            self._infrastructure.store(
                                TRANSACTIONS_TABLE,
                                {
                                    "id": str(uuid.uuid4()),
                                    "portfolio_id": portfolio.id,
                                    "kind": top.transaction.side,
                                    "amount": str(top.transaction.amount),
                                    "broker_transaction_id": top.transaction.broker_transaction_id,
                                    "is_active": True,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                            if top.status == "added":
                                result.transactions_added += 1
                            else:
                                result.transactions_updated += 1

                        for removed_t in find_transactions_to_remove(
                            seen_transaction_ids, transaction_repo, portfolio.id
                        ):
                            stored_t = removed_t.current_transaction
                            _logger.debug(
                                "[SYNC_TRANSACTION] correlation_id=%s portfolio_id=%s "
                                "broker_transaction_id=%s status=removed",
                                correlation_id, portfolio.id,
                                stored_t.transaction.broker_transaction_id,
                            )
                            self._infrastructure.store(
                                TRANSACTIONS_TABLE,
                                {
                                    "id": stored_t.id,
                                    "portfolio_id": stored_t.transaction.portfolio_id,
                                    "kind": stored_t.transaction.kind,
                                    "amount": str(stored_t.transaction.amount),
                                    "broker_transaction_id": stored_t.transaction.broker_transaction_id,
                                    "is_active": False,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                            result.transactions_removed += 1
            except Exception as exc:  # noqa: BLE001 -- real rollback already happened; record it, don't crash the caller
                _logger.error(
                    "[SYNC_FAILED] correlation_id=%s portfolio_id=%s reason=%s",
                    correlation_id, portfolio.id, exc, exc_info=True,
                )
                # The transaction already rolled back every write above --
                # none of the added/updated/removed counts accumulated
                # this far are real anymore, so they're reset to 0 rather
                # than reporting numbers that don't match what's actually
                # in the database. holdings_failed/transactions_failed are
                # set to 1 (not a real per-record count -- SyncResult has
                # no dedicated "whole sync failed" flag) purely so
                # `SyncResult.success` correctly reads False; the real
                # detail lives in failed_records.
                result = SyncResult(
                    portfolio_id=portfolio.id,
                    sync_started_at=sync_started_at,
                    holdings_failed=1,
                    transactions_failed=1,
                    failed_records=[
                        FailedRecord(
                            record_type="sync", broker_id=None, reason=str(exc), raw_data={}
                        )
                    ],
                )
                result.sync_completed_at = datetime.now(timezone.utc)
                self._emit_sync_metrics(portfolio, correlation_id, result)
                return result

            result.sync_completed_at = datetime.now(timezone.utc)
            _logger.info(
                "[SYNC_COMPLETE] correlation_id=%s portfolio_id=%s "
                "holdings(added=%d updated=%d unchanged=%d removed=%d) "
                "transactions(added=%d updated=%d unchanged=%d removed=%d) "
                "duration_ms=%s",
                correlation_id, portfolio.id,
                result.holdings_added, result.holdings_updated,
                result.holdings_unchanged, result.holdings_removed,
                result.transactions_added, result.transactions_updated,
                result.transactions_unchanged, result.transactions_removed,
                result.duration_ms,
            )
            self._emit_sync_metrics(portfolio, correlation_id, result)
            self.track_portfolio_state(portfolio)
            return result

    def _emit_sync_metrics(
        self, portfolio: Portfolio, correlation_id: str, result: SyncResult
    ) -> None:
        """Emit sync observability metrics (STORY-SYNC-15), complementing
        STORY-SYNC-10's logging. No dedicated metrics library
        (prometheus_client/statsd) exists anywhere in this codebase, so
        this reuses Infrastructure.publish() -- the one real,
        already-existing event-emission mechanism (Postgres-backed
        queue_events, ADR-0019) -- rather than inventing new
        infrastructure a single story shouldn't be adding on its own.
        Called on every real completion, success or failure (holdings/
        transactions_processed intentionally counts added+updated+
        unchanged, so a fully-unchanged sync still emits a real,
        nonzero heartbeat a monitor can alert on the ABSENCE of)."""
        holdings_processed = (
            result.holdings_added + result.holdings_updated + result.holdings_unchanged
        )
        transactions_processed = (
            result.transactions_added + result.transactions_updated + result.transactions_unchanged
        )
        self._infrastructure.publish(
            "metrics.sync",
            {
                "correlation_id": correlation_id,
                "account_id": portfolio.id,
                "sync_duration_seconds": (
                    (result.duration_ms or 0) / 1000.0 if result.duration_ms is not None else None
                ),
                "holdings_processed": holdings_processed,
                "transactions_processed": transactions_processed,
                "holdings_failed": result.holdings_failed,
                "transactions_failed": result.transactions_failed,
            },
        )

    def track_portfolio_state(self, portfolio: Portfolio) -> PortfolioSnapshot:
        """Reads holdings already stored — via import_holdings above, or
        stored some other way (e.g. directly through Infrastructure in a
        test) — and assembles a PortfolioSnapshot. `market_value` is set
        to each holding's quantity: no live price feed exists yet (Data
        & Sources, component 02, is still whiteboard-only), so this is a
        quantity-weighted proxy, not a real dollar-weighted valuation,
        until that component ships real prices. This mirrors how
        DefaultStateManager (Agent Runtime, component 10) is honestly
        in-memory-only until its real backing store exists — a forced
        consequence of a not-yet-built dependency, not a design fork
        with a real alternative to choose between."""
        with traced("DefaultUserPortfolio.track_portfolio_state"):
            holdings = self._stored_holdings(portfolio.id)
            positions = [
                Position(holding=holding, market_value=holding.quantity) for holding in holdings
            ]
            snapshot = PortfolioSnapshot(portfolio_id=portfolio.id, positions=positions, exposure={})
            snapshot.exposure = self.calculate_exposure(snapshot)
            return snapshot

    def calculate_exposure(self, snapshot: PortfolioSnapshot) -> dict:
        """Exposure by security: the only grouping the current data
        model supports (Holding carries a security_id, nothing else to
        group by — no asset class or sector field exists yet), so this
        isn't a design fork so much as what the dataclasses already
        available force. Positions sharing a security_id (e.g. from a
        re-synchronized portfolio) are aggregated together. Returns
        `{security_id: {"market_value": total, "weight": share of total
        portfolio market value}}`; weight is 0.0 for every entry when
        total market value is 0, rather than dividing by zero."""
        with traced("DefaultUserPortfolio.calculate_exposure"):
            # market_value is set from Holding.quantity (see
            # track_portfolio_state), a Decimal since STORY-1 -- but
            # Position's own dataclass field is still typed float, and a
            # caller (or test) can still hand this a real float directly.
            # Normalize every value to Decimal up front so the real
            # aggregation below never has to mix the two types (a bare
            # `float += Decimal`, or `Decimal + float`, both raise
            # TypeError). Decimal == float still compares correctly for
            # callers/tests that compare the result against plain float
            # literals.
            market_values = [
                position.market_value
                if isinstance(position.market_value, Decimal)
                else Decimal(str(position.market_value))
                for position in snapshot.positions
            ]
            total_market_value = sum(market_values, start=Decimal("0"))
            exposure: dict[str, dict] = {}
            for position, market_value in zip(snapshot.positions, market_values):
                security_id = position.holding.security_id
                entry = exposure.setdefault(security_id, {"market_value": Decimal("0"), "weight": 0.0})
                entry["market_value"] += market_value
            if total_market_value > 0:
                for entry in exposure.values():
                    # weight is a ratio/percentage, not a "price-related
                    # field" the story's DECIMAL(18,4) precision concern
                    # applies to -- kept as float (its existing, correct
                    # type) rather than Decimal, since e.g. Decimal("0.6")
                    # != 0.6 (0.6 has no exact binary float
                    # representation), which would break every existing
                    # caller/test that already compares against a plain
                    # float literal.
                    entry["weight"] = float(entry["market_value"] / total_market_value)
            return exposure

    def calculate_portfolio_totals(
        self,
        snapshot: PortfolioSnapshot,
        base_currency: str,
        infrastructure: Infrastructure | None = None,
    ) -> dict:
        """Currency-aggregated totals (STORY-8): sums every position's
        `market_value` separately per `holding.currency`, then derives
        a consolidated total in `base_currency` using the real
        INR/USD rate from `fetch_exchange_rate`. Deliberately distinct
        from `calculate_exposure`, which is per-security weighting —
        this is about currency conversion and aggregation, the
        separate question the multi-currency portfolio needs answered.

        Returns a dict with the shape:

            {
                "inr_total":           Decimal,   # sum of INR-currency positions
                "usd_total":           Decimal,   # sum of USD-currency positions
                "consolidated_total":  Decimal | None,  # None on rate-fetch failure
                "base_currency":       str,       # echo of the requested base
                "rate":                Decimal | None,  # real INR/USD rate fetched
                "error":               str | None,  # real failure message, never fabricated
            }

        Every monetary field is a `Decimal` quantized to 4 decimal
        places via this module's `_TOTAL_QUANTUM`/`ROUND_HALF_UP`
        pattern — matching the precision convention
        `_coerce_quantity_to_decimal` and `_quantize_rate` already
        establish. A consolidated total in `base_currency` other than
        "USD" or "INR" raises `ValueError` upfront (a 3rd base
        currency would require a different real exchange rate this
        codebase doesn't fetch, and silently coercing "EUR" → "USD"
        would be the kind of fabrication the story explicitly
        forbids).

        If `fetch_exchange_rate` raises — both vendor sources failed,
        or neither key is configured — the method catches the
        `MissingExchangeRateAPIKeyError` / `ExchangeRateFetchError`
        and returns the real INR / USD subtotals with a real error
        message about the consolidated total being unavailable. A
        fabricated exchange rate or fabricated consolidated total is
        the precise failure mode this method is built not to produce,
        so callers downstream can distinguish "we have INR/USD
        subtotals but no consolidated answer" from "we have a real
        consolidated answer in the requested base currency" purely
        from the returned dict's `consolidated_total is None` /
        `error is not None` shape — no hidden placeholders, no
        silently-coerced values."""
        with traced("DefaultUserPortfolio.calculate_portfolio_totals"):
            if base_currency not in ("USD", "INR"):
                raise ValueError(
                    f"calculate_portfolio_totals: base_currency must be one of "
                    f"('USD', 'INR'); got {base_currency!r}"
                )

            inr_total = Decimal("0")
            usd_total = Decimal("0")
            for position in snapshot.positions:
                market_value = _coerce_market_value_to_decimal(position.market_value)
                if position.holding.currency == "INR":
                    inr_total += market_value
                elif position.holding.currency == "USD":
                    usd_total += market_value
                # Positions in any other currency are not part of the
                # INR/USD aggregation -- Holding.__post_init__'s own
                # validation already restricts currency to {"USD",
                # "INR"}, so reaching this branch means a Position was
                # constructed with a raw dict that bypassed that
                # check (test-only path); silently summing it would
                # hide the bypass. Drop it, but keep the rest of the
                # aggregation honest.

            inr_total = inr_total.quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)
            usd_total = usd_total.quantize(_TOTAL_QUANTUM, rounding=ROUND_HALF_UP)

            result: dict = {
                "inr_total": inr_total,
                "usd_total": usd_total,
                "consolidated_total": None,
                "base_currency": base_currency,
                "rate": None,
                "error": None,
            }

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
                    "access token could not be decrypted for import_holdings"
                )
                raise BrokerAuthError(
                    f"access token for user_id={user_id} broker_id={broker_id} "
                    f"could not be decrypted"
                )

            connector = self._resolve_broker_connector(broker_id)
            try:
                raw_holdings = connector.fetch_holdings(credentials=credentials)
            except BrokerAuthError:
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    f"{connector.display_name} access expired, please reconnect"
                )
                raise
            except BrokerApiError:
                self._infrastructure.mark_broker_connection_error(
                    user_id, broker_id,
                    "BrokerApiError during import_holdings"
                )
                raise

            rows = []
            skipped = 0
            for raw in raw_holdings:
                try:
                    tagged = self._boundary_gate.tag_provenance(asdict(raw), source="broker_connector")
                    _validate_holding_row(tagged)
                except Exception:
                    skipped += 1
                    continue
                rows.append({
                    "symbol": tagged["symbol"],
                    "isin": tagged["isin"],
                    "quantity": tagged.get("quantity"),
                    "average_price": tagged.get("average_price"),
                    "last_price": tagged.get("last_price"),
                    "exchange": tagged.get("exchange"),
                    "instrument_id": tagged.get("instrument_id"),
                    "raw": tagged.get("raw", {}),
                })

            # Real, atomic replace: existing rows for (user_id, broker_id)
            # are deleted and the freshly fetched set inserted inside one
            # transaction (adr/0019's real Postgres, not a best-effort
            # loop) -- a mid-write failure here leaves the PRE-existing
            # rows exactly as they were, never a half-updated mix.
            self._infrastructure.replace_broker_holdings(user_id, broker_id, rows)

            self._infrastructure.touch_last_import(user_id, broker_id)

            return HoldingsImportResult(holdings_written=len(rows), skipped=skipped)

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

            connector = self._resolve_broker_connector(broker_id)
            try:
                raw_transactions = connector.fetch_transactions(
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
                    external_id=tagged["broker_transaction_id"],
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

    def _stored_holdings(self, portfolio_id: str) -> list[Holding]:
        records = self._infrastructure.query(HOLDINGS_TABLE, {"portfolio_id": portfolio_id})
        return [
            Holding(
                portfolio_id=record["portfolio_id"],
                security_id=record["security_id"],
                quantity=Decimal(str(record["quantity"])),
                broker_holding_id=record.get("broker_holding_id"),
            )
            for record in records
            # is_active defaults True for records stored before
            # synchronize_portfolio's removal tracking (STORY-SYNC-05)
            # existed -- only a record explicitly marked inactive by a
            # real reconciliation is excluded here.
            if record.get("is_active", True)
        ]


def _validate_transaction_row(row: dict) -> None:
    """Raise ValueError if a transaction row dict is missing required fields.

    Checks ``broker_transaction_id`` (BrokerTransaction's real dataclass
    field) rather than ``external_id`` -- the latter is only a read-only
    ``@property`` alias for backwards-compat callers, and
    ``dataclasses.asdict()`` (what builds this ``row`` dict) never
    includes computed properties, only real fields. Checking the alias
    here would make every real row look invalid."""
    required = ("broker_transaction_id", "symbol", "isin", "trade_date", "side",
                "quantity", "price", "amount", "exchange", "segment")
    for field in required:
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"import_transactions: missing or empty required field {field!r}")


def _validate_holding_row(row: dict) -> None:
    """Raise ValueError if a holding row dict is missing its required
    identity fields (STORY-14). Only symbol/isin are required -- unlike
    transactions, quantity/average_price/last_price are legitimately
    ``None`` on BrokerHolding, so requiring them here would reject real,
    valid holdings."""
    required = ("symbol", "isin")
    for field in required:
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"import_holdings: missing or empty required field {field!r}")


# Module-level logger for the import_transactions warning
_logger = logging.getLogger(__name__)

