"""Market holiday detection for NSE/BSE and US (NYSE/NASDAQ) exchanges
— the dedicated follow-up to ``src/market_hours.py``. The docstring on
that module explicitly says "this module does NOT yet handle market
holidays and defers that to a future holiday-calendar module"; THIS is
that module.

The split is deliberate. ``src/market_hours.py`` answers
"Is this market open at moment X?" using a real IANA timezone,
DST-aware weekday arithmetic, and the operator-editable session
windows in ``config/market_hours.json``. Holidays are a separate
axis: they override the answer even on a normal weekday inside the
trading session (e.g. NSE on Diwali at 12:00 IST is closed, even
though 12:00 IST is squarely inside the 09:15–15:30 session window).
Mixing holiday data into the trading-hours config would entangle two
unrelated concerns; keeping it here means ops can edit either side
independently and the modules stay testable in isolation — the same
"one external concern per file" posture the rest of ``src/`` takes.

Design notes (mirroring ``src/market_hours.py``):

* **JSON-driven, not hardcoded.** Holiday calendars live in
  ``config/nse_holidays.json``, ``config/bse_holidays.json``, and
  ``config/us_holidays.json`` — three real files per STORY-11
  acceptance criteria. The format is
  ``[{"date": "YYYY-MM-DD", "name": "<holiday>"}, ...]`` so ops can
  add a one-off early-close or one-off closure by editing the file,
  no code change. Each file holds the holidays for ONE exchange even
  when the calendars overlap (NSE and BSE share most dates but
  occasionally diverge on Maharashtra-specific days), so the two
  Indian markets can be edited independently.

* **Lazy loading, import-time safety.** Like ``market_hours.py``, the
  config is read on first use, not at import time, so importing this
  module is side-effect-free and tests can monkeypatch the config
  path or delete a config file between calls without re-importing.

* **Missing-file = "no holidays".** Per acceptance criteria: a
  missing ``config/<x>_holidays.json`` is treated as "this exchange
  has no observed holidays", not an error. The price-cache layer
  (issue #53) must still function in deployments where ops hasn't
  shipped a holiday file yet — falling back to "never a holiday" is
  the safe failure mode (it over-caches slightly, but never wrongly
  says the market is open on a holiday it doesn't know about).

* **Exchange -> file routing.** NSE -> ``nse_holidays.json``,
  BSE -> ``bse_holidays.json``, NYSE and NASDAQ -> ``us_holidays.json``
  (both US exchanges share the SRO holiday calendar). Unknown
  exchanges raise a named ``UnknownMarketError`` — same posture
  ``market_hours.py`` takes so callers can use the same exception
  type for "unknown market" regardless of which module surfaces it.

* **Two-level env override.** ``MARKET_HOLIDAYS_CONFIG_DIR`` lets
  ops point all three holiday files at a different directory (e.g.
  a per-environment override), and individual files can be further
  overridden with ``MARKET_HOLIDAYS_NSE_FILE``,
  ``MARKET_HOLIDAYS_BSE_FILE``, ``MARKET_HOLIDAYS_US_FILE``. This
  mirrors ``market_hours.py``'s ``MARKET_HOURS_CONFIG_PATH`` env var
  and lets tests inject one-off calendars without mutating the bundled
  JSON.

The functions exposed here are deliberately small and focused:
``is_market_holiday`` for the single boolean-ish lookup, and
``get_holiday_name`` as the explicit accessor. ``src/market_hours.py``
imports from this module to honour holiday status in its existing
``is_market_open`` / ``market_status`` answers — no separate
"holiday status" endpoint needed at the call site, because the
caller's question is "is this market open NOW?" and the answer
should already factor holidays in."""

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Path to the holiday-config directory. Default is the bundled
# ``config/`` directory alongside ``market_hours.json``. The actual
# file lookup honours per-exchange env-var overrides below, but
# ``_config_dir`` is the parent of those paths so an ops override
# can repoint the entire directory at once (e.g. for a per-env
# fixtures tree).
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Per-exchange file mapping. NYSE and NASDAQ share the US calendar by
# SRO rule, so both map to the same file — the lookup function treats
# either key as equivalent for holiday purposes.
_EXCHANGE_FILES: dict[str, str] = {
    "NSE": "nse_holidays.json",
    "BSE": "bse_holidays.json",
    "NYSE": "us_holidays.json",
    "NASDAQ": "us_holidays.json",
}

# Per-exchange env-var overrides. Set e.g. ``MARKET_HOLIDAYS_NSE_FILE``
# to point the NSE lookup at a custom file — used by tests that want
# to inject a one-off calendar (e.g. a holiday fixture) without
# mutating the bundled JSON. Per-file overrides win over
# ``MARKET_HOLIDAYS_CONFIG_DIR``.
_ENV_OVERRIDES: dict[str, str] = {
    "NSE": "MARKET_HOLIDAYS_NSE_FILE",
    "BSE": "MARKET_HOLIDAYS_BSE_FILE",
    "NYSE": "MARKET_HOLIDAYS_US_FILE",
    "NASDAQ": "MARKET_HOLIDAYS_US_FILE",
}

