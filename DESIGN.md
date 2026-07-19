# Design decisions

This is the decision log the code comments only hint at: for each major choice,
what was chosen, the alternatives considered, why they were rejected, and the
failure mode the choice guards against. It's meant to be read alongside the
source — and to be the thing you can defend when someone asks "why not X?"

---

## 1. State transitions as an explicit allow-list table

**Chosen:** a `dict[TaskState, frozenset[TaskState]]` mapping each state to the
exact set of states it may become. The only way to change state is
`Task.transition_to`, which consults the table.

**Alternatives rejected:**
- *Enum ordering / integer ranks* (`completed > working`, allow only forward
  moves). Rejected because the lifecycle isn't linear: `input_required` legally
  goes **back** to `working`. A total order can't express a legal backward edge.
- *Scattered `if`/`else` checks at each call site.* Rejected because the rules
  would live in many places and drift; there'd be no single source of truth and
  no way to prove completeness.
- *A general state-machine library.* Rejected as overkill — five states and a
  handful of edges don't justify a dependency, and the table is more auditable.

**Failure mode it guards against:** an illegal transition (e.g. reopening a
`completed` task) silently corrupting state. With the table, the illegal move
raises `InvalidTransition` before anything is written.

---

## 2. Terminal states have an *empty* transition set

**Chosen:** `completed`, `failed`, `cancelled` map to `frozenset()` — zero
outgoing edges — and `frozenset` makes that immutable at runtime.

**Alternatives rejected:**
- *Enforce terminality only in recovery code.* Rejected: the guarantee would be
  one forgotten check away from breaking. Encoding it in the table means it's
  enforced everywhere `transition_to` is used, for free.

**Failure mode it guards against:** the whole crash-recovery guarantee. Recovery
trusts that a `completed` task's side effect already happened and refuses to
re-run it. That trust is only safe if `completed` can *never* flip back — so
terminality must be a hard invariant, not a convention.

---

## 3. `Task` is immutable (frozen dataclass)

**Chosen:** transitions return a *new* `Task` via `dataclasses.replace`, never
mutate in place.

**Alternatives rejected:**
- *Mutable dataclass with setters.* Rejected because shared mutable state is the
  source of "something changed this object behind my back" bugs, which get much
  worse once concurrency and caching enter. Immutability makes a `Task` value a
  safe thing to pass around and compare.

**Failure mode it guards against:** aliasing bugs — two references to the same
task disagreeing about its state after one of them mutates it.

---

## 4. One `TaskStore` Protocol, two backends

**Chosen:** a `typing.Protocol` defining the contract, with `InMemoryTaskStore`
(reference/control) and `PostgresTaskStore` (durable) both conforming
structurally. The **same contract test suite runs against both.**

**Alternatives rejected:**
- *A single Postgres-only class.* Rejected: no fast control to benchmark
  against, no cheap backend for tests that don't need durability, and no proof
  that "durable" didn't accidentally change behavior.
- *An abstract base class with inheritance.* Rejected in favor of a Protocol so a
  backend doesn't need to import or subclass anything — looser coupling.

**Failure mode it guards against:** the durable backend silently diverging from
the intended semantics. Behavioral parity is *tested*, not assumed.

---

## 5. Idempotency enforced by the database (`INSERT ... ON CONFLICT`)

**Chosen:** a `UNIQUE` index on `idempotency_key`, and create does
`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING ...`; if no row
comes back, it `SELECT`s and returns the existing task.

**Alternatives rejected:**
- *Application-level check-then-insert* (SELECT to see if the key exists, then
  INSERT). Rejected because of the time-of-check-to-time-of-use race: two
  concurrent requests both see "not found" and both insert.
  **This isn't hand-waving — it's measured** in `bench/correctness.py`: under
  simultaneous same-key requests, the naive approach double-charges in 100% of
  trials, with duplicate charges scaling as ~N-1. The DB approach: zero.
- *A mutex / advisory lock around the check.* Rejected: serializes all creates
  through one lock (throughput cliff), and doesn't survive a crash mid-hold as
  cleanly as a constraint.
- *`INSERT` and catch the unique-violation `IntegrityError`.* Reasonable, and
  nearly equivalent — `ON CONFLICT` was chosen because it's a single round trip
  and expresses intent directly rather than via exception control-flow.

**Failure mode it guards against:** duplicate side effects (double-charge) from
retried or concurrent requests carrying the same idempotency key.

**Honest note:** NULL keys are allowed to repeat — Postgres treats NULLs as
distinct in a unique index — which is exactly right: a task with no key opts out
of dedup.

---

## 6. Concurrent updates serialized with `SELECT ... FOR UPDATE`

