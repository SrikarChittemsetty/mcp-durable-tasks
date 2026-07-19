# mcp-durable-tasks

[![CI](https://github.com/SrikarChittemsetty/mcp-durable-tasks/actions/workflows/ci.yml/badge.svg)](https://github.com/SrikarChittemsetty/mcp-durable-tasks/actions/workflows/ci.yml)

A crash-durable, spec-conformant task store for the [MCP Tasks extension](https://github.com/modelcontextprotocol/ext-tasks) (SEP-2663) — the persistent backend the reference SDKs leave to implementers.

When an AI agent starts a long-running tool call and the server handling it crashes mid-flight, that work is normally lost — or worse, silently re-run, double-charging a customer or sending an email twice. This project makes those tasks **survive crashes and take effect exactly once**.

---

## The gap this fills

MCP's Tasks extension standardizes the *interface* for long-running tool calls (`tasks/get`, `tasks/update`, `tasks/cancel`) and a task lifecycle. But the reference SDKs ship only an **in-memory** store; persistence, idempotency, crash recovery, and TTL cleanup are explicitly left to whoever deploys it. This is a durable implementation of that store, plus the tests that prove the guarantee holds.

## The core guarantee

> A task's side effect happens **exactly once**, even if the process is hard-killed (`kill -9`) at any point — before the effect, mid-transaction, or after commit — and even if two workers race the same task simultaneously.

That claim isn't asserted; it's **tested against a real Postgres with real `SIGKILL`s** (see [Proof](#proof-not-claims)).

## How it works

```
 agent starts a slow tool call
            │
            ▼
   create_task(idempotency_key)  ──►  dedup at the DB (INSERT … ON CONFLICT):
            │                          same key can never create a second task
            ▼
   state persisted in Postgres  (survives a crash)
            │
        work runs …
            │
   ┌────────┴──────────────── kill -9 ────────────────────┐
   │                                                        │
 before commit:                                  after commit:
 side effect + state change are ONE transaction, task is COMPLETED (terminal),
 so Postgres rolls BOTH back → task still WORKING → recovery sees "done" and
 a retry runs it cleanly, once           refuses to re-run the effect
   │                                                        │
   └───────────────► exactly one side effect ◄─────────────┘
```

### The state machine

Five states, with an explicit per-state allow-list of legal transitions. The three terminal states have **zero** outgoing transitions — that immutability is the property recovery relies on.

| state | may transition to |
|-------|-------------------|
| `working` | `input_required`, `completed`, `failed`, `cancelled` |
| `input_required` | `working` (resumes after getting input), `failed`, `cancelled` |
| `completed` / `failed` / `cancelled` | — (terminal, permanent) |

## Design decisions (the interview talking points)

- **Idempotency is enforced by the database, not application code.** A `UNIQUE` index on `idempotency_key` plus `INSERT … ON CONFLICT DO NOTHING` means even two simultaneous requests with the same key produce exactly one task. A check-then-insert in Python would race; the DB constraint can't.
- **Concurrent updates are serialized with row locks.** `update_task` does `SELECT … FOR UPDATE` inside a transaction, so two workers can't both read the old state and stomp each other. If the state-machine check rejects the transition, the transaction rolls back and nothing persists.
- **The side effect and the state change commit in one transaction** (`complete_with_effect`). That's what makes a *local* side effect (a ledger insert) truly exactly-once: a crash before commit rolls back both.
- **Honest boundary:** for an *external* side effect (a Stripe call that can't join the transaction), true exactly-once is impossible — you get at-least-once plus an idempotency key at the external boundary = *effectively*-once. The `idempotency_key` column is exactly that boundary. This distinction is stated plainly rather than glossed over.
- **One store interface, two backends.** `InMemoryTaskStore` (the reference/control) and `PostgresTaskStore` (durable) satisfy the same `TaskStore` protocol, and the **same contract test suite runs against both** — proving the durable backend didn't change behavior, only added durability.

## Proof, not claims

- **`tests/test_crash_recovery.py`** — spawns the worker as a real OS process, kills it with an actual `SIGKILL` at each commit boundary (`before_commit`, `after_commit`), and asserts the ledger holds exactly one row across the crash + recovery. Also covers a crash-loop (repeated recovery stays exactly-once).
- **`tests/test_concurrency.py`** — launches two workers on the same task at the same instant; the row lock serializes them and the charge still happens exactly once.
- **`tests/test_store_contract.py`** — the behavioral spec, run against both backends.
- **`tests/test_state_machine.py`** — every legal transition, and every illegal move out of a terminal state.

## Benchmark

Durability has a measurable cost, reported with percentiles (averages hide tail latency, which is what a durable commit actually incurs). Full methodology and environment in [`bench/RESULTS.md`](bench/RESULTS.md).

| backend | op | p50 (ms) | p99 (ms) | throughput (ops/s) |
|---------|----|----------|----------|--------------------|
| memory | create | 0.003 | 0.009 | 256,657 |
| postgres | create | 0.122 | 0.621 | 6,950 |
| postgres | update | 0.164 | 0.750 | 5,163 |

The ~30–50× latency gap *is* the fsync-and-network tax for crash-survival — measured, not asserted.

## Run it

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,postgres]"

# state-machine + in-memory tests need no database:
.venv/bin/pytest tests/test_state_machine.py tests/test_reaper.py

# full suite incl. crash recovery needs Postgres:
export MDT_TEST_DATABASE_URL="host=127.0.0.1 port=5432 user=postgres dbname=mdt"
.venv/bin/pytest

# benchmark:
.venv/bin/python bench/benchmark.py --conninfo "$MDT_TEST_DATABASE_URL"
```

## Scope & honest limitations

- Single-node Postgres; no leader election or multi-region. The durability story is "survive process crash," not "survive datacenter loss."
- The benchmark is single-process, single-connection — it measures the store's own overhead, not a production deployment under concurrent load.
- Recovery here is *pull-based* (a worker re-runs and observes terminal state). A push-based scheduler that automatically re-dispatches orphaned `working` tasks is on the roadmap.

## Roadmap

- Temporal-wrapped baseline benchmark (answer "doesn't Temporal already do this?" with numbers).
- Orphan detection: auto-requeue tasks stuck in `working` after a worker dies.
- A thin MCP server binding so it drops into a real agent runtime.

## Why it exists

A deep, hands-on build of the hard parts of durable execution — idempotency, exactly-once-ish semantics, crash recovery — applied to a real, currently-unsolved gap in a fast-moving protocol. Inspired by observability/reliability work on long-running background jobs.

## License

MIT — see [LICENSE](LICENSE).
