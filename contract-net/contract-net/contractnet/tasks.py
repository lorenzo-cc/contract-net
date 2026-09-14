"""
Reference implementations of the compute tasks used in the tournament.

Two properties matter here, and both are deliberate:

1. Every task returns an EXACT INTEGER. No floating-point results cross the
   wire, so the manager can verify your answer by string comparison and there
   is no "close enough" tolerance to argue about.

2. Every task is DETERMINISTIC. Given the same params, this code produces the
   same answer on every machine and every CPython build, because it uses only
   `random.Random` (a specified Mersenne Twister), `hashlib`, and integer
   arithmetic. That is why the manager can hold an answer key.

The instructor generates the answer key by running exactly this module, so if
you change these functions your answers stop matching. You may make them
FASTER (see `execute` in your contractor), but the value they return must be
identical.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Callable, Mapping

# Modulus for checksum-style tasks: a Mersenne prime that keeps intermediate
# values comfortably inside machine integers while staying collision-resistant
# enough for grading.
CHECKSUM_MOD = (1 << 61) - 1


def monte_carlo_pi(params: Mapping[str, Any]) -> int:
    """Count how many of `samples` seeded random points land in the unit circle."""
    rng = random.Random(int(params["seed"]))
    samples = int(params["samples"])
    inside = 0
    for _ in range(samples):
        x = rng.random()
        y = rng.random()
        if x * x + y * y <= 1.0:
            inside += 1
    return inside


def prime_count(params: Mapping[str, Any]) -> int:
    """Count the primes in the half-open range [lo, hi) by trial division."""
    lo = int(params["lo"])
    hi = int(params["hi"])
    count = 0
    for n in range(max(lo, 2), hi):
        if n == 2:
            count += 1
            continue
        if n % 2 == 0:
            continue
        i = 3
        limit = int(n**0.5)
        while i <= limit:
            if n % i == 0:
                break
            i += 2
        else:
            count += 1
    return count


def hash_search(params: Mapping[str, Any]) -> int:
    """
    Find the smallest nonce whose SHA-256 digest falls below a target.

    The first four bytes of the digest are read as a big-endian integer and
    compared against `threshold`. This is how proof-of-work actually sets
    difficulty, and unlike counting whole leading zeros it is adjustable to
    any workload: each nonce succeeds with probability threshold / 2**32, so
    the expected number of attempts is 2**32 / threshold.

    Expected work is all you get. The number of attempts an individual instance
    needs is a geometric random variable, so it can land anywhere.
    """
    seed = str(params["seed"])
    threshold = int(params["threshold"])
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{seed}:{nonce}".encode()).digest()
        if int.from_bytes(digest[:4], "big") < threshold:
            return nonce
        nonce += 1


def sort_checksum(params: Mapping[str, Any]) -> int:
    """Sort `n` seeded integers, then fold them into a position-weighted checksum."""
    rng = random.Random(int(params["seed"]))
    n = int(params["n"])
    values = [rng.randrange(0, 2**31) for _ in range(n)]
    values.sort()
    total = 0
    for i, v in enumerate(values):
        total = (total + (i + 1) * v) % CHECKSUM_MOD
    return total


def matmul_mod(params: Mapping[str, Any]) -> int:
    """Multiply two seeded n x n integer matrices mod `mod`; return the sum mod `mod`."""
    rng = random.Random(int(params["seed"]))
    n = int(params["n"])
    mod = int(params["mod"])

    a = [[rng.randrange(mod) for _ in range(n)] for _ in range(n)]
    b = [[rng.randrange(mod) for _ in range(n)] for _ in range(n)]

    # Transpose b so the inner loop walks contiguous rows.
    bt = [[b[r][c] for r in range(n)] for c in range(n)]

    total = 0
    for row in a:
        for col in bt:
            acc = 0
            for x, y in zip(row, col):
                acc += x * y
            total += acc % mod
    return total % mod


TASKS: dict[str, Callable[[Mapping[str, Any]], int]] = {
    "monte_carlo_pi": monte_carlo_pi,
    "prime_count": prime_count,
    "hash_search": hash_search,
    "sort_checksum": sort_checksum,
    "matmul_mod": matmul_mod,
}


def run_task(task_type: str, params: Mapping[str, Any]) -> int:
    """Dispatch to the reference implementation for `task_type`."""
    try:
        fn = TASKS[task_type]
    except KeyError:
        raise ValueError(f"unknown task type: {task_type!r}") from None
    return fn(params)
