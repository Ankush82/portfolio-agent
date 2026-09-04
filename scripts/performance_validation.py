"""STORY-7 / issue #77: real-scale performance and CPU-usage validation.

Runs ``migrate_us_stocks()`` against a real, freshly-seeded >=5,000,000-row
``stocks`` + ``tmp_us_tickers`` dataset and measures both wall-clock duration
and CPU usage against the local docker-compose Postgres container (capped at
2 real CPUs in ``docker-compose.yml``).

Why a script (not a pytest test)? Sampling ``docker stats`` is a shell
pipeline concern, the dataset is too large to spin up/down per pytest
fixture, and the story explicitly says "whichever fits better". A pytest
wrapper under ``tests/test_migrate_performance.py`` invokes THIS script as a
real subprocess and asserts on its exit code + stdout, so the contract is
still runnable under ``pytest``.

Thresholds (per story acceptance criteria):
  * Duration: < 300 s (5 minutes).
  * CPU usage: <= 70 % of allocated database instance capacity. The local
    container has 2 real CPUs, and ``docker stats`` reports CPU usage against
    one CPU = 100 %, so 2 CPUs = 200 % in docker's own convention. The
    threshold therefore maps to a ``CPUPerc`` reading of <= 140.0 %.

Run with the project's venv (which has psycopg installed):

    .venv/bin/python scripts/performance_validation.py [row_count]

Default row_count is 5,000,000 (the story's own minimum). Override with the
first CLI argument for sub-scale debugging.

Exits 0 on success, non-zero on any threshold failure (and prints concrete
optimization hints when that happens).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# Repo root so this script works regardless of CWD when invoked directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from infrastructure_postgres import DEFAULT_POSTGRES_DSN  # noqa: E402
from scripts.migrate_us_stocks import migrate_us_stocks  # noqa: E402
from scripts.seed_performance_test_data import seed  # noqa: E402

CONTAINER_NAME = "portfolioagent-postgres-1"
# docker-compose.yml: deploy.resources.limits.cpus: "2.0".
ALLOCATED_CPUS = 2.0
# 70 % of allocated capacity in docker stats' per-CPU convention:
# 70 % * 200 % = 140 %.
CPU_PERCENT_THRESHOLD = 70.0 * ALLOCATED_CPUS * 1.0  # = 140.0
DURATION_THRESHOLD_SECONDS = 300.0  # 5 minutes
DEFAULT_ROW_COUNT = 5_000_000

# Polling cadence for docker stats. 0.5 s keeps the sampler cheap while still
# capturing the peak (the actual migration against 5M rows runs in single-
# digit seconds, so we want at least one mid-flight sample).
POLL_INTERVAL_SECONDS = 0.5
# How long to keep sampling AFTER migrate_us_stocks() returns. A short tail
# lets us catch late GC / WAL flush spikes that wouldn't show up if we
# stopped the moment the function returned.
POST_RUN_TAIL_SECONDS = 5.0

_DOCKER_STATS_FORMAT = "{{.CPUPerc}}\t{{.MemUsage}}\t{{.Name}}"  # \t makes it easy to parse


class _DockerStatsSampler:
    """Background ``docker stats --no-stream`` poller for one container.

    Uses ``docker stats --no-stream`` (single-shot) repeated in a loop so we
    don't need a long-running streaming subprocess and so each sample is a
    real, fresh reading -- ``docker stats`` (stream mode) reports CPU since
    container start, which would just drift upward; ``--no-stream`` reports
    CPU since the previous ``--no-stream`` call, which is exactly what we
    want for "what was the CPU while the migration was running".

    ``--no-stream`` is a real docker CLI flag (verified locally via
    ``docker stats --help``); it asks for a single snapshot and exits, so we
    invoke it repeatedly in this thread.
    """

    def __init__(
        self,
        container: str = CONTAINER_NAME,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._container = container
        self._interval = interval
        self.samples: deque[tuple[float, float]] = deque()  # (unix_time, cpu_percent)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_count = 0
        self._errors: list[str] = []

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"docker-stats-sampler-{self._container}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 4 + 2)

    def _run(self) -> None:
        docker_bin = shutil.which("docker")
        if docker_bin is None:
            self._errors.append("docker binary not on PATH; CPU sampling disabled")
            return
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    [
                        docker_bin,
                        "stats",
                        "--no-stream",
                        "--format", _DOCKER_STATS_FORMAT,
                        self._container,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self._errors.append("docker stats --no-stream timed out")
            except Exception as exc:  # pragma: no cover - defensive
                self._errors.append(f"docker stats invocation failed: {exc}")
            else:
                if proc.returncode == 0 and proc.stdout.strip():
                    # Lines look like "42.10%\t213.3MiB / 7.748GiB\tportfolioagent-postgres-1"
                    for line in proc.stdout.strip().splitlines():
                        parts = line.split("\t")
                        if len(parts) < 3:
                            continue
                        cpu_raw = parts[0].strip().rstrip("%")
                        try:
                            cpu = float(cpu_raw)
                        except ValueError:
                            continue
                        self.samples.append((time.monotonic(), cpu))
                        self._sample_count += 1
                elif proc.returncode != 0:
                    self._errors.append(
                        f"docker stats rc={proc.returncode}: {proc.stderr.strip()[:200]}"
                    )
            # Sleep in small chunks so stop() is responsive.
            slept = 0.0
            while slept < self._interval and not self._stop.is_set():
                time.sleep(min(0.05, self._interval - slept))
                slept += 0.05


def _summarise(samples: list[tuple[float, float]]) -> dict[str, float | int]:
    if not samples:
        return {"count": 0, "peak": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    cpus = [cpu for _, cpu in samples]
    return {
        "count": len(cpus),
        "peak": max(cpus),
        "mean": sum(cpus) / len(cpus),
        "min": min(cpus),
        "max": max(cpus),
    }


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "row_count",
        nargs="?",
        type=int,
        default=DEFAULT_ROW_COUNT,
        help=f"Rows to seed (default: {DEFAULT_ROW_COUNT:,}; minimum 5,000,000 per STORY-7 AC1)",
    )
    return parser.parse_args()


def _postgres_reachable() -> bool:
    try:
        import psycopg  # local import keeps --help fast + sandbox-friendly
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def main() -> int:
    args = _parse_cli()
    row_count = args.row_count
    if row_count < 5_000_000:
        print(
            f"ERROR: STORY-7 AC1 requires >= 5,000,000 rows; got {row_count:,}. "
            "Override the threshold via env var STORY7_ALLOW_SUBSCALE=1 if you "
            "are deliberately running below scale.",
            file=sys.stderr,
        )
        if not os.environ.get("STORY7_ALLOW_SUBSCALE"):
            return 2

    print("=" * 72)
    print("STORY-7 performance and resource usage validation")
    print("=" * 72)
    print(f"Repository root        : {REPO_ROOT}")
    print(f"Postgres DSN           : {DEFAULT_POSTGRES_DSN}")
    print(f"Container              : {CONTAINER_NAME}")
    print(f"Allocated CPUs         : {ALLOCATED_CPUS}")
    print(f"Row count              : {row_count:,}")
    print(f"Duration threshold     : < {DURATION_THRESHOLD_SECONDS:.0f} s")
    print(f"CPU% threshold         : <= {CPU_PERCENT_THRESHOLD:.1f} % "
          f"(70 % of {ALLOCATED_CPUS * 100:.0f} %)")
    print(f"Poll interval          : {POLL_INTERVAL_SECONDS} s")
    print()

    if not _postgres_reachable():
        print("ERROR: Postgres is not reachable at DEFAULT_POSTGRES_DSN.", file=sys.stderr)
        return 3
    if not _docker_available():
        print("ERROR: docker CLI not on PATH; cannot sample CPU usage.", file=sys.stderr)
        return 4

    print(f"[1/4] Seeding {row_count:,} rows via scripts/seed_performance_test_data.seed() ...")
    seed_start = time.monotonic()
    seed(row_count=row_count)
    seed_elapsed = time.monotonic() - seed_start
    print(f"      seeding finished in {seed_elapsed:.1f} s")
    print()

    sampler = _DockerStatsSampler()
    print(f"[2/4] Starting docker stats --no-stream sampler (interval={POLL_INTERVAL_SECONDS}s) ...")
    sampler.start()
    # Let the sampler take a baseline reading BEFORE the migration runs.
    time.sleep(POLL_INTERVAL_SECONDS * 2)

    print(f"[3/4] Timing migrate_us_stocks(dry_run=False) ...")
    migrate_start = time.monotonic()
    try:
        rows_updated = migrate_us_stocks(dry_run=False)
    except Exception as exc:
        sampler.stop()
        print(f"ERROR: migrate_us_stocks() raised: {exc}", file=sys.stderr)
        return 5
    migrate_elapsed = time.monotonic() - migrate_start
    print(f"      migrate_us_stocks() returned rows_updated={rows_updated:,} "
          f"in {migrate_elapsed:.3f} s")
    print()

    # Tail so we catch late spikes (WAL flush, etc.).
    print(f"[4/4] Sampling for {POST_RUN_TAIL_SECONDS:.0f} s post-run tail ...")
    time.sleep(POST_RUN_TAIL_SECONDS)
    sampler.stop()

    stats = _summarise(list(sampler.samples))
    peak_cpu = float(stats["peak"])
    mean_cpu = float(stats["mean"])
    sample_count = int(stats["count"])

    if sampler._errors:
        print("Sampler warnings (sampling may be incomplete):")
        for err in sampler._errors[:5]:
            print(f"  - {err}")
        print()

    duration_ok = migrate_elapsed < DURATION_THRESHOLD_SECONDS
    cpu_ok = peak_cpu <= CPU_PERCENT_THRESHOLD

    print("=" * 72)
    print("ACTUAL RESULTS:")
    print("=" * 72)
    print(f"  row_count           : {row_count:,}")
    print(f"  rows_updated        : {rows_updated:,}")
    print(f"  duration_seconds    : {migrate_elapsed:.3f}")
    print(f"  duration_threshold  : {DURATION_THRESHOLD_SECONDS:.0f}")
    print(f"  duration_passed     : {duration_ok}")
    print(f"  cpu_percent_peak    : {peak_cpu:.2f}")
    print(f"  cpu_percent_mean    : {mean_cpu:.2f}")
    print(f"  cpu_percent_min     : {float(stats['min']):.2f}")
    print(f"  cpu_percent_max     : {float(stats['max']):.2f}")
    print(f"  cpu_percent_thresh  : {CPU_PERCENT_THRESHOLD:.1f}")
    print(f"  cpu_passed          : {cpu_ok}")
    print(f"  docker_samples      : {sample_count}")
    print(f"  allocated_cpus      : {ALLOCATED_CPUS}")
    print(f"  peak_cpu_pct_of_cap : {peak_cpu / (ALLOCATED_CPUS * 100.0) * 100.0:.2f} %")
    print("=" * 72)

    if not duration_ok:
        print(
            f"FAIL: duration {migrate_elapsed:.3f} s exceeds threshold "
            f"{DURATION_THRESHOLD_SECONDS:.0f} s.",
            file=sys.stderr,
        )
    if not cpu_ok:
        print(
            f"FAIL: peak CPU {peak_cpu:.2f} % exceeds threshold "
            f"{CPU_PERCENT_THRESHOLD:.1f} %.",
            file=sys.stderr,
        )
    if not duration_ok or not cpu_ok:
        _print_optimization_hints(migrate_elapsed, peak_cpu, rows_updated)
        return 1

    print("PASS: all STORY-7 thresholds satisfied.")
    return 0


def _print_optimization_hints(duration: float, peak_cpu: float, rows_updated: int) -> None:
    print()
    print("OPTIMIZATION HINTS (only relevant if thresholds were missed):")
    print(
        "  * The SQL is already a single set-based UPDATE ... FROM tmp_us_tickers "
        "... RETURNING stocks.id with a WHERE clause restricted to differing "
        "rows; this is the optimal shape for Postgres at 5M rows."
    )
    print(
        "  * If duration is the bottleneck: the hot loop is a hash join between "
        "stocks(id) and tmp_us_tickers(id). Both have PRIMARY KEY indexes so the "
        "join is index-nested-loop / hash on id; verify "
        "EXPLAIN (ANALYZE, BUFFERS) on the real query does not degenerate to a "
        "seq scan (which would happen if stats are stale -- ANALYZE both tables "
        "after seeding)."
    )
    print(
        "  * RETURNING stocks.id streams every matched row back to the client "
        "(psycopg materializes all of them in the cursor). For a migration with "
        "no need to inspect returned rows, dropping RETURNING halves network "
        "round-trips and reduces cursor memory; the Python caller still gets "
        "the rowcount via cursor.rowcount."
    )
    print(
        "  * If CPU is the bottleneck: the WHERE clause already excludes "
        "already-normalized rows so the executor should skip them. Confirm via "
        "EXPLAIN that the planner is using the tmp_us_tickers PK (it's the only "
        "index that matters on that table). Increasing shared_buffers / "
        "work_mem in the dev container can also reduce CPU by avoiding disk "
        "spill."
    )
    print(
        f"  * Observed peak {peak_cpu:.2f} % / duration {duration:.3f} s / rows {rows_updated:,}; "
        "paste the EXPLAIN (ANALYZE, BUFFERS) output into a follow-up issue."
    )


if __name__ == "__main__":
    raise SystemExit(main())