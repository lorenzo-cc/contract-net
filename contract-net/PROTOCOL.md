# Wire protocol reference

Every frame is a single JSON object with a `type` field, sent as a WebSocket
text message. The names follow the FIPA Contract Net Interaction Protocol.

The Python SDK in `student/contractnet/` speaks all of this for you. This
document is for understanding what is happening underneath, for debugging, and
for anyone who wants to write a client in another language.

**Endpoints**

| Path | Purpose |
|---|---|
| `GET /agent?room=…` | Contractor connection (WebSocket upgrade) |
| `GET /spectate?room=…` | Read-only state feed for dashboards (WebSocket upgrade) |
| `GET /` | Tournament leaderboard |
| `GET /dev` | Development dashboard |
| `GET /api/state?room=…` | JSON snapshot, same shape as the spectator feed |
| `GET /health` | Liveness check |

`room` selects which tournament you join; it defaults to the Worker's
`DEFAULT_ROOM`. Each room is a separate Durable Object with its own task pool,
scores, and connections.

---

## Contractor → Manager

### `REGISTER`

Must be the first message on every connection, including after a reconnect.

```json
{
  "type": "REGISTER",
  "name": "Team_07",
  "token": "class-token-if-required",
  "machine": "M3 Pro / 12 cores",
  "benchmark": 14582269.0
}
```

`name` must match `^[A-Za-z0-9][A-Za-z0-9_-]{1,23}$`. `machine` and `benchmark`
are optional and purely informational, and appear on the dashboards.

Registering under a name that is already connected **closes the older
connection**. That is what makes reconnects work; it also means two processes
running under the same name will fight, and neither will work properly.

### `PROPOSE`

A bid on an open call for proposals.

```json
{ "type": "PROPOSE", "task_id": 42, "price": 2.75, "est_seconds": 1.1 }
```

Sending a second `PROPOSE` for the same task replaces your first, as long as
the bidding window is still open.

### `REFUSE`

Declining a call for proposals. Optional but good manners, and it is recorded.

```json
{ "type": "REFUSE", "task_id": 42, "reason": "queue too long" }
```

### `INFORM`

Delivering a completed result.

```json
{ "type": "INFORM", "task_id": 42, "result": "1785318802179667385", "runtime": 1.06 }
```

**`result` is a decimal integer as a JSON string.** Several tasks return values
larger than `2^53`, which a JSON number cannot represent exactly. Send the
string; the manager compares it verbatim against the answer key.

`runtime` is your own measurement. It is recorded and shown to you, but the
manager bills you on its own clock. See `SETTLED`.

### `FAILURE`

Giving up on a contract you won. Honest and slightly cheaper than going silent,
because the manager can re-offer the work immediately instead of waiting out
the deadline.

```json
{ "type": "FAILURE", "task_id": 42, "reason": "MemoryError" }
```

### `ping`

Not JSON. This is the literal text `ping`, to which the manager replies with the
literal text `pong`. This is answered by Cloudflare's WebSocket auto-response
without waking the Durable Object, so it is a free keepalive. The SDK sends one
every 20 seconds.

---

## Manager → Contractor

### `REGISTERED`

```json
{
  "type": "REGISTERED",
  "name": "Team_07",
  "now": 1789000000000,
  "rules": {
    "award_policy": "best_value",
    "time_weight": 2,
    "cost_rate": 1,
    "penalty_rate": 0.5,
    "late_credit": 0,
    "concurrency": 1
  },
  "open_cfps": []
}
```

`rules` is the live scoring configuration. Read it rather than hardcoding
values, because it can change between the practice room and the tournament.
`open_cfps` carries any auctions still open at the moment you registered, in
the same shape as `CFP`, so a contractor that joins mid-round can still bid.

### `CFP`

A call for proposals, broadcast to every registered contractor.

```json
{
  "type": "CFP",
  "task_id": 42,
  "task_type": "matmul_mod",
  "params": { "seed": 883211, "n": 260, "mod": 1000003 },
  "budget": 6.2,
  "deadline_s": 6.2,
  "bid_window_ms": 4000,
  "attempt": 1
}
```

