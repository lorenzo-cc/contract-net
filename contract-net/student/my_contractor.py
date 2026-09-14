"""
CPSC 370, Assignment 1: your Contract Net contractor.

This file is yours. It runs as-is with a deliberately mediocre strategy, so
start by running it against the practice room, watch it lose money, and then
make it better.

    python my_contractor.py --name Team_07 --url wss://contractnet.example.com/agent

Everything you need is on `self`:

    self.estimate(task)   predicted seconds for this task on this machine
    self.queue_seconds    seconds of work you are already committed to
    self.rules            the manager's published scoring rules
    self.history          every Settlement you have received so far
    self.profit           your running profit

And on `task`:

    task.task_type        "monte_carlo_pi", "prime_count", "hash_search",
                          "sort_checksum", or "matmul_mod"
    task.params           the parameters for this instance
    task.budget           the most the manager will pay; higher bids are void
    task.deadline_s       seconds you get, measured from the moment you win
    task.work             a proportional estimate of how much work this is
"""

from __future__ import annotations

import argparse
import random
import time
from collections import deque

from contractnet import Bid, Contractor, Settlement, Task, work_units
from contractnet.benchmark import CALIBRATION

try:
    import numpy as np
except ImportError:  # extra credit is optional; fall back to the reference
    np = None

# hash_search's runtime is a geometric random variable: expected attempts is
# 2**32 / threshold, but for the small thresholds this tournament uses, the
# coefficient of variation of that distribution is close to 1 -- meaning a
# single instance landing at 2-3x the mean is routine, not a fluke. Sizing a
# bid for the expected time alone blows the deadline on a large share of
# instances. The other four task types are dominated by a fixed loop count
# and don't have this problem.
HIGH_VARIANCE_TASKS = {"hash_search"}

# Extra safety folded into *cost* (so we don't bid ourselves into a penalty)
# and into the deadline check (so we don't even try when there isn't enough
# room for a bad draw), specifically for high-variance task types.
VARIANCE_COST_MARKUP = 1.6
VARIANCE_DEADLINE_BUFFER = 1.6

# Queue depth compounds, and it's a task-type-agnostic risk, not just a
# hash_search one: every second already committed (self.queue_seconds) is
# baked into est_seconds for THIS bid already (finish_in below), so the
# score hit is automatic -- but it does NOT automatically raise price, and
# it doesn't account for the domino risk that some OTHER already-queued job
# overruns and drags this one late too. A loaded queue is real dollar risk
# (more chances something upstream slips) and real opportunity cost (that
# capacity could go to a fresher, higher-margin bid instead) -- both belong
# in cost, not just in the deadline check, so a heavily-loaded queue costs
# more to take on rather than being refused outright (refusing gives up a
# bid we might still profit on; pricing it in protects profit either way).
# Scaled by queue_seconds / this task's own deadline_s, capped so one
# short-deadline task can't blow the multiplier up unreasonably.
QUEUE_LOAD_PREMIUM = 0.5   # extra cost fraction at max load ratio
QUEUE_LOAD_BUFFER = 0.25   # extra deadline headroom required at max load ratio
QUEUE_LOAD_CAP = 2.0       # load ratio (queue_seconds / deadline_s) is capped here


