"""Tests for src/broker_token_crypto.py.

The module validates ``BROKER_TOKEN_ENCRYPTION_KEY`` at import time, so
each test sets (or clears) the env var before triggering a fresh import
of the module via ``importlib.reload`` -- the same pattern pytest itself
recommends for modules whose top-level code reads environment variables.
"""

import importlib
import os

import pytest
from cryptography.fernet import Fernet


_KEY_GEN_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


@pytest.fixture
def fresh_fernet_key(monkeypatch):
    """Generate a real Fernet key, set it in the environment, and
    yield it so the test can also use it directly (e.g. to build a
    ciphertext under a *different* key for the wrong-key case). The
    fixture cleans itself up via ``monkeypatch``."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", key)
    return key


def _reload_module():
    """Re-import ``broker_token_crypto`` so its module-load-time
    ``_load_fernet()`` re-reads the current environment. Returns the
    freshly loaded module object."""
    import broker_token_crypto
    return importlib.reload(broker_token_crypto)


def test_encrypt_then_decrypt_round_trips_ascii(monkeypatch):
    """The headline acceptance criterion:
    ``decrypt_secret(encrypt_secret(s)) == s`` for ASCII input."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    plaintext = "upstox-access-token-abc123XYZ"
    ciphertext = mod.encrypt_secret(plaintext)

    assert mod.decrypt_secret(ciphertext) == plaintext


def test_encrypt_then_decrypt_round_trips_unicode(monkeypatch):
    """The headline acceptance criterion also holds for unicode."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    plaintext = "broker-token-\u4e2d\u6587-\U0001F600-\u00e9"
    ciphertext = mod.encrypt_secret(plaintext)

    assert ciphertext != plaintext
    assert mod.decrypt_secret(ciphertext) == plaintext


def test_encrypt_produces_ciphertext_that_differs_from_plaintext(monkeypatch):
    """The ciphertext is never the same string as the plaintext."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    plaintext = "the-quick-brown-fox"
    ciphertext = mod.encrypt_secret(plaintext)

    assert ciphertext != plaintext
    assert plaintext not in ciphertext


def test_encrypt_produces_different_ciphertexts_across_calls(monkeypatch):
    """Fernet uses a random per-message IV, so two encryptions of the
    same plaintext produce different ciphertexts (the other half of
    the project's acceptance criterion)."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    plaintext = "same-plaintext-both-times"
    ciphertext_a = mod.encrypt_secret(plaintext)
    ciphertext_b = mod.encrypt_secret(plaintext)

    assert ciphertext_a != ciphertext_b
    assert mod.decrypt_secret(ciphertext_a) == plaintext
    assert mod.decrypt_secret(ciphertext_b) == plaintext


def test_encrypt_secret_returns_str_not_bytes(monkeypatch):
    """The function signature promises ``str``; the caller should
    be able to drop the result straight into a text column."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    ciphertext = mod.encrypt_secret("hello")

    assert isinstance(ciphertext, str)


def test_decrypt_secret_raises_broker_config_error_on_wrong_key(monkeypatch):
    """Decrypting a value produced under a different key raises a
    clear, named ``BrokerConfigError`` rather than returning garbage
    or leaking ``cryptography.fernet.InvalidToken`` to the caller."""
    first_key = Fernet.generate_key().decode()
    second_key = Fernet.generate_key().decode()

    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", first_key)
    mod_under_first_key = _reload_module()
    ciphertext = mod_under_first_key.encrypt_secret("a-broker-access-token")

    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", second_key)
    mod_under_second_key = _reload_module()

    with pytest.raises(mod_under_second_key.BrokerConfigError) as excinfo:
        mod_under_second_key.decrypt_secret(ciphertext)

    assert "different BROKER_TOKEN_ENCRYPTION_KEY" in str(excinfo.value)
    # The low-level cause should be InvalidToken, not the high-level
    # BrokerConfigError, so callers debugging get the original signal.
    from cryptography.fernet import InvalidToken
    assert isinstance(excinfo.value.__cause__, InvalidToken)


