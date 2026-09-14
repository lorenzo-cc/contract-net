# Contract Net contractor: quickstart

Read `CPSC370-Assignment1-ContractNet.pdf` first. This file is just the
mechanics.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Check that your machine reproduces the reference answers:

```bash
python verify.py
```

Run against the practice room (URL and token are in the Canvas announcement):

```bash
python my_contractor.py --name YourTeamName --url "PRACTICE_URL" --token CLASS_TOKEN
```

**Quote the URL.** It contains a `?` (e.g. `.../agent?room=practice`), and on
zsh (the default shell on modern macOS) an unquoted `?` is treated as a
filename wildcard, not part of the string. Unquoted, you'll get a shell error
like `no matches found`, not a Python error -- the script never even starts.
Working example for this team:

```bash
python my_contractor.py --name Dawgz --url "wss://contractnet.blackdial.workers.dev/agent?room=practice" --token YOUR_CLASS_TOKEN
```

Pick your team name once and use the same one for the whole assignment, in
practice and in the tournament. It is how your results get matched to your
submission. Keep it appropriate: it goes on the projector and into the
published results.

Then open the development dashboard at `PRACTICE_URL/dev` (drop the
`/agent?room=...` part -- just the host, then `/dev`) and pick your team name
from the focus dropdown. That page shows you every message your agent sent
and received, and the reason any bid of yours was thrown out.

**Don't `Ctrl+C` while you might be holding a contract.** The SDK's
auto-reconnect (see "My connection keeps dropping" below) only covers a
*network* drop in the same running process -- killing the process outright
abandons anything it's currently executing, which the manager scores as a
`timeout` and fines. If you need to restart to ship a code change, do it
right after you see a `SETTLED` line in your terminal and before the next
`won #N`, not in the middle of one.

## What's in here

| File | What it is |
|---|---|
| `my_contractor.py` | **Yours.** The only file you edit. Implement `on_cfp`. |
| `verify.py` | Checks your machine, and any custom `execute`, against the reference. |
| `contractnet/client.py` | The SDK: connecting, reconnecting, message framing, running your compute off the event loop. |
| `contractnet/tasks.py` | Reference implementations of the five task types. **Do not modify.** |
| `contractnet/benchmark.py` | Calibration, and the work-unit model your estimates are built on. |

Everything under `contractnet/` is graded against the original. If you change
it, your answers stop matching the answer key and every submission is marked
wrong. `verify.py` will tell you if this has happened.

## The shape of an agent

```python
from contractnet import Bid, Contractor, Task

class MyContractor(Contractor):
    def on_cfp(self, task: Task) -> Bid | None:
        seconds = self.estimate(task)
        if self.queue_seconds + seconds > task.deadline_s:
            return None                      # refusing is free
        price = seconds * self.rules.cost_rate * 1.8
        if price > task.budget:
            return None                      # over budget bids are void
        return Bid(price=price, est_seconds=self.queue_seconds + seconds)
```

Optional hooks, if you want them: `on_registered`, `on_reject`, `on_settled`,
and `on_bid_invalid`. `execute` is also overridable, which is the extra credit.

## Things that trip people up

**Nothing happens when I connect.** The manager only announces tasks when at
least one agent is connected, and it pauses between tasks. Give it a few
seconds and watch the dashboard.

**My bids get thrown out.** Your agent prints the reason. It is almost always
a price above `task.budget` or an `est_seconds` above `task.deadline_s`.

**My agent never bids.** If `on_cfp` raises, the SDK catches it, logs it, and
refuses on your behalf so your agent stays alive. Check your terminal for the
traceback.

**I win everything and lose money.** Compare `est_seconds` against the real
`runtime` in your settlement messages. If your estimates run low, your markup
is not covering the gap.

**My connection keeps dropping.** Expected on campus wifi. The SDK reconnects
and re-registers automatically. Work in progress still gets delivered.

**`SSLCertVerificationError` on connect.** A local trust-store issue, not a
server problem -- Python can't find root certificates to verify the
manager's TLS cert against. Common with a python.org-installed Python on
macOS, which ships its own OpenSSL instead of using the system keychain. Fix:
if `/Applications/Python 3.x/Install Certificates.command` exists, run it and
relaunch. Otherwise, inside your activated `.venv`:

```bash
pip install --upgrade certifi
export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())")
```

`export` only lasts for the current terminal session -- re-run it if you open
a new tab.

**Extra credit needs numpy.** If you override `execute` for `matmul_mod`
using numpy (see the code comment there), it's in `requirements.txt`, so
`pip install -r requirements.txt` covers it. Without numpy installed, that
override silently falls back to the reference implementation rather than
crashing.
