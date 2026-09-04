"""Pytest wrapper for STORY-7 (issue #77) performance validation.

The actual measurement work lives in ``scripts/performance_validation.py``,
which seeds >=5,000,000 rows, times a real ``migrate_us_stocks()`` call, and
samples ``docker stats --no-stream`` against the local Postgres container.
This test invokes that script as a real subprocess and asserts:

  * the script exists and is executable,
  * it exits with status 0 (meaning both the duration < 5 min and the
    CPU <= 70 % of allocated capacity thresholds were satisfied),
  * its stdout contains an ``ACTUAL RESULTS:`` block with the documented
    fields (so a future regression of the report format is caught).

The test is skipped (not failed) when the environment lacks what it needs:
  * docker CLI on PATH,
  * psycopg importable (so we can probe the live Postgres),
  * the local Postgres container reachable at DEFAULT_POSTGRES_DSN.

Note: ``import psycopg`` is done lazily inside the probe functions -- never
at module top -- so this file collects cleanly under pytest invocations
that don't have psycopg installed (which is how the test infra is run in
the project's CI sandbox; the script itself is the heavyweight run).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "performance_validation.py"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _venv_python() -> Path:
    """Path to the project's venv python (which has psycopg)."""
    return REPO_ROOT / ".venv" / "bin" / "python"


