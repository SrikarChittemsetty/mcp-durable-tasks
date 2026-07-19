# mcp-durable-tasks

A crash-durable task store for the [MCP Tasks extension](https://github.com/modelcontextprotocol/ext-tasks) (SEP-2663).

## The problem

MCP's Tasks extension standardizes the *interface* for long-running agent tool calls (`tasks/get`, `tasks/update`, `tasks/cancel`), but the reference SDKs ship only an in-memory store — persistence, idempotency, crash recovery, and TTL cleanup are explicitly left to implementers.

This project is that missing piece: a Postgres-backed task store where a task's state survives a server restart, the same request can never be double-executed, and every state transition is guarded by an explicit state machine.

## Build plan

1. **In-memory core** — the `TaskState` machine and a `TaskStore` interface, no persistence.
2. **Postgres backend** — the same interface, durable across restarts.
3. **Idempotency** — same idempotency key returns the same task, never a duplicate side effect.
4. **TTL reaping** — finished tasks are garbage-collected.
5. **Fault-injection tests** — kill the process mid-task and prove exactly-once recovery.
6. **Benchmark** — against a Temporal-wrapped baseline (p50/p95/p99).

See the commit history for the build-in-public log.

## Why it exists

A deep dive into distributed-systems fundamentals — idempotency, exactly-once-ish semantics, and crash recovery — applied to a real, currently-unsolved gap in a fast-moving protocol.

## Dev

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
