"""Market hours detection for NSE/BSE and US (NYSE/NASDAQ) exchanges —
the small focused module the project already uses for "one external
concern per file" (mirrors `src/exchange_rate_client.py`,
`src/alpha_vantage_client.py`, `src/tavily_client.py`, etc.).

This is a genuine prerequisite for issue #53 (price caching), which
needs to reason about market-open/market-closed transitions — i.e. it
isn't safe to cache a price fetched at 10:00 ET on a weekday the same
way one fetched at 02:00 ET on a Sunday would be, and the cache logic
needs a single source of truth for "is this market open right now?" to
make that distinction. That's the only reason this module exists: to
give the caching layer (and any future consumer) a real, DST-aware,
configuration-driven answer.

Holidays (issue #55) are layered on top of the trading-hours answer
here via ``src/market_holidays.py`` rather than mixed into the
``config/market_hours.json`` file. The split keeps the two concerns
editable independently (ops adjusts session hours vs. ops adds a
one-off exchange closure), keeps each module testable in isolation,
and lets this module's single public question — "is this market open
right now?" — incorporate the holiday calendar as a single override
applied after the weekday/session-block computation rather than as a
third axis the caller has to remember to check. See
``src/market_holidays.py`` for the calendar loading and the
``holiday_name`` key added to :func:`market_status`'s returned dict.

Design notes:

* **DST awareness via stdlib zoneinfo.** Python 3.9+ ships
  `zoneinfo.ZoneInfo`, which uses IANA tzdata and is fully DST-aware.
  This project requires Python >= 3.11 (see `pyproject.toml`), so no
  `pytz` dependency is needed. NSE/BSE run on `Asia/Kolkata` (a fixed
  +05:30 offset, no DST in practice); NYSE/NASDAQ run on
  `America/New_York` (UTC-5 in standard time, UTC-4 in daylight time).
  The same `ZoneInfo` call handles both correctly because the IANA
  database reflects each zone's real transition rules.

* **UTC internally.** Every `datetime` that crosses the
  `is_market_open` / `market_status` boundary is converted to UTC
  before any comparison, so the storage layer (issue #53) and any
  downstream caller can persist the "current status snapshot" in UTC
  without losing information. The `now_utc` parameter is explicitly
  typed `datetime` (not `datetime.datetime` — already in the
  `datetime` namespace) and is treated as UTC when naive, matching the
  project-wide convention that internal time is UTC and anything else
  has an explicit tzinfo.

* **Configuration-driven, not hardcoded.** Trading hours live in
  `config/market_hours.json` so ops can adjust them (e.g. for a market
  holiday observed as a one-off, or for an early-close day) without a
  code change. The config file is read lazily on first use, not at
  import time, so importing this module is side-effect-free — the same
  posture `src/exchange_rate_client.py` takes with `.env` loading.

* **Both `is_market_open` and `market_status`.** The story leaves
  whether `market_status` is useful to the caching logic up to the
  implementer's judgement; it is included because issue #53 needs the
  *next* open/close transition timestamp to decide how long its cache
  is valid for (a price fetched at 09:00 ET is good until 09:30 ET, a
  price fetched at 15:55 ET is only good until 16:00 ET, and a price
  fetched at 17:00 ET on a Friday is good until Monday 09:30 ET). The
  bare `bool` answer from `is_market_open` doesn't carry that — the
  dict from `market_status` does, and both functions share the same
  underlying computation so there is no duplication."""

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# `datetime` here is the *class* imported from the `datetime` module,
# used for `datetime.combine(...)`, `datetime.now(...)`, etc. — same
# as the rest of this codebase. For testability of the
# `now_utc=None` code path, tests can monkeypatch the bound
# `datetime` symbol with a stand-in class that has BOTH a `now`
# classmethod AND a `combine` static method (see
# `test_now_utc_none_uses_real_wall_clock`).

_LOGGER = logging.getLogger(__name__)

# Path to the trading-hours configuration. Located at the project root
# (sibling of `src/`, `tests/`, `pyproject.toml`) so it sits alongside
# other operator-edited artefacts. Configurable via the
# `MARKET_HOURS_CONFIG_PATH` env var for ops who want to point at a
# different file (e.g. a per-environment override) without editing
# this module.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "market_hours.json"

# A market's "status" can be one of these three. Kept as plain string
# constants (not an Enum) so callers can compare against the JSON
# without importing this module, and so the dict returned by
# `market_status` is JSON-round-trippable for free.
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_UNKNOWN = "unknown"


