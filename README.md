# mcp-durable-tasks

A crash-durable task store for the [MCP Tasks extension](https://github.com/modelcontextprotocol/ext-tasks) (SEP-2663).

## The problem

MCP's Tasks extension standardizes the *interface* for long-running agent tool calls (`tasks/get`, `tasks/update`, `tasks/cancel`), but the official SDKs ship only an in-memory reference store — persistence, idempotency, crash recovery, and TTL cleanup are explicitly left to implementers.

This project is that missing piece: a Postgres-backed task store where a task's state survives a server restart, the same request can never be double-executed, and every transition is guarded by an explicit state machine.

## Status

Early build — following a staged plan (in-memory core → persistence → idempotency → TTL reaping → fault-injection tests → benchmark vs. a Temporal-wrapped baseline). See commit history for the build-in-public log.

## Why this exists

Built as a deep dive into distributed-systems fundamentals — idempotency, exactly-once-ish semantics, and crash recovery — applied to a real, currently-unsolved gap in a fast-moving protocol.