- `budget`: the most the manager will pay. A higher bid is thrown out.
- `deadline_s`: seconds allowed for execution, counted from `ACCEPT_PROPOSAL`.
  An `est_seconds` above this is thrown out.
- `bid_window_ms`: how long you have to respond.
- `attempt`: 1 normally. Higher means a previous contractor failed this task
  and it is back on the market.

### `BID_INVALID`

Sent immediately when a proposal is thrown out, rather than letting it vanish.

```json
{ "type": "BID_INVALID", "task_id": 42, "reason": "price 9.4 is above the budget of 6.2" }
```

### `ACCEPT_PROPOSAL`

You won. Start work.

```json
{ "type": "ACCEPT_PROPOSAL", "task_id": 42, "price": 2.75, "due_at": 1789000006200, "deadline_s": 6.2 }
```

`due_at` is absolute server time in milliseconds. Comparing it against the
`now` you were given at registration is the reliable way to know how long you
actually have, without trusting your own clock to agree with the manager's.

### `REJECT_PROPOSAL`

Someone else won. Sent only to contractors who bid.

```json
{ "type": "REJECT_PROPOSAL", "task_id": 42, "winner": "Team_12", "winning_price": 2.4 }
```

The winner and price are public, and they are the raw material for modelling your
competitors.

### `SETTLED`

The books are closed on one of your contracts.

```json
{
  "type": "SETTLED",
  "task_id": 42,
  "verdict": "correct",
  "revenue": 2.75,
  "cost": 1.09,
  "penalty": 0,
  "profit": 1.66,
  "runtime": 1.093,
  "est_seconds": 1.1
}
```

`verdict` is one of:

| Verdict | Meaning |
|---|---|
| `correct` | Right answer, delivered before the deadline. Paid in full. |
| `late` | Right answer, delivered after the deadline. Paid `late_credit` share, fined. |
| `wrong_answer` | Delivered on time, but the value did not match. Fined. |
| `timeout` | Nothing arrived before the deadline plus grace. Fined. |
| `failure` | You sent `FAILURE`. Fined. |

`runtime` here is the manager's own measurement, from award to arrival and
including network time, and it is what you were billed for. Compare it against your
`est_seconds` to see how good your predictions are. Anything other than
`correct` also puts the task back on the market for someone else.

### `ERROR`

```json
{ "type": "ERROR", "code": "bidding_closed", "message": "task 42 is not accepting proposals" }
```

| Code | Meaning |
|---|---|
| `bad_json` | The frame was not valid JSON. |
| `bad_token` | Missing or wrong class token. The connection is then closed. |
| `bad_name` | Name failed validation. The connection is then closed. |
| `not_registered` | You sent something before `REGISTER`. |
| `bidding_closed` | The auction for that task is over, or never existed. |
| `bad_bid` | `price` or `est_seconds` was not a number. |
| `not_your_contract` | You informed on a task you do not hold, or already settled. |
| `unknown_type` | Unrecognised `type` field. |

---

## A complete exchange

```
contractor                                     manager
    │                                             │
    │──── REGISTER {name: "Team_07"} ────────────▶│
    │◀─── REGISTERED {rules, open_cfps} ──────────│
    │                                             │
    │◀─── CFP {task_id: 42, budget, deadline} ────│
    │──── PROPOSE {price: 2.75, est: 1.1} ───────▶│
    │                                             │  (bidding window closes)
    │◀─── ACCEPT_PROPOSAL {price, due_at} ────────│
    │                                             │
    │     (compute locally)                       │
    │                                             │
    │──── INFORM {result: "629524", runtime} ────▶│
    │◀─── SETTLED {verdict: "correct", profit} ───│
```

---

## Notes for writing your own client

- The manager never sends binary frames, and ignores any it receives.
- Messages are not ordered relative to your own sends; an `ACCEPT_PROPOSAL`
  for one task can arrive while you are still computing another.
- With `concurrency > 1`, several auctions run at once, and you can be awarded
  a second contract while the first is still executing. Your bids need to
  account for the queue.
- On reconnect you must `REGISTER` again. Bids for auctions that closed while
  you were disconnected are dead; an `INFORM` for a contract you still hold is
  still worth sending, and will be graded `late` rather than lost if the
  deadline has passed.
