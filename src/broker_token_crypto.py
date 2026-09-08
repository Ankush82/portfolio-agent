"""At-rest symmetric encryption for broker access tokens.

This module exposes two tiny helpers, ``encrypt_secret(plaintext) -> str``
and ``decrypt_secret(ciphertext) -> str``, used to wrap a broker access
token before it touches the database and to unwrap it on the way out.
It uses :class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256,
authenticated, with a per-ciphertext random IV) and reads the key from
a single new environment variable, ``BROKER_TOKEN_ENCRYPTION_KEY``,
expected to be the urlsafe-base64-encoded 32-byte Fernet key Fernet
itself generates.

Validation happens at module load time with a plain
``os.environ.get(...)`` check -- matching this project's own pattern
elsewhere (see ``src/upstox_config.py``'s ``UpstoxConfig.from_env`` and
``src/llm.py``'s own ``OPENROUTER_API_KEY`` handling; there is no
``vf.ensure_keys`` helper in this codebase). The key must be present,
non-blank, and a well-formed Fernet key -- a missing/blank/invalid key
raises ``BrokerConfigError`` with the exact command an operator needs
to generate a fresh one, rather than inventing a default key or
silently falling back to plaintext storage. Fernet's authenticated
construction means a value produced under a different key, or any
tampered ciphertext, raises ``cryptography.fernet.InvalidToken`` --
this module re-raises that as ``BrokerConfigError`` so callers see a
single, named, descriptive error type instead of a low-level
``InvalidToken`` from a third-party library.

OPEN QUESTION (please flag in the PR description): this project does
not currently have a canonical app-secret or KMS mechanism -- I
checked ``src/cross_cutting/security.py``, ``src/llm.py``, the four
vendor-key ADRs (0043, 0046, 0047, 0048), and every component module
in ``src/components/`` before writing this, and nothing else wraps
arbitrary secrets at rest -- so the env-var path is the simplest thing
that actually works today. If a future ADR introduces a real KMS /
secrets-manager abstraction, this module should be the one and only
caller migrated to it, with no change to the ``encrypt_secret`` /
``decrypt_secret`` call sites. Today the deviation is intentionally
narrow: one env var, one Fernet key, one module.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

# Local definition of the custom exception this module raises on every
# failure path (missing key, invalid key, wrong-key ciphertext,
# tampered ciphertext). It is intentionally defined here in addition
# to being importable from ``src/upstox_config.py`` so this module is
# self-contained -- a caller that imports only ``broker_token_crypto``
# still gets a single, named, descriptive exception type to catch,
# rather than having to depend on a side effect of importing
# ``upstox_config``. Re-exported from ``upstox_config`` as well so both
# modules' ``BrokerConfigError`` references are the same class.
class BrokerConfigError(RuntimeError):
    """Raised for every failure path of this module: a missing,
    blank, or malformed ``BROKER_TOKEN_ENCRYPTION_KEY``, or a
    ciphertext that cannot be decrypted under the currently
    configured key (wrong key, tampered token, or garbage input).
    Callers should catch this -- not the underlying
    ``cryptography.fernet.InvalidToken`` or a generic ``RuntimeError``
    -- so the same exception type covers all "the encryption layer
    is not usable right now" cases."""


_ENV_VAR_NAME = "BROKER_TOKEN_ENCRYPTION_KEY"
_KEY_GEN_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _load_fernet() -> Fernet:
    """Read and validate ``BROKER_TOKEN_ENCRYPTION_KEY`` exactly once,
    at module load time. Returns a ready-to-use :class:`Fernet` or
    raises :class:`BrokerConfigError` with the key-generation command
    in the message -- never returns ``None``, never returns a
    placeholder, never logs the key value."""
    raw = os.environ.get(_ENV_VAR_NAME)
    if raw is None or not raw.strip():
        raise BrokerConfigError(
            f"{_ENV_VAR_NAME} is not set. A urlsafe-base64 32-byte Fernet key is "
            f"required to encrypt broker access tokens before they are stored. "
            f"Generate one with: {_KEY_GEN_COMMAND} and set the result as "
            f"{_ENV_VAR_NAME} in your .env file. Plaintext fallback is intentionally "
            f"not supported."
        )
    key = raw.strip()
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        # ValueError: not valid urlsafe-base64 or wrong decoded length.
        # TypeError: defensive -- Fernet only raises ValueError today,
        # but the contract is "rejected keys raise", so anything else
        # from Fernet() should also be wrapped, not leaked.
        raise BrokerConfigError(
            f"{_ENV_VAR_NAME} is present but is not a valid Fernet key "
            f"(expected a urlsafe-base64-encoded 32-byte key). Generate a fresh "
            f"one with: {_KEY_GEN_COMMAND} and replace the current value. "
            f"Underlying error: {exc}"
        ) from exc


# Validated once at import time, so encrypt_secret / decrypt_secret are
# zero-cost per call -- there is no key-parse work on the hot path.
# A misconfigured environment fails fast and loudly at startup, never
# silently at the first token write.
_FERNET = _load_fernet()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt ``plaintext`` (a broker access token) into a urlsafe
    Fernet token string ready to store. The output includes Fernet's
    own per-message random IV and timestamp, so two encryptions of the
    same plaintext produce different ciphertexts (one of the project's
    acceptance criteria). Returns ``str`` rather than ``bytes`` so the
    caller can drop the result straight into a text column without a
    second encoding step."""
    if not isinstance(plaintext, str):
        raise TypeError(
            f"encrypt_secret expects str plaintext, got {type(plaintext).__name__}"
        )
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a value previously produced by :func:`encrypt_secret`.
    Raises :class:`BrokerConfigError` if the ciphertext was produced
    under a different ``BROKER_TOKEN_ENCRYPTION_KEY`` than the one
    currently configured, or if the ciphertext was tampered with --
    never silently returns garbage. The original ``InvalidToken`` is
    preserved as ``__cause__`` for debugging but is not the exception
    the caller should be expected to catch."""
    if not isinstance(ciphertext, str):
        raise TypeError(
            f"decrypt_secret expects str ciphertext, got {type(ciphertext).__name__}"
        )
    try:
        return _FERNET.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        # InvalidToken: wrong key, tampered ciphertext, malformed token.
        # ValueError: malformed base64 / non-ascii input that Fernet
        # rejects before it can even reach HMAC verification. Both mean
        # the same thing to the caller -- "this ciphertext is not
        # decryptable with the key this process is configured with" --
        # so wrap both as BrokerConfigError so there is one exception
        # type to catch, not two.
        raise BrokerConfigError(
            "Failed to decrypt broker token: the ciphertext was produced under "
            "a different BROKER_TOKEN_ENCRYPTION_KEY than the one currently set, "
            "or the ciphertext is malformed/tampered with. If the encryption key "
            f"was rotated, the old token must be re-issued. Underlying error: {exc}"
        ) from exc