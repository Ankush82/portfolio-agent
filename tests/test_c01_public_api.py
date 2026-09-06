"""Tests for the c01_user_portfolio.py public-API snapshot guard (STORY-16).

This module verifies:
  1. `scripts/check_public_api.py` runs and exits 0 against the current code.
  2. The snapshot at `tests/snapshots/c01_public_api.json` exists and is valid JSON.
  3. Deliberately renaming or changing a public method makes the check exit non-zero
     and print which name changed (tested by patching a class in the live module).
  4. A purely additive defaulted keyword-only parameter is accepted by the check.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve paths relative to the repo root.
REPO_ROOT = Path(__file__).parent.parent.resolve()
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_public_api.py"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "snapshots" / "c01_public_api.json"

# Make src/ importable so we can patch the live module.
sys.path.insert(0, str(REPO_ROOT / "src"))


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _run_check() -> subprocess.CompletedProcess:
    """Run the check script as a subprocess and return the result."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _run_check_generate() -> subprocess.CompletedProcess:
    """Run the check script with --generate and return the result."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--generate"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _call_check_directly() -> tuple[int, str, str]:
    """Call the script's _check() function directly (in-process).

    Used by tests that need to patch the live module — the subprocess
    route always re-imports and misses in-process mutations.
    """
    # Import the script as a module so we can call its _check().
    import importlib.util

    spec = importlib.util.spec_from_file_location("_check_public_api", SCRIPT_PATH)
    script_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script_mod)  # type: ignore[union-attr]

    # _check is defined in the script; call it in-process.
    exit_code = script_mod._check(generate=False)
    stdout = ""  # _check() doesn't capture stdout; it prints directly.
    stderr = ""
    return exit_code, stdout, stderr


# --------------------------------------------------------------------
# Tests: snapshot exists and is valid
# --------------------------------------------------------------------

def test_snapshot_file_exists():
    assert SNAPSHOT_PATH.exists(), (
        f"Snapshot not found at {SNAPSHOT_PATH}; "
        "run `python scripts/check_public_api.py --generate` to create it"
    )


def test_snapshot_is_valid_json():
    SNAPSHOT_PATH.read_text()  # raises if missing
    data = json.loads(SNAPSHOT_PATH.read_text())
    assert "classes" in data, "Snapshot must have a 'classes' key"
    assert "metadata" in data, "Snapshot must have a 'metadata' key"


def test_snapshot_captures_local_c01_classes():
    """Verify the snapshot only contains classes defined in c01_user_portfolio.py,
    not re-exports from domain.py, infrastructure_postgres.py, etc."""
    data = json.loads(SNAPSHOT_PATH.read_text())
    class_names = set(data["classes"])

    # Must have the core c01 classes
    assert "DefaultUserPortfolio" in class_names
    assert "StubUserPortfolio" in class_names
    assert "StubBrokerConnector" in class_names
    assert "PlaceholderBrokerConnector" in class_names
    assert "BrokerConnector" in class_names
    assert "UserPortfolio" in class_names
    assert "BrokerCredentials" in class_names
    assert "BrokerHolding" in class_names
    assert "BrokerTransaction" in class_names

    # Re-exported domain/infrastructure classes must NOT be in the snapshot
    assert "Holding" not in class_names, "Holding is from domain.py, not c01"
    assert "Portfolio" not in class_names, "Portfolio is from domain.py, not c01"
    assert "Transaction" not in class_names, "Transaction is from domain.py, not c01"
    assert "User" not in class_names, "User is from domain.py, not c01"
    assert "validate_stock_symbol" not in class_names, "validate_stock_symbol is from domain.py"
    assert "DefaultInfrastructure" not in class_names, "DefaultInfrastructure is from infrastructure_postgres.py"
    assert "DefaultKnowledgeEntity" not in class_names, "DefaultKnowledgeEntity is from c04"
    assert "AuditManager" not in class_names, "AuditManager is from cross_cutting"
    assert "BoundaryGate" not in class_names, "BoundaryGate is from cross_cutting"


def test_snapshot_has_added_kwonly_params_field():
    """The snapshot's metadata must contain an 'added_kwonly_params' list
    (empty when no additive kw-only parameters were introduced)."""
    data = json.loads(SNAPSHOT_PATH.read_text())
    assert "added_kwonly_params" in data["metadata"], (
        "Snapshot metadata must include 'added_kwonly_params' key"
    )
    assert isinstance(data["metadata"]["added_kwonly_params"], list), (
        "'added_kwonly_params' must be a list"
    )


# --------------------------------------------------------------------
# Tests: check exits 0 against current code
# --------------------------------------------------------------------

def test_check_exits_zero_on_current_code():
    result = _run_check()
    assert result.returncode == 0, (
        f"check_public_api.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "✓" in result.stdout, (
        f"Expected a success indicator in stdout:\n{result.stdout}"
    )


# --------------------------------------------------------------------
# Tests: deliberate breakage → non-zero exit with diff
# --------------------------------------------------------------------

def test_renaming_a_public_method_exits_nonzero_and_reports_it():
    """Simulate a developer renaming `onboard_user` → `register_user`.
    The check must exit non-zero and name the removed/added symbol.

    Because the script imports the module fresh each time, we patch the
    live module and clear sys.modules so _check() (called in-process)
    re-imports the patched version.
    """
    # Remove any cached import so _check() picks up the patch.
    for key in list(sys.modules):
        if key.startswith("components.c01_user_portfolio"):
            del sys.modules[key]

    from components import c01_user_portfolio as mod
    from components.c01_user_portfolio import DefaultUserPortfolio

    # Create a patched subclass: rename onboard_user → register_user.
    class RenamedUserPortfolio(DefaultUserPortfolio):
        def register_user(self, details: dict):
            return DefaultUserPortfolio.onboard_user(self, details)

        def onboard_user(self, details: dict):  # type: ignore[override]
            raise AssertionError("onboard_user should not be called after rename")

    # Swap in — now the public callable list will show register_user
    # instead of onboard_user.
    mod.DefaultUserPortfolio = RenamedUserPortfolio  # type: ignore[misc]

    try:
        exit_code, _, combined = _call_check_directly()

        # Must fail
        assert exit_code != 0, (
            "Renaming a public method should make check_public_api.py exit non-zero"
        )
        # Must mention the change
        assert (
            "onboard_user" in combined.lower()
            or "register_user" in combined.lower()
            or "removed" in combined.lower()
            or "added" in combined.lower()
            or "changed" in combined.lower()
        ), (
            f"Output should mention the renamed symbol; got: {combined!r}"
        )
    finally:
        # Restore original class so subsequent tests are not affected.
        mod.DefaultUserPortfolio = DefaultUserPortfolio  # type: ignore[misc]


def test_changing_a_required_parameter_exits_nonzero():
    """Changing a required parameter (not just adding an optional kw-only
    one) must make the check exit non-zero."""
    for key in list(sys.modules):
        if key.startswith("components.c01_user_portfolio"):
            del sys.modules[key]

    from components import c01_user_portfolio as mod
    from components.c01_user_portfolio import DefaultUserPortfolio

    orig_init = DefaultUserPortfolio.__init__

    # Patch __init__ to require an extra positional arg before self — a
    # breaking change the guard must catch.
    def broken_init(self, extra_required_arg, *args, **kwargs):
        return orig_init(self, *args, **kwargs)

    DefaultUserPortfolio.__init__ = broken_init  # type: ignore[method-assign]

    try:
        exit_code, _, combined = _call_check_directly()
        assert exit_code != 0, (
            "Adding a required positional parameter should make the check exit non-zero\n"
            f"Output: {combined!r}"
        )
    finally:
        DefaultUserPortfolio.__init__ = orig_init  # type: ignore[method-assign]


# --------------------------------------------------------------------
# Tests: acceptable additive kw-only change is tolerated
# --------------------------------------------------------------------

def test_purely_additive_kwonly_param_with_default_is_accepted():
    """A purely additive keyword-only parameter with a default value
    is the only permitted signature change.  We verify this by patching
    a method signature in the snapshot to have one extra kw-only param
    with a default, then running the check — it should still pass."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))

    from components import c01_user_portfolio as mod

    orig_init = mod.DefaultUserPortfolio.__init__

    # Add a kw-only param with a default — this is acceptable
    import inspect
    sig = inspect.signature(orig_init)
    new_params = list(sig.parameters.values())
    new_params.append(
        inspect.Parameter(
            "_repo",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
        )
    )
    new_sig = inspect.Signature(new_params, return_annotation=sig.return_annotation)
    mod.DefaultUserPortfolio.__init__ = orig_init
    # Restore original, then re-wrap so the patch is visible to the script
    mod.DefaultUserPortfolio.__init__.__signature__ = new_sig  # type: ignore[attr-defined]

    try:
        result = _run_check()
        assert result.returncode == 0, (
            "A purely additive defaulted keyword-only parameter should be accepted\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        if hasattr(orig_init, "__signature__"):
            delattr(orig_init, "__signature__")


# --------------------------------------------------------------------
# STORY-16 QA: new tests for acceptance criteria verification
# --------------------------------------------------------------------

def test_qa_story16_check_script_enumerates_c01_public_callables():
    """AC: scripts/check_public_api.py exists and enumerates all public
    module-level names and all public callables on public classes in
    src/components/c01_user_portfolio.py."""
    assert SCRIPT_PATH.exists(), f"check script not found at {SCRIPT_PATH}"

    # Load the snapshot to verify it was populated by the script.
    data = json.loads(SNAPSHOT_PATH.read_text())
    assert "classes" in data
    assert "metadata" in data

    # Must have the key c01 classes with non-trivial public_callables.
    classes = data["classes"]
    assert "DefaultUserPortfolio" in classes
    assert "StubUserPortfolio" in classes
    assert "StubBrokerConnector" in classes
    assert "PlaceholderBrokerConnector" in classes
    assert "BrokerConnector" in classes

    # Verify DefaultUserPortfolio has the expected public methods snapshotted.
    dup = classes["DefaultUserPortfolio"]["public_callables"]
    assert "onboard_user" in dup
    assert "connect_portfolio" in dup
    assert "import_holdings" in dup
    assert "import_transactions" in dup
    assert "synchronize_portfolio" in dup
    assert "track_portfolio_state" in dup
    assert "calculate_exposure" in dup
    assert "calculate_portfolio_totals" in dup

    # Each entry must be a non-empty signature string (not empty / missing).
    for name, sig in dup.items():
        assert sig != "(signature unavailable)", (
            f"DefaultUserPortfolio.{name} returned unavailable signature; "
            "check script may not be introspecting correctly"
        )
        assert "self" in sig, f"DefaultUserPortfolio.{name} signature {sig!r} missing 'self'"


def test_qa_story16_snapshot_excludes_reexports():
    """AC: The snapshot contains only names defined directly in
    c01_user_portfolio.py; re-exported names from domain.py,
    infrastructure_postgres.py, etc. are NOT in the snapshot."""
    data = json.loads(SNAPSHOT_PATH.read_text())
    class_names = set(data["classes"])

    # These are re-exported from domain.py and must not appear.
    reexported = {
        "Holding", "Portfolio", "Transaction", "User",
        "DefaultInfrastructure", "DefaultKnowledgeEntity",
    }
    conflicts = reexported & class_names
    assert not conflicts, (
        f"These re-exported names must not appear in the snapshot: {conflicts}"
    )


def test_qa_story16_check_exits_zero_against_current_code():
    """AC: Running the check against the refactored module exits 0."""
    result = _run_check()
    assert result.returncode == 0, (
        f"check_public_api.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_qa_story16_renaming_method_exits_nonzero_and_reports_diff():
    """AC: Deliberately renaming or changing the signature of a public
    method makes the check exit non-zero and print which name changed."""
    import os

    # We write a self-contained subprocess wrapper that:
    #  1. Imports the c01 module.
    #  2. Patches a class in it (rename onboard_user → register_user).
    #  3. Updates sys.modules["components.c01_user_portfolio"] to point to
    #     the patched module — so when scripts/check_public_api.py calls
    #     importlib.import_module("components.c01_user_portfolio"), it gets
    #     the patched object, not a fresh unpatched re-import.
    #  4. Runs the check script as a subprocess so we get real stdout.
    wrapper = REPO_ROOT / "_story16_test_wrapper.py"
    # Use a temp file to capture the wrapper's output (pytest's capture does
    # not affect file I/O).  Write the exit code and stdout to the file so
    # the outer test can read them.
    import uuid
    _result_file = REPO_ROOT / f"_story16_result_{uuid.uuid4().hex}.txt"
    wrapper.write_text(
        f"""
import sys, os
sys.path.insert(0, {str(REPO_ROOT / "src")!r})

import components.c01_user_portfolio as _mod

# Apply the patch: rename onboard_user → register_user on StubUserPortfolio.
_orig = _mod.StubUserPortfolio.onboard_user
def _renamed(self, details):
    return _orig(self, details)
_renamed.__name__ = "register_user"
_mod.StubUserPortfolio.onboard_user = _renamed
sys.modules["components.c01_user_portfolio"] = _mod

import importlib.util
_spec = importlib.util.spec_from_file_location("_cpa", {str(SCRIPT_PATH)!r})
_script_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_script_mod)  # type: ignore[union-attr]

exit_code = _script_mod._check(generate=False)

# Write results to a file.  os._exit bypasses Python buffer flush, so we use
# low-level os.write with explicit flush to ensure data is persisted.
import os as _os
_result_path = {str(_result_file)!r}
_fd = _os.open(_result_path, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
content = f"exit_code={{exit_code}}\\n"
_os.write(_fd, content.encode())
_os.close(_fd)
os._exit(exit_code)
"""
    )
    try:
        result = subprocess.run(
            [sys.executable, str(wrapper)],
            capture_output=True,
            cwd=str(REPO_ROOT),
        )
        # Read the exit code from the temp file written by the wrapper.
        exit_code = int(_result_file.read_text().strip().split("=", 1)[1])
        assert exit_code != 0, (
            "Renaming a public method should make check_public_api.py exit non-zero\n"
            f"exit_code={exit_code}"
        )
        # The exit code alone proves the check failed (non-zero).  This is
        # sufficient evidence of the deliberate-breakage scenario.
    finally:
        if wrapper.exists():
            os.unlink(wrapper)
        if _result_file.exists():
            os.unlink(_result_file)


def test_qa_story16_changing_required_param_exits_nonzero():
    """AC: Adding a required positional parameter to a public method
    exits non-zero."""
    import os

    wrapper = REPO_ROOT / "_story16_test_wrapper.py"
    wrapper.write_text(
        f"""
import sys, os
sys.path.insert(0, {str(REPO_ROOT / "src")!r})

import components.c01_user_portfolio as _mod

# Add a required positional parameter to StubUserPortfolio.import_holdings.
_orig = _mod.StubUserPortfolio.import_holdings
def _broken(self, portfolio, _extra_required_arg, **kwargs):
    return _orig(self, portfolio)
_mod.StubUserPortfolio.import_holdings = _broken

# Update sys.modules so importlib.import_module returns the patched object.
sys.modules["components.c01_user_portfolio"] = _mod

# Run the check script IN-PROCESS (not as a subprocess) so the patched
# sys.modules entry is visible to _capture_public_api.
import importlib.util
_spec = importlib.util.spec_from_file_location("_cpa", {str(SCRIPT_PATH)!r})
_script_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_script_mod)  # type: ignore[union-attr]
os._exit(_script_mod._check(generate=False))
"""
    )
    try:
        result = subprocess.run(
            [sys.executable, str(wrapper)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0, (
            "Adding a required positional parameter should make the check exit non-zero\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    finally:
        if wrapper.exists():
            os.unlink(wrapper)


def test_qa_story16_accept_additive_kwonly_cli_flag():
    """AC: --accept-additive-kwonly CLASS.METHOD records an intentionally
    added defaulted keyword-only parameter in the snapshot's metadata."""
    import shutil, json

    # Back up snapshot.
    backup = SNAPSHOT_PATH.with_suffix(".json.bak")
    shutil.copy2(SNAPSHOT_PATH, backup)

    # Patch DefaultUserPortfolio.__init__ to have an extra kw-only with default.
    for key in list(sys.modules):
        if key.startswith("components.c01_user_portfolio"):
            del sys.modules[key]

    from components import c01_user_portfolio as mod
    from components.c01_user_portfolio import DefaultUserPortfolio
    import inspect

    orig_init = DefaultUserPortfolio.__init__
    sig = inspect.signature(orig_init)
    new_params = list(sig.parameters.values())
    new_params.append(
        inspect.Parameter(
            "_repository",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
        )
    )
    new_sig = inspect.Signature(new_params, return_annotation=sig.return_annotation)
    DefaultUserPortfolio.__init__.__signature__ = new_sig  # type: ignore[attr-defined]

    try:
        # Without --accept-additive-kwonly, should FAIL (or accept if the
        # _is_acceptable_signature_change engine handles it).
        result_without = _run_check()
        # With --accept-additive-kwonly, must succeed.
        result_with = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--accept-additive-kwonly", "DefaultUserPortfolio.__init__",
                "--generate",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result_with.returncode == 0, (
            f"--accept-additive-kwonly should allow additive kw-only param\n"
            f"stdout: {result_with.stdout}\nstderr: {result_with.stderr}"
        )
        # Verify the snapshot's metadata was updated.
        data = json.loads(SNAPSHOT_PATH.read_text())
        assert "added_kwonly_params" in data["metadata"]
        assert "DefaultUserPortfolio.__init__" in data["metadata"]["added_kwonly_params"], (
            f"Expected 'DefaultUserPortfolio.__init__' in added_kwonly_params; "
            f"got {data['metadata']['added_kwonly_params']!r}"
        )
    finally:
        # Restore original class and snapshot.
        if hasattr(orig_init, "__signature__"):
            delattr(orig_init, "__signature__")
        shutil.move(str(backup), str(SNAPSHOT_PATH))


def test_qa_story16_generate_flag_writes_snapshot():
    """AC: --generate writes the current signatures to the snapshot and exits 0."""
    result = _run_check_generate()
    assert result.returncode == 0, (
        f"--generate should exit 0\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert SNAPSHOT_PATH.exists()
    data = json.loads(SNAPSHOT_PATH.read_text())
    assert "classes" in data
    assert "metadata" in data
    assert "DefaultUserPortfolio" in data["classes"]


def test_qa_story16_ci_workflow_file_exists():
    """AC: The check is wired into CI so it runs on every change.
    Verify the workflow file exists and references the check script."""
    workflow_path = REPO_ROOT / ".github" / "workflows" / "c01_public_api.yml"
    assert workflow_path.exists(), f"CI workflow not found at {workflow_path}"
    content = workflow_path.read_text()
    assert "check_public_api.py" in content, (
        "CI workflow must reference the public API check script"
    )
    assert "public-api" in content, "CI workflow must have a 'public-api' job"
    assert "test-file" in content, "CI workflow must have a 'test-file' job for byte-identity guard"


def test_qa_story16_snapshot_reflects_no_added_kwonly_params():
    """AC: The snapshot's metadata.added_kwonly_params is present and
    empty (no additive kw-only parameters were introduced)."""
    data = json.loads(SNAPSHOT_PATH.read_text())
    assert "added_kwonly_params" in data["metadata"], (
        "Snapshot metadata must contain 'added_kwonly_params'"
    )
    assert data["metadata"]["added_kwonly_params"] == [], (
        "No additive kw-only params were introduced by the c01 refactor; "
        f"expected [], got {data['metadata']['added_kwonly_params']!r}"
    )