**Chosen:** `update_task` and `complete_with_effect` lock the row
(`SELECT ... FOR UPDATE`) inside a transaction, then read-modify-write.

**Alternatives rejected:**
- *Optimistic concurrency (a `version` column, compare-and-swap, retry on
  conflict).* A legitimate alternative with better throughput under low
  contention. Rejected here for *clarity*: pessimistic locking makes the
  read-modify-write obviously correct with no retry loop to reason about. Noted
  as a possible optimization.
- *No locking.* Rejected: two updaters could both read the old state and issue
  conflicting writes (lost update).

**Failure mode it guards against:** the lost-update / double-apply race — two
workers both moving a `working` task forward and both running the side effect.
Proven prevented in `tests/test_concurrency.py`.

---

## 7. Side effect + state change in ONE transaction (`complete_with_effect`)

**Chosen:** the caller's side effect (e.g. a ledger `INSERT`) and the transition
to `COMPLETED` commit together, in the same transaction.

**Alternatives rejected:**
- *Do the effect, then separately mark the task done.* Rejected: a crash between
  the two leaves the effect applied but the task not `completed` — recovery
  re-runs the effect → double side effect.
- *Transactional outbox / two-phase patterns.* The right tool when the effect is
  in a *different* system; unnecessary complexity when the effect lives in the
  same database.

**Failure mode it guards against:** the classic "crashed between the write and
the bookkeeping" double-execution. One transaction means a crash before commit
rolls back *both*.

**Honest boundary — this is the most important nuance in the project:** true
exactly-once only holds when the side effect can join this transaction (same
Postgres). For an **external** effect (a Stripe charge over HTTP), it cannot —
you fundamentally get *at-least-once delivery plus an idempotency key at the
external boundary* = *effectively*-once. Claiming true exactly-once for an
external call would be wrong. The `idempotency_key` column is precisely that
external-boundary dedup handle.

---

## 8. Pull-based recovery (terminal-state check), not a push scheduler

**Chosen:** recovery happens when a worker re-runs a task and observes it's
already terminal (no-op) or still `working` (re-run cleanly).

**Alternatives rejected / deferred:**
- *A push-based scheduler that detects orphaned `working` tasks (a worker died)
  and auto-requeues them.* Genuinely better for a production system — it's on the
  roadmap. Deferred because it needs liveness tracking (heartbeats / lease
  expiry) that's a project in itself, and the exactly-once *core* is
  demonstrable without it.

**Failure mode it guards against (and its limit):** re-running a task is always
safe (idempotent). What it does *not* yet do: automatically notice that a worker
died and nobody will retry. That honest gap is stated in the README.

---

## 9. TTL reaper as a separate sweep

**Chosen:** a background loop deletes terminal tasks whose `expires_at` passed;
a partial index `(state, expires_at) WHERE expires_at IS NOT NULL` supports it.

**Alternatives rejected:**
- *Filter expired tasks out at query time and never delete.* Rejected: the table
  grows without bound; every query pays for dead rows forever.
- *Rely on a DB-native TTL / partition-drop.* Postgres has no row TTL; partition
  rotation is heavier machinery than a small sweep needs at this scale.

**Failure mode it guards against:** unbounded table growth. Only *terminal* tasks
are reaped — in-flight work is never deleted out from under a caller.

---

## 10. Timestamps are `timestamptz`, stored UTC

**Chosen:** every timestamp is timezone-aware UTC (`timestamptz` column,
`datetime.now(timezone.utc)`).

**Alternative rejected:** naive local-time datetimes. Rejected because they're
ambiguous across DST and across machines in different zones — a classic source of
"the reaper deleted things an hour early/late" bugs.

---

## 11. Sync API (not async)

**Chosen:** synchronous methods, for readability and easy reasoning about the
transactional read-modify-write.

**Alternative / honest tradeoff:** real MCP servers are async (asyncio). A
production binding would want an async store (`psycopg`'s async mode). Chosen sync
here to keep the concurrency story about *database* locking rather than about
event-loop mechanics — the crash/concurrency proofs use real OS processes, which
sidesteps the sync/async question entirely. Async is a mechanical port, not a
redesign.

---

## 12. Our state model vs. SEP-2663's final semantics

**Honest divergence worth knowing:** SEP-2663's final draft reserves `failed`
for *JSON-RPC transport* errors and treats a tool result with `isError: true` as
a **completed** task. This project models `failed` as a first-class task state
for *any* failure. When binding to the official wire protocol (see `protocol.py`
and the roadmap), a tool-level error maps to `completed` with an error result,
per the spec — the store's `failed` state then represents infrastructure/JSON-RPC
failures. This is called out so the mapping is deliberate, not accidental.