class UnknownMarketError(ValueError):
    """Raised when an `exchange` argument is not present in the
    `config/market_hours.json` markets section. A specific, named
    exception (the same posture `src/exchange_rate_client.py`'s
    `MissingExchangeRateAPIKeyError` and `ExchangeRateFetchError`
    already take) so callers get an unambiguous signal that they
    asked about an exchange this module genuinely doesn't know about
    — not a generic `KeyError` they'd have to decode themselves."""


class MarketHoursConfigError(RuntimeError):
    """Raised when `config/market_hours.json` is missing, malformed, or
    structurally invalid (missing a required key, an unparseable
    `timezone`, an out-of-range `trading_days` value, etc.). A named
    exception so a real configuration error surfaces loudly rather
    than silently falling through to "market is closed for everyone"."""

    pass


def get_config_path() -> Path:
    """Returns the path to the trading-hours config file. Honours the
    `MARKET_HOURS_CONFIG_PATH` env var when set, otherwise returns the
    documented default (`<repo-root>/config/market_hours.json`). Reads
    the env var at call time — not at import time — so importing this
    module never touches the environment."""
    import os

    override = os.environ.get("MARKET_HOURS_CONFIG_PATH")
    if override:
        return Path(override)
    return _CONFIG_PATH


def _load_config() -> dict:
    """Reads and parses `config/market_hours.json`. Called lazily by
    `_resolve_market`, never at module import time, so the module
    stays importable in environments where the config file isn't
    present (tests, smoke runs) — the same import-time posture the
    other vendor client modules take.

    The file is cached at module level after the first successful
    load so repeated `is_market_open` / `market_status` calls don't
    re-read the disk on every check. If ops edit the file, a process
    restart picks up the new values — intentional, since the price
    caching layer that will consume this is itself a long-running
    process and doesn't need hot-reload semantics."""
    global _CONFIG_CACHE  # noqa: PLW0603
    try:
        return _CONFIG_CACHE
    except NameError:
        pass

    config_path = get_config_path()
    try:
        raw = config_path.read_text()
    except FileNotFoundError as exc:
        raise MarketHoursConfigError(
            f"Market-hours config file not found at {config_path}. "
            "Create config/market_hours.json with the markets section, or set "
            "MARKET_HOURS_CONFIG_PATH to point at a different file."
        ) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarketHoursConfigError(
            f"Market-hours config at {config_path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict) or "markets" not in parsed:
        raise MarketHoursConfigError(
            f"Market-hours config at {config_path} must be a JSON object with a "
            f"'markets' key; got {type(parsed).__name__} with keys "
            f"{list(parsed.keys()) if isinstance(parsed, dict) else '(not a dict)'}"
        )

    _CONFIG_CACHE = parsed
    return parsed


def _resolve_market(exchange: str) -> tuple[str, dict]:
    """Looks up an exchange's market definition from the loaded config
    and returns `(zone_name, market_def)`. Validates that the
    `timezone` value is a real IANA zone this Python installation can
    resolve (catches typos at config-load time, not at first market
    check), and that `trading_days` and `session_blocks` are well-formed.
    Raises `UnknownMarketError` for an unknown exchange name;
    `MarketHoursConfigError` for a structurally broken one."""
    config = _load_config()
    markets = config.get("markets")
    if not isinstance(markets, dict):
        raise MarketHoursConfigError(
            f"Market-hours config 'markets' must be an object; got {type(markets).__name__}"
        )
    market_def = markets.get(exchange)
    if market_def is None:
        raise UnknownMarketError(
            f"Unknown market {exchange!r}. Known markets: "
            f"{sorted(markets.keys())}. Edit config/market_hours.json to add it."
        )
    if not isinstance(market_def, dict):
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r} must be an object; "
            f"got {type(market_def).__name__}"
        )

    zone_name = market_def.get("timezone")
    if not isinstance(zone_name, str) or not zone_name:
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r} is missing 'timezone'"
        )
    # Eagerly resolve the ZoneInfo so a typo (e.g. "Asia/Kolkatta") is
    # caught at the first lookup, not the first is_market_open call.
    # Wrap the ZoneInfo lookup so a bad zone name surfaces as our named
    # `MarketHoursConfigError` rather than `zoneinfo.ZoneInfoNotFoundError` —
    # callers can then distinguish "config is broken" from "exchange is
    # unknown" using the same exception type as the rest of this module's
    # config-validation paths.
    try:
        ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r} has an unrecognised "
            f"IANA timezone {zone_name!r}: {exc}"
        ) from exc

    trading_days = market_def.get("trading_days")
    if not isinstance(trading_days, list) or not all(
        isinstance(d, int) and 0 <= d <= 6 for d in trading_days
    ):
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r} has invalid "
            f"'trading_days': must be a list of integers 0..6 (Mon..Sun); got {trading_days!r}"
        )

    session_blocks = market_def.get("session_blocks")
    if not isinstance(session_blocks, list) or not session_blocks:
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r} has invalid "
            f"'session_blocks': must be a non-empty list of {{open, close}} "
            f"objects; got {session_blocks!r}"
        )
    for block in session_blocks:
        if (
            not isinstance(block, dict)
            or "open" not in block
            or "close" not in block
        ):
            raise MarketHoursConfigError(
                f"Market-hours config entry for {exchange!r} has a malformed "
                f"session_block: {block!r}. Each block must be an object with "
                f"'open' and 'close' HH:MM strings."
            )
        _parse_hhmm(block["open"], exchange, "open")
        _parse_hhmm(block["close"], exchange, "close")

    return zone_name, market_def


