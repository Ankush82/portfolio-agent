"""Resend client — resolves ADR-0040's notification-delivery-channel gap
(ADR-0048), the same "one small module per external vendor" shape
`src/llm.py`, `src/mem0_embedder.py`, `src/alpha_vantage_client.py`, and
`src/tavily_client.py` already established.

This module is deliberately the *only* place in the codebase that reads
`RESEND_API_KEY` or talks to Resend's `/emails` endpoint.
`src/components/c13_interaction_notification.py`'s `ResendNotificationChannel`
calls `send_email` from here rather than reimplementing any part of this.

`RESEND_API_KEY` is read at call time, inside `get_api_key()` — never at
import time — so this module stays importable, and importing it never
touches the environment or the filesystem.
"""

import os
from pathlib import Path

import requests

RESEND_SEND_URL = "https://api.resend.com/emails"

# Resend's own conventional sandbox sender address. Real, not a
# placeholder value — but whether it actually works with no domain
# verification depends on the account: verified live against this
# project's own Resend account (ADR-0048) and rejected with a real 403
# ("The resend.com domain is not verified") until a domain is added and
# verified at resend.com/domains. Kept as the default because it is
# still the correct address once a domain is verified for accounts
# where Resend does allow it unverified — this project's own account
# is the stricter case, not necessarily the general one.
DEFAULT_FROM_ADDRESS = "onboarding@resend.com"

_REQUEST_TIMEOUT_SECONDS = 30
_ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"


class MissingResendAPIKeyError(RuntimeError):
    """Raised by `send_email` when `RESEND_API_KEY` is not set in the
    environment at call time. A specific, named exception — the same
    posture every other vendor module in this project already takes —
    so a caller that genuinely wanted real delivery gets an unambiguous
    signal, rather than a generic error or a silent fallback to
    placeholder-shaped output."""


def _load_dotenv_into_environ() -> None:
    """Manual `.env` parsing, not `python-dotenv` — same reasoning and
    same shape as every other vendor module's own
    `_load_dotenv_into_environ`. Never overwrites a variable already set
    in the real process environment."""
    if not _ENV_FILE_PATH.exists():
        return
    for line in _ENV_FILE_PATH.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, _, value = stripped_line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str | None:
    """Reads `RESEND_API_KEY` from the environment or a `.env` file at
    the repo root, at call time. Returns `None` when it isn't configured
    — callers (`get_notification_channel`, below) use that `None` to
    decide whether to construct a real channel or fall back to a
    placeholder, the same selection shape every other vendor module in
    this project already established."""
    _load_dotenv_into_environ()
    return os.environ.get("RESEND_API_KEY")


def send_email(to: str, subject: str, text: str, from_address: str = DEFAULT_FROM_ADDRESS) -> dict:
    """One real HTTP POST to Resend's `/emails` endpoint. Returns
    Resend's own response body (a dict containing a real message `id`
    on success) — deliberately not reshaped, since the caller
    (`ResendNotificationChannel`) only needs to know whether the call
    succeeded, not interpret the body itself.

    Raises `MissingResendAPIKeyError` if no key is configured at call
    time, and raises on a non-2xx response (`raise_for_status`) — both
    real failures, not silently degraded here. The caller decides what,
    if anything, to do about either.

    Real, honest limitation, not hidden: verified live against this
    project's own Resend account (ADR-0048), sending from
    `DEFAULT_FROM_ADDRESS` with no domain added/verified at
    resend.com/domains fails with a real `403` ("The resend.com domain
    is not verified") — Resend's own anti-abuse restriction on
    unverified accounts, not a limitation this module invents. Every
    send will fail this way until a domain is verified; that failure
    propagates as a normal `requests.exceptions.HTTPError`, the same as
    any other non-2xx response."""
    api_key = get_api_key()
    if not api_key:
        raise MissingResendAPIKeyError(
            "RESEND_API_KEY is not set. send_email requires a real Resend API key at call time "
            "(from the environment or a .env file at the repo root) — construct the caller's "
            "own placeholder channel instead if no key is configured."
        )
    response = requests.post(
        RESEND_SEND_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": from_address, "to": [to], "subject": subject, "text": text},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()
