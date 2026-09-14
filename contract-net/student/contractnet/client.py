"""
The Contract Net contractor SDK.

Everything in this file is plumbing: connecting, reconnecting, framing JSON,
tracking your commitments, and running your compute off the event loop so a
long job never stops you from hearing the next call for proposals.

You should not need to edit it. Subclass `Contractor` in your own file and
override `on_cfp` (required) and, if you want the extra credit, `execute`.
"""

from __future__ import annotations

import asyncio
import json
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - older releases
    from websockets.client import connect as ws_connect  # type: ignore[no-redef]

from websockets.exceptions import ConnectionClosed

from .benchmark import calibrate, overall_score, work_units
from .tasks import run_task

APP_PING_INTERVAL = 20.0

# The manager closes a socket with this code when another connection registers
# the same team name. Retrying would just evict the other process right back,
# so this one stops instead.
DUPLICATE_NAME = 4001


@dataclass(frozen=True)
class Task:
    """A unit of work being auctioned."""

    task_id: int
    task_type: str
    params: dict[str, Any]
    budget: float
    deadline_s: float
    bid_window_ms: int
    attempt: int

    @property
    def work(self) -> float:
        """Proportional work estimate; see benchmark.work_units."""
        return work_units(self.task_type, self.params)


@dataclass(frozen=True)
class Bid:
    """What you are willing to do the job for."""

    price: float
    est_seconds: float


@dataclass(frozen=True)
class Rules:
    """The manager's published scoring rules, delivered at registration."""

    award_policy: str = "best_value"
    # Auction score under best_value is price + time_weight * est_seconds.
    time_weight: float = 2.0
    # Charged per second of runtime, whatever the outcome.
    cost_rate: float = 1.0
    # Breach damages, as a share of the TASK BUDGET, not of your bid.
    penalty_rate: float = 0.5
    # Share of the price still paid for correct-but-late work.
    late_credit: float = 0.0
    concurrency: int = 1


@dataclass
class Settlement:
    """The manager's accounting for one finished contract."""

    task_id: int
    # Filled in locally from the task you bid on, so you can group your
    # history by task type without tracking ids yourself.
    task_type: str
    verdict: str
    revenue: float
    cost: float
    penalty: float
    profit: float
    runtime: Optional[float]
    est_seconds: Optional[float]


@dataclass
class _Commitment:
    task: Task
    est_seconds: float
    started_at: Optional[float] = None