def test_module_load_raises_broker_config_error_when_env_var_missing(monkeypatch, tmp_path):
    """A missing ``BROKER_TOKEN_ENCRYPTION_KEY`` fails fast at import
    time with a message that contains the exact command to generate
    a key -- no default key, no plaintext fallback. Tested in a fresh
    subprocess so the module's own top-level code (which runs once on
    import and raises on a missing key) is actually exercised."""
    import subprocess
    import sys

    env_patch = {"PYTHONPATH": os.pathsep.join(sys.path), "PATH": os.environ.get("PATH", "")}
    # Explicitly strip the env var so the child process really has none set.
    env_patch.pop("BROKER_TOKEN_ENCRYPTION_KEY", None)

    result = subprocess.run(
        [sys.executable, "-c", "import broker_token_crypto"],
        env={k: v for k, v in os.environ.items() if k != "BROKER_TOKEN_ENCRYPTION_KEY"},
        capture_output=True,
        text=True,
        cwd="src",
    )

    assert result.returncode != 0, f"module imported successfully despite missing key: {result.stdout}"
    combined = result.stdout + result.stderr
    assert "BrokerConfigError" in combined
    assert "BROKER_TOKEN_ENCRYPTION_KEY" in combined
    assert _KEY_GEN_COMMAND in combined
    assert "Plaintext fallback is intentionally not supported" in combined


def test_module_load_raises_broker_config_error_when_env_var_blank(monkeypatch):
    """A blank / whitespace-only value is treated the same as
    missing -- otherwise an accidental empty .env entry would
    silently fall through to plaintext storage."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "BROKER_TOKEN_ENCRYPTION_KEY"}
    env["BROKER_TOKEN_ENCRYPTION_KEY"] = "   \t\n   "

    result = subprocess.run(
        [sys.executable, "-c", "import broker_token_crypto"],
        env=env,
        capture_output=True,
        text=True,
        cwd="src",
    )

    assert result.returncode != 0, "blank key was accepted"
    combined = result.stdout + result.stderr
    assert "BrokerConfigError" in combined
    assert "BROKER_TOKEN_ENCRYPTION_KEY" in combined
    assert _KEY_GEN_COMMAND in combined


def test_module_load_raises_broker_config_error_on_invalid_key_format(monkeypatch):
    """A key that is present but not a valid urlsafe-base64 32-byte
    Fernet key fails fast with the key-generation command in the
    message -- not a low-level ``ValueError`` from cryptography."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "BROKER_TOKEN_ENCRYPTION_KEY"}
    env["BROKER_TOKEN_ENCRYPTION_KEY"] = "this-is-not-a-real-fernet-key"

    result = subprocess.run(
        [sys.executable, "-c", "import broker_token_crypto"],
        env=env,
        capture_output=True,
        text=True,
        cwd="src",
    )

    assert result.returncode != 0, "garbage key was accepted"
    combined = result.stdout + result.stderr
    assert "BrokerConfigError" in combined
    assert "BROKER_TOKEN_ENCRYPTION_KEY" in combined
    assert _KEY_GEN_COMMAND in combined
    assert "valid Fernet key" in combined


def test_decrypt_secret_raises_broker_config_error_on_garbage_input(monkeypatch):
    """A clearly-malformed ciphertext (not even Fernet-shaped) still
    raises the project's named ``BrokerConfigError``, not a raw
    ``ValueError`` from Fernet's base64 layer."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    with pytest.raises(mod.BrokerConfigError) as excinfo:
        mod.decrypt_secret("not-a-real-ciphertext")

    assert "Failed to decrypt broker token" in str(excinfo.value)


def test_encrypt_secret_rejects_non_string_input(monkeypatch):
    """Defensive: a caller passing ``bytes`` by mistake gets a clear
    ``TypeError`` rather than a silently-encoded value the caller did
    not intend."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    with pytest.raises(TypeError) as excinfo:
        mod.encrypt_secret(b"raw-bytes-not-allowed")

    assert "encrypt_secret expects str plaintext" in str(excinfo.value)