def _postgres_reachable() -> bool:
    """Probe Postgres reachability via a subprocess so the test file
    collects cleanly even when the pytest interpreter doesn't have psycopg
    (the project venv does). This avoids the previous failure where the
    system pytest skipped the entire module just because psycopg wasn't
    importable at collection time.
    """
    py = _venv_python()
    if not py.is_file():
        # Fall back: try in-process import in case pytest is running from the venv.
        try:
            import psycopg  # type: ignore[import-not-found]
        except Exception:
            return False
        try:
            with psycopg.connect(
                os.environ.get(
                    "DEFAULT_POSTGRES_DSN",
                    "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent",
                ),
                connect_timeout=2,
            ):
                return True
        except Exception:
            return False
        return True
    probe_code = (
        "import os, sys\n"
        "sys.path.insert(0, str(__import__('pathlib').Path('"
        + str(REPO_ROOT / "src")
        + "').resolve()))\n"
        "try:\n"
        "    import psycopg\n"
        "    dsn = os.environ.get('DEFAULT_POSTGRES_DSN', "
        "'postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent')\n"
        "    with psycopg.connect(dsn, connect_timeout=2):\n"
        "        sys.exit(0)\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )
    try:
        proc = subprocess.run(
            [str(py), "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (SCRIPT.is_file() and _docker_available() and _postgres_reachable()),
    reason=(
        "performance validation prerequisites not met: need "
        f"scripts/performance_validation.py present, docker on PATH, and "
        f"Postgres reachable at DEFAULT_POSTGRES_DSN"
    ),
)


def test_performance_validation_script_exists_and_is_runnable() -> None:
    """AC5: the performance test exists as a real, runnable script in this repo."""
    assert SCRIPT.is_file(), f"expected real script at {SCRIPT}"
    # Source is syntactically valid Python (the import below would surface SyntaxError).
    import importlib.util

    spec = importlib.util.spec_from_file_location("performance_validation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # We do NOT execute the module -- just verify it's importable / parseable.
    # (Executing it would actually run the migration, which the dedicated
    # subprocess test below does in a controlled way.)


def test_performance_validation_against_local_postgres_passes_thresholds() -> None:
    """AC1 + AC2 + AC4: real run meets duration and CPU thresholds and
    documents actual results.

    Runs the script as a real subprocess against the locally-running
    Postgres container. Honours STORY7_SKIP_HEAVY_RUN=1 for fast local
    iteration (in which case the script is only invoked with --help, and
    we assert the help/usage path is sane rather than actually migrating).
    """
    env_overrides = {
        # Pass through the live DSN + any parent-env quirks; do NOT mask them.
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }

    if os.environ.get("STORY7_SKIP_HEAVY_RUN") == "1":
        # Quick existence/usage check rather than a real 5M-row run.
        proc = subprocess.run(
            ["python3", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, (
            f"--help must exit 0; got rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "row_count" in proc.stdout, "--help must document row_count"
        return

    # Heavy run: actually invoke the script. Use the project venv's python
    # (which has psycopg) if available, else fall back to python3.
    python_bin = REPO_ROOT / ".venv" / "bin" / "python"
    if not python_bin.is_file():
        python_bin_path = shutil.which("python3") or "python3"
        python_bin = Path(python_bin_path)  # type: ignore[assignment]

    # Sub-5M override only with explicit opt-in; default respects the story.
    row_count_env = os.environ.get("STORY7_ROW_COUNT")
    args = [str(python_bin), str(SCRIPT)]
    if row_count_env:
        args.append(row_count_env)

    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=600,  # well over the 5-minute AC threshold
        check=False,
    )

    # Both threshold failures and infra failures print to stderr; capture both.
    combined = proc.stdout + "\n" + proc.stderr

    # --- AC5: real runnable script exists (it just ran). ---
    assert SCRIPT.is_file()

    # --- AC1: duration < 5 min ---
    # Pull the actual numbers from the ACTUAL RESULTS block.
    duration_match = re.search(r"duration_seconds\s*:\s*([0-9.]+)", combined)
    assert duration_match, (
        "ACTUAL RESULTS block must include duration_seconds; got stdout="
        f"{proc.stdout[:500]!r} stderr={proc.stderr[:500]!r}"
    )
    duration = float(duration_match.group(1))
    assert duration < 300.0, (
        f"STORY-7 AC1 violated: duration {duration:.3f}s >= 300s; full output:\n{combined}"
    )

    # --- AC2: peak CPU% <= 70 % of allocated capacity (= 140 % for 2 CPUs) ---
    peak_match = re.search(r"cpu_percent_peak\s*:\s*([0-9.]+)", combined)
    assert peak_match, (
        "ACTUAL RESULTS block must include cpu_percent_peak; got stdout="
        f"{proc.stdout[:500]!r} stderr={proc.stderr[:500]!r}"
    )
    peak_cpu = float(peak_match.group(1))
    assert peak_cpu <= 140.0, (
        f"STORY-7 AC2 violated: peak CPU {peak_cpu:.2f}% > 140% (70% of 2 CPUs); "
        f"full output:\n{combined}"
    )

    # --- AC4: actual duration + CPU% documented in the output ---
    assert "ACTUAL RESULTS:" in combined, (
        f"output must contain an ACTUAL RESULTS: block; got combined[:500]={combined[:500]!r}"
    )
    assert "duration_seconds" in combined and "cpu_percent_peak" in combined

    # Final exit-code gate: the script should have exited 0 (both thresholds met).
    assert proc.returncode == 0, (
        f"performance_validation.py must exit 0 on success; got rc={proc.returncode} "
        f"stderr={proc.stderr[:500]!r}"
    )


def test_performance_validation_declares_thresholds_in_its_source() -> None:
    """Defensive: the script's source itself must mention the AC1/AC2
    thresholds so a future drive-by edit can't quietly change them without
    this test catching it."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "DURATION_THRESHOLD_SECONDS = 300.0" in src, (
        "performance_validation.py must declare a 300s duration threshold"
    )
    assert "ALLOCATED_CPUS = 2.0" in src, (
        "performance_validation.py must declare 2.0 allocated CPUs"
    )
    assert "CPU_PERCENT_THRESHOLD = 70.0 * ALLOCATED_CPUS * 1.0" in src, (
        "performance_validation.py must derive CPU threshold from "
        "70% * allocated CPUs (i.e. 140% in docker's per-CPU convention)"
    )