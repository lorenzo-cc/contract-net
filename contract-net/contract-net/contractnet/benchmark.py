"""
Measuring how fast *your* machine is.

To bid well you need to answer one question before you answer any other:
"how long will this take me?" You cannot answer it from the task parameters
alone, because a 2019 dual-core laptop and an M4 Pro are not the same
contractor. So: estimate the amount of abstract work in a task, measure your
own throughput on each task type, and divide.

    est_seconds = work_units(task) / rate_for_that_task_type

`work_units` is a rough model of the algorithm's cost. It does not need to be
in any particular unit -- it only has to be *proportional* to real work, since
calibration divides the constant back out.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Mapping

from .tasks import run_task

# Attempts timed when measuring raw hashing throughput. See calibrate().
HASH_CALIBRATION_ROUNDS = 150_000

# Small instances used for calibration. Each is sized to run in well under a
# second so that starting a contractor is not itself a long job.
CALIBRATION: dict[str, Mapping[str, Any]] = {
    "monte_carlo_pi": {"seed": 1, "samples": 200_000},
    "prime_count": {"lo": 2_000_000, "hi": 2_025_000},
    "sort_checksum": {"seed": 1, "n": 200_000},
    "matmul_mod": {"seed": 1, "n": 70, "mod": 1_000_003},
}


def work_units(task_type: str, params: Mapping[str, Any]) -> float:
    """
    A proportional estimate of how much work a task represents.

    monte_carlo_pi  one unit per sample
    prime_count     trial division costs about sqrt(n)/2 per candidate
    hash_search     expected attempts, which is 2**32 / threshold
    sort_checksum   dominated by generating and folding n values
    matmul_mod      the classic n^3 multiply-accumulate count
    """
    if task_type == "monte_carlo_pi":
        return float(params["samples"])

    if task_type == "prime_count":
        lo, hi = int(params["lo"]), int(params["hi"])
        mid = max(2.0, (lo + hi) / 2.0)
        return (hi - lo) * math.sqrt(mid) / 2.0

    if task_type == "hash_search":
        # Each attempt succeeds with probability threshold / 2**32, so this is
        # the expected number of attempts. Only the expectation is knowable;
        # a given instance can need far more or far fewer.
        return 2.0**32 / float(params["threshold"])

    if task_type == "sort_checksum":
        n = int(params["n"])
        return n * math.log2(max(n, 2))

    if task_type == "matmul_mod":
        return float(int(params["n"]) ** 3)

    raise ValueError(f"unknown task type: {task_type!r}")


def _measure_hash_rate() -> tuple[float, float]:
    """
    Time a fixed number of hash attempts, rather than running a search.

    A search is a geometric random variable, so timing one would give a rate
    estimate that is wrong by a random factor of two or three on every run.
    Counting a fixed number of attempts measures the machine instead of the
    dice, which is the whole point of calibration: the uncertainty you price
    should belong to the task, not to your own measurement.
    """
    started = time.perf_counter()
    for i in range(HASH_CALIBRATION_ROUNDS):
        digest = hashlib.sha256(f"calibrate:{i}".encode()).digest()
        int.from_bytes(digest[:4], "big")
    return float(HASH_CALIBRATION_ROUNDS), time.perf_counter() - started


def calibrate(verbose: bool = True) -> dict[str, float]:
    """
    Time this machine on a small instance of every task type.

    Returns a mapping of task_type -> work units per second. Takes a couple of
    seconds. Run it once at startup, not on every call for proposals.
    """
    rates: dict[str, float] = {}
    for task_type in ("monte_carlo_pi", "prime_count", "hash_search",
                      "sort_checksum", "matmul_mod"):
        if task_type == "hash_search":
            units, elapsed = _measure_hash_rate()
        else:
            params = CALIBRATION[task_type]
            started = time.perf_counter()
            run_task(task_type, params)
            elapsed = time.perf_counter() - started
            units = work_units(task_type, params)

        rates[task_type] = units / max(elapsed, 1e-6)
        if verbose:
            print(
                f"  {task_type:<16} {max(elapsed, 0):6.3f}s  "
                f"{rates[task_type]:,.0f} units/s"
            )
    return rates


def overall_score(rates: Mapping[str, float]) -> float:
    """
    A single "how fast is this box" number, for the leaderboard's machine column.

    Geometric mean across task types, so no single task type dominates.
    """
    values = [r for r in rates.values() if r > 0]
    if not values:
        return 0.0
    return math.exp(sum(math.log(v) for v in values) / len(values))