def _parse_hhmm(value, exchange: str, field: str) -> time:
    """Parses an "HH:MM" string (24-hour clock) into a `datetime.time`.
    Raises `MarketHoursConfigError` on a malformed value so a typo in
    config/market_hours.json fails loudly here, not silently as a
    24-hour-open market or a never-open one."""
    if not isinstance(value, str):
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r}: {field!r} must be an "
            f"'HH:MM' string, got {type(value).__name__}: {value!r}"
        )
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise MarketHoursConfigError(
            f"Market-hours config entry for {exchange!r}: {field!r}={value!r} "
            f"is not a valid 'HH:MM' time: {exc}"
        ) from exc
    return parsed


def _normalize_now(now_utc: datetime | None) -> datetime:
    """Coerces the `now_utc` parameter into an aware UTC `datetime`.

    * `None` -> `datetime.now(timezone.utc)` (the real wall clock, in
      UTC). This is what the price-cache layer will pass when it
      computes "is this market open at *now*?" — the literal answer
      to the question, not a fabricated one.
    * A naive `datetime` (no tzinfo) is interpreted as UTC. The
      project's internal-time convention is "UTC unless otherwise
      tagged", and silently treating a naive input as local-time
      would be a real bug — e.g. a tester running tests in a
      non-UTC timezone would get answers that depend on their TZ env.
      This coercion makes that assumption explicit.
    * An aware `datetime` in any other timezone is converted to UTC.
      Accepting aware datetimes from other zones is necessary because
      that's what `datetime.now(ZoneInfo('America/New_York'))` returns,
      and tests that exercise DST transitions will construct aware
      datetimes directly."""
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def _local_now(now_utc: datetime, zone_name: str) -> datetime:
    """Converts an aware UTC `datetime` into the market's local zone,
    using a real IANA-aware `ZoneInfo` so DST transitions are handled
    correctly for `America/New_York`. The `astimezone` call is the
    single point where DST actually enters the computation — keeping
    it here, rather than spread across `is_market_open` and
    `market_status`, ensures the two functions can never disagree on
    "what time is it in New York right now?"."""
    return now_utc.astimezone(ZoneInfo(zone_name))