# Module-level cache so repeated ``is_market_holiday`` calls don't
# re-read the same JSON file on every check. Keyed by full file path
# (so two different env-var overrides pointing at different files
# don't collide). Cleared by ``reset_holiday_cache`` for tests that
# mutate the config mid-run.
_CONFIG_CACHE: dict[str, dict[str, str]] = {}


class UnknownMarketError(ValueError):
    """Raised when an `exchange` argument is not one of the known
    keys (``NSE``, ``BSE``, ``NYSE``, ``NASDAQ``). Same posture as
    ``src/market_hours.py``'s ``UnknownMarketError`` so callers can
    treat the two modules' "unknown market" errors as a single
    failure mode."""


class MarketHolidaysConfigError(RuntimeError):
    """Raised when a holiday-config file exists but is structurally
    invalid (not JSON, missing ``holidays`` key, has a malformed
    entry, etc.). Distinct from a *missing* file, which is treated
    as "no holidays" per the acceptance criteria — see
    ``is_market_holiday`` for the missing-file path."""


def _config_dir() -> Path:
    """Returns the directory holiday-config files are loaded from.
    Honours ``MARKET_HOLIDAYS_CONFIG_DIR`` when set; defaults to the
    bundled ``<repo-root>/config/`` directory. Read at call time so
    importing this module never touches the environment."""
    override = os.environ.get("MARKET_HOLIDAYS_CONFIG_DIR")
    if override:
        return Path(override)
    return _DEFAULT_CONFIG_DIR


def _file_for_exchange(exchange: str) -> Path:
    """Returns the (possibly env-override) path to the holiday-config
    file for `exchange`. Per-exchange env var wins over
    ``MARKET_HOLIDAYS_CONFIG_DIR`` so a single-file test override
    doesn't have to rebind the whole directory.

    An exchange not in :data:`_EXCHANGE_FILES` (e.g. a custom market
    defined in ``config/market_hours.json`` for a test fixture) maps
    to ``None`` — the caller (:func:`_load_holidays`) treats that as
    "no holidays observed" rather than raising. This mirrors the
    missing-file contract: ``src/market_hours.py`` defines the
    canonical set of known markets, but ``src/market_holidays.py``
    must not break when a test or downstream caller registers a new
    market there. Raising ``UnknownMarketError`` here would couple
    the two modules' notion of "known market" unnecessarily; the
    safe answer is "no holidays" — same as a missing file."""
    filename = _EXCHANGE_FILES.get(exchange)
    if filename is None:
        return None
    env_var = _ENV_OVERRIDES.get(exchange)
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return Path(override)
    return _config_dir() / filename


def _parse_entry(entry: Any, exchange: str, index: int) -> tuple[date, str]:
    """Validates one ``{"date": "...", "name": "..."}`` entry. Returns
    ``(parsed_date, name)``. Raises ``MarketHolidaysConfigError`` on a
    malformed entry so a typo in the JSON fails loudly at first use,
    not silently as "this date is never a holiday"."""
    if not isinstance(entry, dict):
        raise MarketHolidaysConfigError(
            f"Holidays config for {exchange!r}: entry #{index} must be an object "
            f"with 'date' and 'name'; got {type(entry).__name__}: {entry!r}"
        )
    raw_date = entry.get("date")
    raw_name = entry.get("name")
    if not isinstance(raw_date, str) or not isinstance(raw_name, str) or not raw_name:
        raise MarketHolidaysConfigError(
            f"Holidays config for {exchange!r}: entry #{index} must have "
            f"non-empty string 'date' and 'name'; got {entry!r}"
        )
    try:
        parsed = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise MarketHolidaysConfigError(
            f"Holidays config for {exchange!r}: entry #{index} has invalid "
            f"'date' {raw_date!r}: {exc}"
        ) from exc
    return parsed, raw_name


