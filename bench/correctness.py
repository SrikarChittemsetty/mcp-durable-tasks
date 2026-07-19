"""Comparative benchmark: does the idempotency guarantee actually hold under
concurrency — and does the naive alternative measurably fail?

This is a *controlled experiment*. Two dedup strategies are pitted against the
identical workload; the ONLY variable is how duplicates are prevented:

  * naive  — application-level check-then-insert: SELECT to see if the key
             exists, and if not, INSERT. No database constraint. This is the
             first thing most people write, and it has a time-of-check to
             time-of-use (TOCTOU) race: two requests can both SELECT "not
             found" and both INSERT.
  * ours   — INSERT ... ON CONFLICT (idempotency_key) DO NOTHING, backed by a
             UNIQUE index. The database is the referee; a duplicate cannot be
             created even under a perfect tie.

Each trial fires N concurrent requests carrying the SAME idempotency key (the
real-world scenario: at-least-once delivery / client retries). We then count how
many rows each strategy actually created. Correct = exactly 1. Anything more is
a double-charge.

A few milliseconds of "work" is inserted between the naive check and its insert.
That doesn't manufacture the bug — the TOCTOU window exists regardless; the delay
just makes an inherently timing-dependent race *reliably observable* instead of
something that shows up only occasionally. The ON CONFLICT path gets the same
delay, so the comparison stays fair.

Run:
    python bench/correctness.py --conninfo "host=... dbname=..." \
        --trials 200 --concurrency 8 --work-ms 5
"""

from __future__ import annotations

import argparse
import threading
import time
import uuid

import psycopg

NAIVE_DDL = """
CREATE TABLE IF NOT EXISTS bench_naive (
    id text PRIMARY KEY,
    idempotency_key text NOT NULL          -- NOTE: deliberately NOT unique
);
"""
OURS_DDL = """
CREATE TABLE IF NOT EXISTS bench_ours (
    id text PRIMARY KEY,
    idempotency_key text UNIQUE NOT NULL   -- the DB is the referee
);
"""


def _naive_create(conninfo: str, key: str, barrier: threading.Barrier, work_s: float):
    conn = psycopg.connect(conninfo)
    try:
        barrier.wait()  # release all threads at once to maximize the race
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM bench_naive WHERE idempotency_key = %s", [key])
            if cur.fetchone() is not None:
                return
            time.sleep(work_s)  # representative check->act window (TOCTOU)
            cur.execute(
                "INSERT INTO bench_naive (id, idempotency_key) VALUES (%s, %s)",
                [uuid.uuid4().hex, key],
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def _ours_create(conninfo: str, key: str, barrier: threading.Barrier, work_s: float):
    conn = psycopg.connect(conninfo)
    try:
        barrier.wait()
        with conn.cursor() as cur:
            time.sleep(work_s)  # same delay, for a fair comparison
            cur.execute(
                "INSERT INTO bench_ours (id, idempotency_key) VALUES (%s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                [uuid.uuid4().hex, key],
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def _run_trial(conninfo, table, fn, key, concurrency, work_s) -> int:
    """Fire `concurrency` concurrent same-key requests; return rows created."""
    barrier = threading.Barrier(concurrency)
    threads = [
        threading.Thread(target=fn, args=(conninfo, key, barrier, work_s))
        for _ in range(concurrency)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = psycopg.connect(conninfo)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE idempotency_key = %s", [key])
        n = cur.fetchone()[0]
    conn.close()
    return n


def _run_strategy(conninfo, table, fn, *, trials, concurrency, work_s, tag):
    """Run `trials` trials of a strategy; return (dupe_trials, excess_rows)."""
    dupe_trials = 0
    excess = 0
    for i in range(trials):
        key = f"{table}-{tag}-{i}"
        created = _run_trial(conninfo, table, fn, key, concurrency, work_s)
        if created > 1:
            dupe_trials += 1
            excess += created - 1
    return dupe_trials, excess


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conninfo", required=True)
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--work-ms", type=float, default=2.0)
    ap.add_argument(
        "--concurrency-levels",
        default="2,4,8,16",
        help="comma-separated concurrency levels to sweep",
    )
    args = ap.parse_args()
    levels = [int(c) for c in args.concurrency_levels.split(",")]
    work_s = args.work_ms / 1000.0

    conn = psycopg.connect(args.conninfo)
    with conn.cursor() as cur:
        cur.execute(NAIVE_DDL)
        cur.execute(OURS_DDL)
        cur.execute("TRUNCATE bench_naive, bench_ours")
    conn.commit()
    conn.close()

    rows = []
    for c in levels:
        tag = f"c{c}"
        naive_dupes, naive_excess = _run_strategy(
            args.conninfo, "bench_naive", _naive_create,
            trials=args.trials, concurrency=c, work_s=work_s, tag=tag,
        )
        ours_dupes, ours_excess = _run_strategy(
            args.conninfo, "bench_ours", _ours_create,
            trials=args.trials, concurrency=c, work_s=work_s, tag=tag,
        )
        rows.append((c, naive_dupes, naive_excess, ours_dupes, ours_excess))
        print(f"  concurrency={c} done", flush=True)

    print(
        f"\nComparative correctness — duplicate charges vs. concurrency\n"
        f"({args.trials} trials per level; each trial = N simultaneous same-key requests; "
        f"{args.work_ms:.0f}ms check->act window)\n"
    )
    print("| concurrent requests | naive: trials double-charged | naive: avg dup charges/trial | ours: double-charges |")
    print("|---------------------|------------------------------|------------------------------|----------------------|")
    for c, nd, nx, od, ox in rows:
        nr = 100.0 * nd / args.trials
        avg = nx / args.trials
        print(
            f"| {c} | {nd}/{args.trials} ({nr:.0f}%) | {avg:.2f} | {od}/{args.trials} ({ox} excess) |"
        )


if __name__ == "__main__":
    main()