def _compute_status(
    now_utc: datetime, zone_name: str, market_def: dict, exchange: str
) -> dict:
    """The shared inner computation for `is_market_open` and
    `market_status`. Returns a dict with at minimum:

        {
          "exchange": <str>,
          "status": "open" | "closed",
          "now_utc": <ISO-8601 string>,
          "now_local": <ISO-8601 string>,
          "timezone": <zone name>,
          "next_transition_utc": <ISO-8601 string> | None,
          "next_status": "open" | "closed" | None,
          "holiday_name": <str> | None,
        }

    `next_transition_utc` is the next moment the market will switch
    between open and closed (or vice versa), in real UTC. Returns
    `None` for both fields if the next transition is more than a week
    away — e.g. if a holiday rules out the next weekday, the answer
    would be ambiguous without a holiday calendar and the function
    errs on the side of "I don't know, ask the operator".

    The function does NOT raise on weekends or after-hours; it just
    returns `status="closed"` and points `next_transition_utc` at the
    next open time. The caller decides what to do with that.

    Holiday handling: when the local date is a market holiday per
    ``src/market_holidays.py``, ``status`` is forced to
    ``STATUS_CLOSED`` regardless of weekday or session-block math,
    and ``holiday_name`` is set to the observed holiday's name. The
    holiday override is layered ON TOP of the trading-hours answer
    rather than mixed into it, so a bug in the holiday module
    surfaces as "always says open on a holiday" (the existing
    session-block answer) rather than masking the trading-hours
    logic entirely."""
    local_now = _local_now(now_utc, zone_name)
    weekday = local_now.weekday()  # 0=Monday .. 6=Sunday
    trading_days = market_def["trading_days"]
    session_blocks_raw = market_def["session_blocks"]

    # Convert HH:MM strings to time objects once per call (cheap; this
    # function isn't on a hot path).
    session_blocks = [
        (_parse_hhmm(b["open"], market_def.get("display_name", "?"), "open"),
         _parse_hhmm(b["close"], market_def.get("display_name", "?"), "close"))
        for b in session_blocks_raw
    ]

    open_now = weekday in trading_days and any(
        open_t <= local_now.time() < close_t for open_t, close_t in session_blocks
    )
    status = STATUS_OPEN if open_now else STATUS_CLOSED

    # Holiday override: layered on top of the session-block answer
    # so a holiday on a normal trading day (e.g. NSE Diwali, US
    # Thanksgiving) closes the market even at 12:00 local. The
    # import is local because (a) market_hours must remain
    # importable without market_holidays present in pathological
    # test environments, and (b) market_holidays itself imports
    # nothing from market_hours so there's no real cycle — the
    # local import is purely about import-time safety.
    from market_holidays import is_market_holiday

    holiday_name = is_market_holiday(exchange, local_now.date())
    if holiday_name is not None:
        status = STATUS_CLOSED

    next_transition_utc, next_status = _next_transition(
        now_utc, local_now, weekday, trading_days, session_blocks
    )

    return {
        "exchange": market_def.get("display_name") or "",
        "status": status,
        "now_utc": now_utc.isoformat(),
        "now_local": local_now.isoformat(),
        "timezone": zone_name,
        "next_transition_utc": next_transition_utc.isoformat() if next_transition_utc else None,
        "next_status": next_status,
        "holiday_name": holiday_name,
    }


def _next_transition(
    now_utc: datetime,
    local_now: datetime,
    weekday: int,
    trading_days: list[int],
    session_blocks: list[tuple[time, time]],
) -> tuple[datetime | None, str | None]:
    """Computes the next open/close transition strictly AFTER
    `local_now`, in UTC. Walks the schedule forward up to a week; if
    no transition is found within that window (which would imply a
    broken config — e.g. an empty `trading_days` list — or a missing
    holiday calendar), returns `(None, None)` and lets the caller
    handle the ambiguity."""
    zone_name = local_now.tzinfo
    local_date = local_now.date()

    # Search up to 8 days forward (today + 7). 7 is the max realistic
    # answer (next weekday after a Sunday close); 8 gives us a one-day
    # safety margin for any config edge case without risking an
    # infinite loop on a broken schedule.
    for day_offset in range(0, 8):
        candidate_date = local_date + timedelta(days=day_offset)
        candidate_weekday = (weekday + day_offset) % 7
        if candidate_weekday not in trading_days:
            continue

        for open_t, close_t in session_blocks:
            if day_offset == 0:
                # Today: only future transitions count.
                if open_t > local_now.time():
                    return (
                        _local_to_utc(
                            datetime.combine(candidate_date, open_t, tzinfo=zone_name)
                        ),
                        STATUS_OPEN,
                    )
                if close_t > local_now.time():
                    return (
                        _local_to_utc(
                            datetime.combine(candidate_date, close_t, tzinfo=zone_name)
                        ),
                        STATUS_CLOSED,
                    )
            else:
                # Future day: the first open of the day is the next
                # transition from a currently-closed state.
                return (
                    _local_to_utc(
                        datetime.combine(candidate_date, open_t, tzinfo=zone_name)
                    ),
                    STATUS_OPEN,
                )

    # No transition found within the search window.
    return None, None


def _local_to_utc(local_dt: datetime) -> datetime:
    """Converts a wall-clock datetime in some IANA zone to UTC. Used
    internally to turn the "next transition" wall-clock time into the
    UTC timestamp the caller (and the caching layer) actually wants."""
    return local_dt.astimezone(timezone.utc)