class Contractor:
    """
    Base class for your agent.

    Minimum viable subclass::

        class MyContractor(Contractor):
            def on_cfp(self, task):
                seconds = self.estimate(task)
                if seconds > task.deadline_s:
                    return None
                return Bid(price=task.budget * 0.9, est_seconds=seconds)
    """

    def __init__(
        self,
        name: str,
        url: str,
        token: Optional[str] = None,
        machine: Optional[str] = None,
        auto_calibrate: bool = True,
        verbose: bool = True,
    ) -> None:
        self.name = name
        self.url = url
        self.token = token
        self.verbose = verbose
        self.machine = machine or f"{platform.system()} {platform.machine()}"

        self.rules = Rules()
        self.rates: dict[str, float] = {}
        self.history: list[Settlement] = []

        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._commitments: dict[int, _Commitment] = {}
        self._outbox: list[dict[str, Any]] = []
        self._ws: Any = None
        self._auto_calibrate = auto_calibrate

    # -- the two methods you care about -------------------------------------

    def on_cfp(self, task: Task) -> Optional[Bid]:
        """
        Decide whether to bid, and how much to charge.

        Return a `Bid` to compete for the contract, or `None` to refuse.
        Refusing is free. Winning work you cannot deliver on time is not.
        """
        raise NotImplementedError("your contractor must implement on_cfp")

    def execute(self, task: Task) -> int:
        """
        Actually do the work. Runs in a worker thread, so blocking is fine.

        The default uses the reference implementation. Overriding this with a
        faster-but-identical implementation is the extra credit: your answer
        must still match the reference exactly.
        """
        return run_task(task.task_type, task.params)

    # -- optional hooks ------------------------------------------------------

    def on_registered(self) -> None:
        """Called once the manager has accepted your registration."""

    def on_reject(self, task_id: int, winner: Optional[str], price: Optional[float]) -> None:
        """Called when someone else won a contract you bid on."""

    def on_settled(self, settlement: Settlement) -> None:
        """Called when the manager has graded and paid for one of your jobs."""

    def on_bid_invalid(self, task_id: int, reason: str) -> None:
        """Called when a proposal was rejected before the auction even ran."""

    # -- state you can use while bidding ------------------------------------

    def estimate(self, task: Task) -> float:
        """Predicted seconds for this task on this machine, from calibration."""
        rate = self.rates.get(task.task_type, 0.0)
        if rate <= 0:
            return float("inf")
        return task.work / rate

    @property
    def queue_seconds(self) -> float:
        """
        Estimated seconds of work you are already committed to.

        Add this to a fresh estimate before comparing against a deadline: if
        two jobs are already queued, a task you could normally finish in two
        seconds may not actually land for six.
        """
        now = time.perf_counter()
        total = 0.0
        for c in self._commitments.values():
            if c.started_at is None:
                total += c.est_seconds
            else:
                total += max(0.0, c.est_seconds - (now - c.started_at))
        return total

    @property
    def profit(self) -> float:
        """Your running profit, as settled by the manager."""
        return sum(s.profit for s in self.history)

    # -- entry point ---------------------------------------------------------

    def run(self) -> None:
        """Connect and work until interrupted. Blocks."""
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            self._log("shutting down")

    async def _main(self) -> None:
        if self._auto_calibrate:
            self._log(f"calibrating {self.machine}…")
            self.rates = calibrate(verbose=self.verbose)
            self._log(f"benchmark score {overall_score(self.rates):,.0f}")

        worker = asyncio.create_task(self._worker())
        try:
            await self._connection_loop()
        finally:
            worker.cancel()

    # -- networking ----------------------------------------------------------

    async def _connection_loop(self) -> None:
        """Stay connected. Reconnect with backoff; classroom wifi is not reliable."""
        backoff = 1.0
        while True:
            try:
                async with ws_connect(self.url, max_queue=64) as ws:
                    self._ws = ws
                    backoff = 1.0
                    await self._send(
                        {
                            "type": "REGISTER",
                            "name": self.name,
                            "token": self.token,
                            "machine": self.machine,
                            "benchmark": round(overall_score(self.rates), 2)
                            if self.rates
                            else None,
                        }
                    )
                    await self._flush_outbox()
                    pinger = asyncio.create_task(self._ping_loop())
                    try:
                        await self._receive_loop(ws)
                    finally:
                        pinger.cancel()
            except ConnectionClosed as err:
                if getattr(err.rcvd, "code", None) == DUPLICATE_NAME:
                    self._log(
                        f"another process registered the name {self.name!r} and took "
                        "over this connection. Stopping. Use a different --name, or "
                        "quit the other one."
                    )
                    return
                self._log(f"connection lost ({err.__class__.__name__}); retrying in {backoff:.0f}s")
            except OSError as err:
                self._log(f"connection lost ({err.__class__.__name__}); retrying in {backoff:.0f}s")
            except Exception as err:  # noqa: BLE001 - keep the agent alive
                self._log(f"unexpected error: {err!r}; retrying in {backoff:.0f}s")
            finally:
                self._ws = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            if raw == "pong":
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            await self._dispatch(msg)

    async def _ping_loop(self) -> None:
        """Application-level keepalive the manager answers without waking up."""
        while True:
            await asyncio.sleep(APP_PING_INTERVAL)
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send("ping")
                except (OSError, ConnectionClosed):
                    return

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")

        if kind == "REGISTERED":
            self.rules = Rules(**{k: v for k, v in msg.get("rules", {}).items() if k in Rules.__dataclass_fields__})
            self._log(f"registered as {msg.get('name')} (policy: {self.rules.award_policy})")
            self.on_registered()
            for spec in msg.get("open_cfps", []):
                await self._handle_cfp(spec)

        elif kind == "CFP":
            await self._handle_cfp(msg)

        elif kind == "ACCEPT_PROPOSAL":
            task_id = msg["task_id"]
            commitment = self._commitments.get(task_id)
            if commitment is None:
                return
            self._log(
                f"won #{task_id} at ${msg['price']:.0f} "
                f"({msg['deadline_s']:.1f}s to deliver)"
            )
            await self._queue.put(commitment.task)

        elif kind == "REJECT_PROPOSAL":
            task_id = msg["task_id"]
            self._commitments.pop(task_id, None)
            self.on_reject(task_id, msg.get("winner"), msg.get("winning_price"))

        elif kind == "SETTLED":
            commitment = self._commitments.pop(msg["task_id"], None)
            s = Settlement(
                task_id=msg["task_id"],
                task_type=commitment.task.task_type if commitment else "",
                verdict=msg["verdict"],
                revenue=msg["revenue"],
                cost=msg["cost"],
                penalty=msg["penalty"],
                profit=msg["profit"],
                runtime=msg.get("runtime"),
                est_seconds=msg.get("est_seconds"),
            )
            self.history.append(s)
            self._log(
                f"#{s.task_id} {s.verdict}: profit ${s.profit:+.0f} "
                f"(ran {s.runtime:.2f}s, estimated {s.est_seconds or 0:.2f}s) "
                f"| total ${self.profit:+.0f}"
            )
            self.on_settled(s)

        elif kind == "BID_INVALID":
            # The most common reason an agent "never wins anything".
            self._commitments.pop(msg["task_id"], None)
            self._log(
                f"!! bid on #{msg['task_id']} was thrown out: {msg.get('reason')}"
            )
            self.on_bid_invalid(msg["task_id"], str(msg.get("reason", "")))

        elif kind == "ERROR":
            self._log(f"manager error [{msg.get('code')}]: {msg.get('message')}")

    async def _handle_cfp(self, spec: dict[str, Any]) -> None:
        task = Task(
            task_id=spec["task_id"],
            task_type=spec["task_type"],
            params=spec.get("params", {}),
            budget=spec["budget"],
            deadline_s=spec["deadline_s"],
            bid_window_ms=spec.get("bid_window_ms", 0),
            attempt=spec.get("attempt", 1),
        )

        try:
            bid = self.on_cfp(task)
        except Exception as err:  # noqa: BLE001 - a broken strategy should not kill the agent
            self._log(f"on_cfp raised {err!r}; refusing #{task.task_id}")
            bid = None

        if bid is None:
            await self._send({"type": "REFUSE", "task_id": task.task_id})
            return

        # Remember the estimate now so queue_seconds is right if we win.
        self._commitments[task.task_id] = _Commitment(task, bid.est_seconds)
        await self._send(
            {
                "type": "PROPOSE",
                "task_id": task.task_id,
                "price": round(float(bid.price), 4),
                "est_seconds": round(float(bid.est_seconds), 4),
            }
        )

    async def _send(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        payload = json.dumps(msg)
        if ws is None:
            self._outbox.append(msg)
            return
        try:
            await ws.send(payload)
        except (OSError, ConnectionClosed):
            self._outbox.append(msg)

    async def _flush_outbox(self) -> None:
        """Deliver anything that piled up while we were disconnected."""
        pending, self._outbox = self._outbox, []
        for msg in pending:
            # Bids for auctions that closed while we were away are dead letters.
            if msg.get("type") in ("PROPOSE", "REFUSE"):
                continue
            await self._send(msg)

    # -- execution -----------------------------------------------------------

    async def _worker(self) -> None:
        """
        Run awarded contracts one at a time, off the event loop.

        Serial by design: a contractor is one machine. If you take on two jobs
        at once they both get slower, which is exactly what `queue_seconds` is
        there to help you reason about.
        """
        while True:
            task = await self._queue.get()
            commitment = self._commitments.get(task.task_id)
            if commitment is not None:
                commitment.started_at = time.perf_counter()

            started = time.perf_counter()
            try:
                result = await asyncio.to_thread(self.execute, task)
                runtime = time.perf_counter() - started
                await self._send(
                    {
                        "type": "INFORM",
                        "task_id": task.task_id,
                        "result": str(result),
                        "runtime": round(runtime, 4),
                    }
                )
            except Exception as err:  # noqa: BLE001
                self._log(f"execution of #{task.task_id} failed: {err!r}")
                await self._send(
                    {
                        "type": "FAILURE",
                        "task_id": task.task_id,
                        "reason": repr(err),
                    }
                )
            finally:
                self._queue.task_done()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {self.name}: {message}", flush=True)