def test_decrypt_secret_rejects_non_string_input(monkeypatch):
    """Same defensive contract on the decrypt side."""
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod = _reload_module()

    with pytest.raises(TypeError) as excinfo:
        mod.decrypt_secret(b"raw-bytes-not-allowed")

    assert "decrypt_secret expects str ciphertext" in str(excinfo.value)


def test_no_plaintext_token_or_key_in_error_messages(monkeypatch):
    """The acceptance criterion: no plaintext token or key value is
    ever written into an error message. Generate a clearly
    recognizable sentinel plaintext and confirm none of it appears in
    any of this module's error strings."""
    import subprocess
    import sys

    sentinel_plaintext = "SENTINEL-PLAINTEXT-SECRET-XYZ12345"
    sentinel_key = Fernet.generate_key().decode()

    # Missing-key path -- run a fresh subprocess with no env var and
    # confirm neither the sentinel plaintext nor the sentinel key
    # appear in the captured stderr/stdout.
    result = subprocess.run(
        [sys.executable, "-c", "import broker_token_crypto"],
        env={k: v for k, v in os.environ.items() if k != "BROKER_TOKEN_ENCRYPTION_KEY"},
        capture_output=True,
        text=True,
        cwd="src",
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert sentinel_plaintext not in combined
    assert sentinel_key not in combined

    # Invalid-key path -- the sentinel plaintext used as the key must
    # not be echoed back into the error.
    env = {k: v for k, v in os.environ.items()}
    env["BROKER_TOKEN_ENCRYPTION_KEY"] = sentinel_plaintext
    result = subprocess.run(
        [sys.executable, "-c", "import broker_token_crypto"],
        env=env,
        capture_output=True,
        text=True,
        cwd="src",
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert sentinel_plaintext not in combined, f"sentinel plaintext leaked into error: {combined}"

    # Now configure a real key and produce an error decrypting a
    # sentinel plaintext ciphertext under the wrong key -- the error
    # must not include the sentinel plaintext.
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", sentinel_key)
    mod = _reload_module()
    ciphertext = mod.encrypt_secret(sentinel_plaintext)
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    mod2 = _reload_module()
    with pytest.raises(mod2.BrokerConfigError) as excinfo:
        mod2.decrypt_secret(ciphertext)
    assert sentinel_plaintext not in str(excinfo.value)
    assert sentinel_key not in str(excinfo.value)


def test_broker_config_error_is_subclass_of_runtime_error():
    """``BrokerConfigError`` is the project's named exception --
    callers should be able to catch ``RuntimeError`` and still get
    these, matching ``src/upstox_config.py``'s own contract."""
    import broker_token_crypto

    assert issubclass(broker_token_crypto.BrokerConfigError, RuntimeError)


def test_qa_story10_acceptance_criteria_all_in_one(monkeypatch):
    """STORY-10 QA verification: exercise every acceptance criterion
    from the story in one focused test, hitting the *public*
    ``encrypt_secret`` / ``decrypt_secret`` surface end-to-end so we
    verify what the rest of the codebase will actually use, not just
    internals. Distinct from the existing tests because it asserts
    the full criterion set in one place against the live, currently-
    imported module.

    Criteria covered:
      1. ASCII round-trip: decrypt(encrypt(s)) == s
      2. Unicode round-trip: decrypt(encrypt(s)) == s (non-ASCII,
         multi-byte, mixed scripts)
      3. Ciphertext != plaintext, and ciphertext changes across calls
         (random per-message IV from Fernet)
      4. Wrong-key decryption raises BrokerConfigError, NOT garbled
         plaintext, and the low-level InvalidToken is preserved as
         __cause__ for debugging
      5. The module is importable only when BROKER_TOKEN_ENCRYPTION_KEY
         is set to a real Fernet key -- exercising the env var by
         name (which is the contract the rest of the codebase cares
         about)
    """
    import subprocess
    import sys

    # Step 1+2+3: ASCII and unicode round-trip with the live module,
    # under a freshly generated Fernet key set in the env so the
    # story's "use this env var" contract is genuinely exercised.
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", key)
    mod = _reload_module()

    ascii_plaintext = "upstox-access-token-ASCII-abc123XYZ"
    unicode_plaintext = "broker-token-\u4e2d\u6587-mixed-\U0001F600-scripts"

    ascii_ct = mod.encrypt_secret(ascii_plaintext)
    unicode_ct = mod.encrypt_secret(unicode_plaintext)

    # Ciphertext must not equal plaintext (and must not contain it).
    assert ascii_ct != ascii_plaintext
    assert ascii_plaintext not in ascii_ct
    assert unicode_ct != unicode_plaintext
    assert unicode_plaintext not in unicode_ct

    # Round-trip must recover the original for both ASCII and unicode.
    assert mod.decrypt_secret(ascii_ct) == ascii_plaintext
    assert mod.decrypt_secret(unicode_ct) == unicode_plaintext

    # Two encryptions of the same plaintext must produce different
    # ciphertexts (Fernet's per-message random IV), and both must
    # still decrypt back to the original.
    ct_a = mod.encrypt_secret(ascii_plaintext)
    ct_b = mod.encrypt_secret(ascii_plaintext)
    assert ct_a != ct_b
    assert mod.decrypt_secret(ct_a) == ascii_plaintext
    assert mod.decrypt_secret(ct_b) == ascii_plaintext

    # Step 4: encrypt under key A, reload module under key B, decrypt
    # must raise BrokerConfigError -- never silently return a wrong
    # string. The original InvalidToken must be preserved as __cause__
    # so debugging has access to the low-level signal.
    key_a = Fernet.generate_key().decode()
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", key_a)
    mod_a = _reload_module()
    sentinel = "real-broker-token-under-key-a"
    ciphertext_under_a = mod_a.encrypt_secret(sentinel)

    key_b = Fernet.generate_key().decode()
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", key_b)
    mod_b = _reload_module()

    with pytest.raises(mod_b.BrokerConfigError) as excinfo:
        mod_b.decrypt_secret(ciphertext_under_a)
    # The error message must name the env var so an operator reading
    # it knows what to fix, and must NOT echo the plaintext token.
    err_msg = str(excinfo.value)
    assert "BROKER_TOKEN_ENCRYPTION_KEY" in err_msg
    assert sentinel not in err_msg
    # Low-level InvalidToken must be the __cause__.
    from cryptography.fernet import InvalidToken
    assert isinstance(excinfo.value.__cause__, InvalidToken)

    # Step 5: the env var name must be exactly BROKER_TOKEN_ENCRYPTION_KEY
    # (not e.g. FERNET_KEY, ENCRYPTION_KEY, etc. -- the contract the
    # rest of the codebase relies on).
    result = subprocess.run(
        [sys.executable, "-c",
         "import os; assert 'BROKER_TOKEN_ENCRYPTION_KEY' not in os.environ; "
         "import broker_token_crypto"],
        env={k: v for k, v in os.environ.items() if k != "BROKER_TOKEN_ENCRYPTION_KEY"},
        capture_output=True,
        text=True,
        cwd="src",
    )
    assert result.returncode != 0, (
        "module imported with no BROKER_TOKEN_ENCRYPTION_KEY set"
    )
    combined = result.stdout + result.stderr
    assert "BrokerConfigError" in combined
    assert "BROKER_TOKEN_ENCRYPTION_KEY" in combined
    # The exact key-generation command from the story's acceptance
    # criteria must be in the message.
    assert _KEY_GEN_COMMAND in combined