def is_market_open(exchange: str, now_utc: datetime | None = None) -> bool:
    """Returns `True` iff the named market is currently open for
    trading.

    `exchange` is one of the keys in `config/market_hours.json`'s
    `markets` section (e.g. "NSE", "BSE", "NYSE", "NASDAQ").
    Case-sensitive — same posture as the rest of this codebase, where
    exchange codes are uppercase canonical identifiers.

    `now_utc` is the moment to evaluate at; defaults to
    `datetime.now(timezone.utc)` (the real wall clock) when omitted.
    Naive datetimes are interpreted as UTC; aware datetimes in other
    zones are converted to UTC. All comparisons happen in the
    market's local time internally (so DST transitions are handled
    correctly for `America/New_York`) but the parameter and the
    price-cache layer see only UTC.

    Returns `False` whenever the market is closed — including when
    the local date is a market holiday per
    ``src/market_holidays.py`` (e.g. NSE on Diwali, NYSE on
    Thanksgiving). Holiday closure is layered on top of the
    weekday/session-block answer, so a holiday on a normal trading
    day returns `False` even at noon local time. To get the holiday
    name in addition to the closed status, use
    :func:`market_status` and read its `holiday_name` key.

    Raises `UnknownMarketError` if `exchange` isn't in the config, and
    `MarketHoursConfigError` if the config itself is malformed. Both
    are real, named exceptions so callers can distinguish "I asked
    about a market this module doesn't know" from "the config file is
    broken" — the same shape `src/exchange_rate_client.py`'s error
    hierarchy takes.

    Backed by `zoneinfo.ZoneInfo` — stdlib since Python 3.9, fully
    DST-aware via IANA tzdata — so NYSE/NASDAQ's 9:30-16:00 ET window
    is correct on both sides of the March/November DST transitions
    without this module needing to know they exist.
    """
    zone_name, market_def = _resolve_market(exchange)
    normalized = _normalize_now(now_utc)
    return _compute_status(normalized, zone_name, market_def, exchange)["status"] == STATUS_OPEN


def market_status(exchange: str, now_utc: datetime | None = None) -> dict:
    """Returns a dict describing the market's current status plus the
    next moment it will transition. Shape:

        {
          "exchange":   "<display name from config>",
          "status":     "open" | "closed",
          "now_utc":    "<ISO-8601 UTC timestamp>",
          "now_local":  "<ISO-8601 local timestamp>",
          "timezone":   "<IANA zone name>",
          "next_transition_utc": "<ISO-8601 UTC timestamp>" | None,
          "next_status":         "open" | "closed" | None,
          "holiday_name":        "<holiday name>" | None,
        }

    `next_transition_utc` is the moment the market will next switch
    state, in real UTC. This is what the price-cache layer (issue
    #53) keys off: a cache entry's TTL is "the time until the next
    transition", not a fixed wall-clock window, because once a market
    closes the price stops moving and the cache can be served much
    longer.

    `next_status` is the status the market will be in *after*
    `next_transition_utc`. For a currently-closed market it is
    "open"; for a currently-open one it is "closed". When neither
    value can be computed within a one-week search window (e.g.
    config says trading_days is empty), both are `None` and the
    caller should treat that as "ask the operator" rather than
    guessing.

    `holiday_name` is the name of the market holiday observed today
    (per ``src/market_holidays.py``) or ``None`` if today is not a
    holiday. When set, `status` is forced to ``"closed"`` — the
    holiday override is layered on top of the trading-hours answer
    so a holiday on a normal trading day closes the market even
    inside the normal session window. Callers displaying the market
    status to a user should surface the holiday name alongside the
    "closed" status.

    Raises the same `UnknownMarketError` / `MarketHoursConfigError`
    as `is_market_open`. Same DST handling. Same `now_utc`
    semantics."""
    zone_name, market_def = _resolve_market(exchange)
    normalized = _normalize_now(now_utc)
    # Carry the exchange *code* (the input key), not the display name,
    # so the dict round-trips: market_status("NSE", ...)["exchange"]
    # is "NSE" regardless of what the config calls it.
    result = _compute_status(normalized, zone_name, market_def, exchange)
    result["exchange"] = exchange
    return result


def list_known_markets() -> list[str]:
    """Returns the sorted list of market keys defined in
    `config/market_hours.json`. Useful for diagnostics (e.g. the
    price-cache layer can log "no config entry for exchange X,
    skipping") and for tests that want to assert against the actual
    set rather than hardcoding it."""
    config = _load_config()
    markets = config.get("markets", {})
    return sorted(markets.keys())


def reset_config_cache() -> None:
    """Drops the in-memory copy of the config so the next call to
    `_load_config` re-reads `config/market_hours.json` from disk.
    Intended for tests that mutate the config file mid-run; not for
    production use (the price-cache layer is a long-running process
    that reads config once at startup)."""
    global _CONFIG_CACHE  # noqa: PLW0603
    try:
        del _CONFIG_CACHE
    except NameError:
        pass