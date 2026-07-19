# Benchmark results

**What this measures:** per-operation latency (percentiles) and throughput for
the core store operations, comparing the in-memory reference backend against the
durable Postgres backend. The gap between them is the cost of durability — the
price paid, per operation, to survive a crash.

**How to reproduce:**

```bash
python bench/benchmark.py --conninfo "host=127.0.0.1 port=5432 user=postgres dbname=mdt" \
    --iterations 2000 --warmup 200
```

## Environment

| | |
|---|---|
| Machine | Apple Silicon (arm64), macOS |
| Python | 3.12 |
| Postgres | 16 (local, single connection, TCP to 127.0.0.1) |
| Iterations | 2000 measured, 200 warmup (discarded) |
| Clock | `time.perf_counter` (monotonic, high-resolution) |

Single process, single connection. This is the store's own overhead, not a
distributed deployment.

## Results

| backend | op | n | mean (ms) | p50 | p95 | p99 | max | throughput (ops/s) |
|---------|----|---|-----------|-----|-----|-----|-----|--------------------|
| memory | create | 2000 | 0.004 | 0.003 | 0.004 | 0.009 | 1.676 | 256,657 |
| memory | get | 2000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 15,500,869 |
| memory | update | 2000 | 0.003 | 0.002 | 0.003 | 0.006 | 0.412 | 392,960 |
| postgres | create | 2000 | 0.144 | 0.122 | 0.263 | 0.621 | 1.379 | 6,950 |
| postgres | get | 2000 | 0.107 | 0.100 | 0.138 | 0.268 | 0.755 | 9,328 |
| postgres | update | 2000 | 0.194 | 0.164 | 0.326 | 0.750 | 2.178 | 5,163 |

## Reading the numbers

- **Durability costs ~30–50× in latency and ~1–2 orders of magnitude in
  throughput.** In-memory `create` runs at ~257k ops/s; durable `create` at
  ~7k ops/s. That is the fsync-and-network tax for state that survives a crash.
- **`update` is the most expensive durable op** (~0.16ms p50, ~0.75ms p99),
  which is expected: it's a `SELECT ... FOR UPDATE` (row lock) followed by an
  `UPDATE`, two statements plus a commit, versus `create`'s single insert.
- **Tail vs. median:** durable p99 is ~4–5× the p50. The tail is dominated by
  commit-time I/O — the reason percentiles, not averages, are the honest way to
  report a durable system's latency.

## Roadmap

The intended next comparison is a **Temporal-wrapped baseline** running the same
task lifecycle, to answer "doesn't Temporal already do this?" with a measured
latency/throughput delta rather than an assertion.
