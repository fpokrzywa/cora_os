"""Deterministic verification of the worker's bounded concurrent pool
(app.worker._fill_slots / _reap / _idle_wait).

No DB and no real jobs: the pool helpers take injected claim/run coroutines, so
this drives them with in-memory fakes and asserts the contract that backlog
item 3 ("worker concurrency") required:

  A) up to WORKER_MAX_CONCURRENCY jobs run at once; the cap is never exceeded;
     every queued job runs exactly once (slots refill as tasks finish).
  B) cap=1 serializes (peak concurrency stays 1) — the old behavior, opt-in.
  C) a crashing task is reaped without wedging the pool.
  D) SHUTDOWN stops the pool from claiming new work.

    docker cp apps/cora-api/scripts/verify_worker_concurrency.py cora-api:/tmp/vw.py
    docker exec -e PYTHONPATH=/app cora-api python /tmp/vw.py   # 0=PASS 1=FAIL
"""
import asyncio
import sys

from app import worker


async def _drain(inflight, *, cap, claim, run, expect):
    """Run a fill→wait→reap pool to completion, asserting the cap each pass."""
    while True:
        worker._reap(inflight)
        await worker._fill_slots(
            inflight, max_concurrency=cap, claim=claim, run=run
        )
        expect(len(inflight) <= cap, f"in-flight never exceeds cap (cap={cap})")
        if not inflight:
            break
        await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)


async def main() -> int:
    fails: list[str] = []
    seen: set = set()

    def expect(cond: bool, msg: str) -> None:
        # de-dup by message so the repeated per-pass cap assertion prints once
        if msg in seen:
            if not cond and msg not in fails:
                fails.append(msg)
                print(f"  FAIL {msg}")
            return
        seen.add(msg)
        if cond:
            print(f"  ok   {msg}")
        else:
            fails.append(msg)
            print(f"  FAIL {msg}")

    worker.SHUTDOWN.clear()

    # ---- A) bounded concurrency, every job runs, slots refill ----
    print("A) bounded concurrent pool")
    cap, n = 3, 7
    pending = [{"id": i} for i in range(n)]
    live = peak = 0
    done: list[int] = []

    async def claim_a():
        return pending.pop(0) if pending else None

    async def run_a(job):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        done.append(job["id"])

    inflight: set = set()
    first = await worker._fill_slots(
        inflight, max_concurrency=cap, claim=claim_a, run=run_a
    )
    expect(first == cap, f"first fill starts exactly cap jobs ({first}=={cap})")
    await _drain(inflight, cap=cap, claim=claim_a, run=run_a, expect=expect)
    expect(peak == cap, f"peak concurrency reached the cap (peak={peak})")
    expect(sorted(done) == list(range(n)), "every queued job ran exactly once")
    expect(len(inflight) == 0, "pool fully drained")

    # ---- B) cap=1 serializes ----
    print("B) cap=1 serializes")
    pending_b = [{"id": i} for i in range(4)]
    live_b = peak_b = 0
    done_b: list[int] = []

    async def claim_b():
        return pending_b.pop(0) if pending_b else None

    async def run_b(job):
        nonlocal live_b, peak_b
        live_b += 1
        peak_b = max(peak_b, live_b)
        await asyncio.sleep(0.01)
        live_b -= 1
        done_b.append(job["id"])

    inflight_b: set = set()
    await _drain(inflight_b, cap=1, claim=claim_b, run=run_b, expect=expect)
    expect(peak_b == 1, f"cap=1 keeps peak concurrency at 1 (peak={peak_b})")
    expect(len(done_b) == 4, "all jobs still run when serialized")

    # ---- C) a crashing task is reaped, not wedged ----
    print("C) crashing task is reaped")
    one = [{"id": 99}]

    async def claim_c():
        return one.pop(0) if one else None

    async def boom(_job):
        raise RuntimeError("intentional task crash")

    inflight_c: set = set()
    await worker._fill_slots(
        inflight_c, max_concurrency=2, claim=claim_c, run=boom
    )
    expect(len(inflight_c) == 1, "the crashing job was spawned")
    await asyncio.wait(inflight_c)
    worker._reap(inflight_c)  # surfaces + drains the exception
    expect(len(inflight_c) == 0, "a crashing task is reaped (pool not wedged)")

    # ---- D) SHUTDOWN halts claiming ----
    print("D) SHUTDOWN halts the pool")
    worker.SHUTDOWN.set()
    jobs_d = [{"id": 1}, {"id": 2}]

    async def claim_d():
        return jobs_d.pop(0) if jobs_d else None

    async def run_d(_job):
        await asyncio.sleep(0)

    inflight_d: set = set()
    started = await worker._fill_slots(
        inflight_d, max_concurrency=3, claim=claim_d, run=run_d
    )
    expect(
        started == 0 and not inflight_d,
        "SHUTDOWN stops the pool from claiming new work",
    )
    worker.SHUTDOWN.clear()

    # ---- config sanity ----
    expect(
        isinstance(worker.WORKER_MAX_CONCURRENCY, int)
        and worker.WORKER_MAX_CONCURRENCY >= 1,
        "WORKER_MAX_CONCURRENCY is a positive int",
    )

    print()
    if fails:
        print(f"FAIL ({len(fails)}): " + "; ".join(dict.fromkeys(fails)))
        return 1
    print("PASS: worker concurrency verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