class MyContractor(Contractor):
    """
    Bids near true cost, corrected two ways the flat-markup baseline ignores:

    1. Calibration drift. `self.estimate()` is only as good as the one-time
       benchmark taken at startup. Real settlements are ground truth, so each
       one nudges a per-task-type bias factor that corrects the estimate
       going forward. (README: "If your estimates keep coming in low, your
       calibration is optimistic. Correct for it.")

    2. Market feedback. `penalty_rate` applies to the task's BUDGET, not to
       our bid, so shading price down does not reduce the downside of a late
       or wrong answer -- the only lever that matters there is whether we
       should bid at all. What price-shading DOES affect is whether we win,
       and by how much we profit when we do. `on_settled` and `on_reject`
       are the only signal we get about that, so markup drifts up when we
       keep winning easily and down when we keep losing or missing
       deadlines, instead of sitting at one fixed number all night.

    3. Queue load. Taking on work is not free just because it clears its
       own deadline -- every already-queued second raises the odds some
       other job overruns and drags this one late too, and ties up
       capacity a fresher, higher-margin bid could have used instead. That
       is a dollar-profit concern, not a win-rate one, so a loaded queue
       gets a real cost premium and a wider deadline buffer (QUEUE_LOAD_*
       below) instead of being ignored or refused outright.

    4. Honest speed. self.rates (and therefore self.estimate()) is always
       measured against the SDK's reference implementation, never against
       any execute() override -- so a faster execute() alone does nothing
       for price or score unless estimate() is told about it too. See
       _calibrate_fast_paths and the estimate() override below.

    Estimates are never padded: under best_value scoring
    (`price + time_weight * est_seconds`), a padded estimate makes the bid
    look worse for zero benefit, since the real due_at comes from the CFP's
    `deadline_s`, not from what we promise.

    Also logs every CFP received and every decision made on it. A prior
    version of this file only printed wins and settlements, which made "the
    connection silently died" and "we bid on everything and lost" produce
    an identical (empty) terminal -- not a strategy bug, but a real one.
    """

    # Where markup starts, and how far it's allowed to drift from there.
    # MAX_MARKUP was raised after a practice run where profit was climbing
    # steadily with zero evidence the old 3.0x ceiling needed to be that low.
    BASE_MARKUP = 1.5
    MIN_MARKUP = 1.05   # never bid below ~1% over cost -- see the note below
    MAX_MARKUP = 5.0

    # Chase a WIN RATE, not just "as much markup as the room tolerates". One
    # team's practice-room dominance (near-100% win share) is a market-share
    # problem as much as a pricing one: sitting at a cautious markup while a
    # competitor wins almost everything means we compete on almost nothing.
    # Rather than drifting markup open-endedly on each win/loss, steer it
    # toward a target win rate over a rolling window -- winning much more
    # than the target means there's room to charge more; winning much less
    # means price is too high relative to whoever's beating us, and margin
    # should give way to actually being in the auction.
    #
    # This does NOT mean bidding below cost on purpose: MIN_MARKUP keeps
    # price at least ~5% over cost even when the controller is pushed to its
    # floor, so a bad settlement can still lose money (a late verdict prices
    # in nothing about the deadline), but the bid itself is never a
    # guaranteed loss by design -- "win more, even thin" is defensible in
    # the write-up; "win more by pricing under cost" is not.
    TARGET_WIN_RATE = 0.6
    WIN_RATE_WINDOW = 20
    WIN_RATE_GAIN = 0.35

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # actual_runtime / promised_est_seconds, per task type, as an
        # exponential moving average. 1.0 (trust the calibration) until we
        # have evidence otherwise, so one weird early sample can't swing it.
        self._bias: dict[str, float] = {}
        self._markup = self.BASE_MARKUP
        # True = won (a settlement only happens for contracts we hold),
        # False = lost (on_reject). Bounded window so ancient history
        # doesn't keep steering today's price.
        self._outcomes: deque[bool] = deque(maxlen=self.WIN_RATE_WINDOW)
        # task_id -> (our price, our est_seconds) for whatever we last bid
        # on that task, so a loss can be logged as a real comparison.
        self._last_bids: dict[int, tuple[float, float]] = {}
        # Per-task-type rate measured against OUR execute() override, for
        # any task type where we actually have one. See _calibrate_fast_paths.
        self._fast_rates: dict[str, float] = {}
        self._calibrate_fast_paths()

    def _calibrate_fast_paths(self) -> None:
        """
        self.rates -- what self.estimate() divides task.work by -- is set by
        the SDK's own calibrate(), which times contractnet.tasks.run_task,
        the REFERENCE implementation, always. It has no way to know we
        overrode execute() for matmul_mod, so every matmul_mod bid was being
        priced AND scored (est_seconds) off the slow reference's speed, even
        though our own execute() runs it 20-30x faster. That is pure
        self-sabotage: too expensive to be competitive on price, and too
        slow-looking to be competitive on score, on a task we'd already
        solved faster than the reference.

        Fix it the same way the SDK measures the reference: time our own
        fast path on the same small CALIBRATION instance, with the same
        work_units() formula, so self.estimate() can use OUR real rate for
        anything we've actually sped up, and fall back to the SDK's number
        for everything else.
        """
        if np is None:
            return
        params = CALIBRATION["matmul_mod"]
        started = time.perf_counter()
        self._matmul_mod_fast(params)
        elapsed = time.perf_counter() - started
        self._fast_rates["matmul_mod"] = work_units("matmul_mod", params) / max(elapsed, 1e-6)

    def estimate(self, task: Task) -> float:
        # Prefer our own measured rate for anything we've actually sped up
        # (see _calibrate_fast_paths); fall back to the SDK's reference-speed
        # calibration for everything else, exactly like the base class does.
        rate = self._fast_rates.get(task.task_type) or self.rates.get(task.task_type, 0.0)
        if rate <= 0:
            return float("inf")
        return task.work / rate

    def on_cfp(self, task: Task) -> Bid | None:
        # Visibility fix: neither this file nor the SDK previously logged a
        # CFP arriving, a PROPOSE being sent, or a REFUSE being sent -- only
        # wins and settlements printed. That made a silently-disconnected
        # agent and a connected-but-losing-everything agent look identical
        # in the terminal. Print every decision so that distinction is never
        # invisible again.
        self._log(f"CFP #{task.task_id} {task.task_type} (attempt {task.attempt}) "
                   f"budget=${task.budget:.2f} deadline={task.deadline_s:.2f}s "
                   f"queue={self.queue_seconds:.2f}s")

        # How long will this take me, including the work already in my
        # queue, corrected for how wrong that kind of estimate has run so far?
        bias = self._bias.get(task.task_type, 1.0)
        compute_seconds = self.estimate(task) * bias
        finish_in = self.queue_seconds + compute_seconds

        # How loaded is our queue relative to what THIS task's own deadline
        # gives us to work with? finish_in above already makes a loaded
        # queue hurt our score (it's baked into est_seconds), but it says
        # nothing about the extra risk of taking on MORE work while already
        # loaded -- some other queued job overrunning and dragging this one
        # late too. Capped so a single short-deadline task can't send the
        # ratio (and the premium/buffer below) to an unreasonable extreme.
        load_ratio = min(self.queue_seconds / task.deadline_s, QUEUE_LOAD_CAP) if task.deadline_s > 0 else 0.0

        # For a high-variance task, require real headroom before the
        # deadline -- not because we distrust the mean, but because the mean
        # is a bad guide to any one instance. For everything else, the plain
        # deadline check is enough. A task re-offered after someone else
        # already failed it (attempt > 1) is extra evidence, not just extra
        # risk, for hash_search specifically: the attempt count needed is a
        # deterministic function of that task's seed, so a prior timeout on
        # THIS task_id means this particular instance is a confirmed
        # outlier, not an average draw -- so it gets a bigger buffer, not
        # the same one. On top of that, ANY task type gets extra headroom
        # when the queue is already loaded, since the domino risk of "some
        # other queued job overruns" isn't specific to hash_search.
        if task.task_type in HIGH_VARIANCE_TASKS:
            buffer = VARIANCE_DEADLINE_BUFFER * (1.25 if task.attempt > 1 else 1.0)
        else:
            buffer = 1.0
        buffer *= 1 + QUEUE_LOAD_BUFFER * load_ratio
        if finish_in * buffer > task.deadline_s:
            self._log(f"  -> refuse #{task.task_id}: {finish_in * buffer:.2f}s "
                       f"needed (load_ratio={load_ratio:.2f}) vs {task.deadline_s:.2f}s deadline")
            return None  # refusing is free; blowing the deadline is not

        # What the job actually costs to run, plus a risk premium on top for
        # tasks whose realized cost can land well above the mean we priced,
        # plus a separate premium for how loaded the queue already is --
        # taking on a thin-margin job while already loaded risks a domino
        # late verdict AND ties up capacity a fresher bid could use instead,
        # so it should cost more, not just clear its own deadline for free.
        cost = compute_seconds * self.rules.cost_rate
        if task.task_type in HIGH_VARIANCE_TASKS:
            cost *= VARIANCE_COST_MARKUP * (1.25 if task.attempt > 1 else 1.0)
        cost *= 1 + QUEUE_LOAD_PREMIUM * load_ratio

        # self.rules.award_policy can differ between the practice room and
        # the tournament, and it changes what price even means:
        #   - best_value:      score = price + time_weight * est_seconds.
        #                      Price and speed both matter; markup chases
        #                      whatever the win/loss feedback below implies.
        #   - lowest_price:    est_seconds no longer buys anything
        #                      competitively (it's still checked against the
        #                      deadline, just not scored), so price is the
        #                      entire contest -- same formula, but there's no
        #                      reason left to shade est_seconds for score.
        #   - earliest_finish: price doesn't affect who wins at all, only
        #                      whether it's under budget -- shading it down
        #                      from budget for "safety" is pure lost revenue.
        policy = self.rules.award_policy
        if policy == "earliest_finish":
            price = task.budget * 0.98
            if price <= cost:
                return None
        else:
            price = cost * self._markup
            # A markup that clears the budget is money left on the table
            # only if the budget itself still clears cost -- so take the
            # budget rather than refuse outright, and only give up if even
            # that isn't profitable.
            if price > task.budget:
                price = task.budget * 0.98
                if price <= cost:
                    return None

        # Quote the honest finish time. Padding it only raises our score
        # (worse, under best_value) without changing what we're actually
        # held to. Stash it so on_reject can show what we lost by, not just
        # that we lost -- that comparison is the only real evidence of
        # whether a dominant competitor is beating us on price or on speed.
        self._last_bids[task.task_id] = (price, finish_in)
        self._log(f"  -> propose #{task.task_id}: ${price:.4f} / {finish_in:.4f}s")
        return Bid(price=price, est_seconds=finish_in)

    def on_settled(self, settlement: Settlement) -> None:
        # Ground truth beats a startup benchmark. Fold in how wrong this
        # estimate was so the next one for this task type is better.
        if settlement.runtime is not None and settlement.est_seconds:
            observed = settlement.runtime / settlement.est_seconds
            prior = self._bias.get(settlement.task_type, 1.0)
            self._bias[settlement.task_type] = 0.7 * prior + 0.3 * observed

        # A settlement only happens for a contract we won -- record it and
        # retarget toward TARGET_WIN_RATE before applying the separate,
        # smaller correction below.
        self._outcomes.append(True)
        self._retarget_markup()
        self._last_bids.pop(settlement.task_id, None)

        # A late/timeout verdict is a timing miss, not a pricing one -- it
        # says the deadline buffer was wrong, independent of win rate -- so
        # it gets its own, sharper pullback on top of the retargeting.
        if settlement.verdict in ("late", "timeout"):
            self._markup = max(self._markup * 0.9, self.MIN_MARKUP)

    def on_reject(self, task_id: int, winner: str | None, price: float | None) -> None:
        # Losing an auction is free -- but silent losses are the reason a
        # room full of REJECT_PROPOSAL messages looked identical to a dead
        # connection. Log exactly what beat us: their winning price against
        # our own price/est_seconds on the same task, which is the only
        # direct evidence of whether a dominant competitor wins on price or
        # on speed.
        mine = self._last_bids.pop(task_id, None)
        if mine is not None and price is not None:
            ours_price, ours_est = mine
            delta = "cheaper" if price < ours_price else "pricier"
            self._log(f"lost #{task_id} to {winner}: they bid ${price:.4f} "
                      f"({delta} than our ${ours_price:.4f} / {ours_est:.4f}s)")
        else:
            self._log(f"lost #{task_id} to {winner} at ${price}")
        self._outcomes.append(False)
        self._retarget_markup()

    def _retarget_markup(self) -> None:
        """
        Steer markup toward TARGET_WIN_RATE instead of drifting open-endedly.

        Winning well above target (a competitor is weak, or absent) means
        there's margin being left on the table -- push price up. Winning
        well below target (a competitor like the room's dominant bidder is
        beating us on most shared auctions) means price is too high to
        compete at all -- push it down, toward the MIN_MARKUP floor if
        that's what it takes, since being in the auction thin beats being
        priced out of it entirely. Needs a handful of data points before it
        moves, so one early result can't swing it.
        """
        if len(self._outcomes) < 5:
            return
        win_rate = sum(self._outcomes) / len(self._outcomes)
        gap = win_rate - self.TARGET_WIN_RATE
        factor = 1 + self.WIN_RATE_GAIN * gap
        self._markup = min(max(self._markup * factor, self.MIN_MARKUP), self.MAX_MARKUP)

    # Extra credit: override `execute` with a faster implementation. It must
    # return exactly the same integer as the reference, or the manager scores
    # it as a wrong answer and fines you.
    def execute(self, task: Task) -> int:
        if task.task_type == "matmul_mod" and np is not None:
            return self._matmul_mod_fast(task.params)
        return super().execute(task)

    @staticmethod
    def _matmul_mod_fast(params: dict) -> int:
        """
        Same math as contractnet.tasks.matmul_mod, done as bulk NumPy array
        ops instead of a Python-level triple loop (n^3 scalar
        multiply-accumulates run one at a time by the interpreter). The
        reference builds matrix a, matrix b, then for every (row, column)
        pair sums the elementwise products and folds each pair's total into
        a running sum mod `mod`.

        Two things have to match the reference exactly, not just
        approximately:

        1. The random draws. `rng.randrange(mod)` must be called in the
           exact same order -- every entry of `a` row by row, then every
           entry of `b` row by row -- so the matrices themselves come out
           identical. A flat list comprehension over n*n draws produces the
           same sequence as the reference's nested one; reshaping it into an
           n x n array afterward doesn't change draw order.

        2. Integer exactness. NumPy defaults to floating point, which would
           silently lose precision. Using int64 keeps every intermediate
           exact, and mod is small enough here (at most ~1e6) that even the
           least favorable task size in this tournament stays far under
           int64's ~9.2e18 ceiling for a single dot product -- but summing
           ALL n^2 dot products together first, before ever reducing mod,
           could still overflow for a large n. So each dot product is
           reduced mod `mod` before the final sum, exactly mirroring the
           reference's `total += acc % mod` -- summing already-reduced
           terms and reducing once more at the end gives the same answer as
           reducing after every term, since modular addition doesn't care
           where along the way you take the mod.
        """
        seed = int(params["seed"])
        n = int(params["n"])
        mod = int(params["mod"])

        rng = random.Random(seed)
        a_flat = [rng.randrange(mod) for _ in range(n * n)]
        b_flat = [rng.randrange(mod) for _ in range(n * n)]

        a = np.array(a_flat, dtype=np.int64).reshape(n, n)
        b = np.array(b_flat, dtype=np.int64).reshape(n, n)

        product = a @ b            # n^3 multiply-adds, done in compiled code
        reduced = product % mod    # per-entry mod, before summing (see above)
        return int(reduced.sum() % mod)


def main() -> None:
    parser = argparse.ArgumentParser(description="CPSC 370 Contract Net contractor")
    parser.add_argument("--name", required=True, help="your team name, e.g. Team_07")
    parser.add_argument("--url", required=True, help="wss://.../agent")
    parser.add_argument("--token", default=None, help="class token, if the room requires one")
    parser.add_argument("--machine", default=None, help="label shown on the leaderboard")
    args = parser.parse_args()

    MyContractor(
        name=args.name,
        url=args.url,
        token=args.token,
        machine=args.machine,
    ).run()


if __name__ == "__main__":
    main()