def _load_holidays(exchange: str) -> dict[str, str]:
    """Reads and parses the holiday-config file for `exchange`,
    returning a ``{YYYY-MM-DD: holiday_name}`` dict. The dict is
    cached at module level keyed by full file path so multiple calls
    for the same exchange don't re-read disk, but different env-var
    overrides pointing at different files don't collide.

    Missing file (or unknown exchange with no mapping in
    :data:`_EXCHANGE_FILES`): returns an empty dict. Per the
    acceptance criteria, a missing holiday-config file is treated as
    "no holidays observed", NOT an error — so the price-cache layer
    (issue #53) can still run in deployments where ops hasn't shipped
    a holiday file yet. The empty-dict contract is what makes
    ``is_market_holiday`` and ``get_holiday_name`` safe to call in
    that environment: the dict lookup just returns ``None`` /
    ``False``. The "unknown exchange" path lets
    ``src/market_hours.py`` keep owning the canonical "known market"
    set without this module coupling to it — adding a new market to
    ``config/market_hours.json`` for a test fixture doesn't require
    a parallel edit here.

    Malformed file (exists but unparseable): raises
    ``MarketHolidaysConfigError``. Distinct from "missing" — the
    operator has shipped a file, so a real config error should
    surface loudly rather than silently disabling holiday detection."""
    path = _file_for_exchange(exchange)
    cache_key = str(path) if path is not None else f"<unknown:{exchange}>"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]

    # Unknown exchange (no entry in _EXCHANGE_FILES) -> no holidays.
    # Same contract as a missing file: don't raise, just return
    # empty. The caller (market_hours) is the source of truth for
    # which markets are "known"; this module stays agnostic.
    if path is None:
        _LOGGER.debug(
            "No holiday mapping for exchange %r; treating as 'no holidays'.",
            exchange,
        )
        _CONFIG_CACHE[cache_key] = {}
        return _CONFIG_CACHE[cache_key]

    # Missing file -> no holidays. Logged at debug so a fresh deploy
    # without a holiday file is observable in logs but doesn't spam
    # warnings on every market check.
    if not path.exists():
        _LOGGER.debug(
            "No holiday config for %s at %s; treating as 'no holidays'.",
            exchange,
            path,
        )
        _CONFIG_CACHE[cache_key] = {}
        return _CONFIG_CACHE[cache_key]

    try:
        raw = path.read_text()
    except OSError as exc:
        # OSError covers FileNotFoundError on a race (file deleted
        # between the exists() check and the read), plus permission
        # errors. Both are "can't read this file" -> "no holidays" is
        # the safe failure mode, matching the missing-file contract.
        _LOGGER.debug(
            "Could not read holiday config for %s at %s: %s; treating as 'no holidays'.",
            exchange,
            path,
            exc,
        )
        _CONFIG_CACHE[cache_key] = {}
        return _CONFIG_CACHE[cache_key]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarketHolidaysConfigError(
            f"Holidays config for {exchange!r} at {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise MarketHolidaysConfigError(
            f"Holidays config for {exchange!r} at {path} must be a JSON object "
            f"with a 'holidays' key; got {type(parsed).__name__}"
        )
    holidays_raw = parsed.get("holidays")
    if not isinstance(holidays_raw, list):
        raise MarketHolidaysConfigError(
            f"Holidays config for {exchange!r} at {path} must have a 'holidays' "
            f"list; got {type(holidays_raw).__name__}"
        )

    holidays: dict[str, str] = {}
    for index, entry in enumerate(holidays_raw):
        parsed_date, name = _parse_entry(entry, exchange, index)
        # ISO format string keeps the dict JSON-round-trippable and
        # matches the file format directly.
        holidays[parsed_date.isoformat()] = name

    _CONFIG_CACHE[cache_key] = holidays
    return holidays


def is_market_holiday(exchange: str, on_date: date) -> str | None:
    """Returns the holiday name if `on_date` is a market holiday for
    `exchange`, else ``None``. Same posture as
    ``src/market_hours.py``'s public API: case-sensitive exchange
    codes (``"NSE"``, ``"BSE"``, ``"NYSE"``, ``"NASDAQ"``), real
    config-file-driven lookup, no fabricated answers.

    Returns the holiday *name* rather than a ``bool`` so the caller
    can surface it directly to the user (the ``'holiday_name'`` key
    in ``market_status``'s dict). ``None`` and an empty string are
    both "not a holiday" — callers comparing against ``None`` (the
    recommended path) will never confuse the two.

    A missing holiday-config file is NOT an error: it returns
    ``None`` (no holidays observed) per the acceptance criteria.
    Only a *malformed* existing file raises
    ``MarketHolidaysConfigError``. Unknown exchange raises
    ``UnknownMarketError`` — same named exception
    ``market_hours.py`` raises for the same condition, so callers
    can use a single ``except`` clause for either module."""
    holidays = _load_holidays(exchange)
    return holidays.get(on_date.isoformat())


def get_holiday_name(exchange: str, on_date: date) -> str | None:
    """Alias for :func:`is_market_holiday` with a name that reflects
    the return type. Provided so callers that only care about the
    string can write intent-revealing code
    (``name = get_holiday_name("NSE", today)``) without a
    comment-explained boolean."""
    return is_market_holiday(exchange, on_date)


def reset_holiday_cache() -> None:
    """Drops the in-memory holiday-config caches so the next call to
    ``is_market_holiday`` re-reads from disk. Intended for tests that
    mutate a holiday file (or swap one in via env var) mid-run; not
    for production use. Mirrors ``market_hours.py``'s
    ``reset_config_cache``."""
    _CONFIG_CACHE.clear()


def list_known_holiday_markets() -> list[str]:
    """Returns the sorted list of exchange keys for which holiday
    lookup is supported. Useful for diagnostics and for tests that
    want to assert against the actual set rather than hardcoding it
    (same posture as ``market_hours.list_known_markets``)."""
    return sorted(_EXCHANGE_FILES.keys())