#!/usr/bin/env python3
"""EasyGlobals 0.2.0 doom-scenario suite (uncompiled version).

Targeted attempts to break the shared-memory implementation:

  ownership   foreign-write detection under every condition: plain foreign
              write, N-process CREATION RACE on the same fresh name,
              takeover after owner SIGKILL, and (the nastiest) owner killed
              MID-WRITE — readers and new writers must not wedge on the
              abandoned odd seqlock
  threads     sibling threads of the owner hammering one key (allowed; the
              per-process write lock must serialize without corruption)
  exhaustion  slot-table overflow and segment-capacity overflow must fail
              LOUDLY and leave the namespace usable
  churn       create/delete cycles, grow/shrink cycles, attach/detach
              cycles — segment allocation and RSS must stay bounded
  massacre    SIGKILL every process mid-traffic, then a fresh attach must
              detect the stale segment, recover, and serve a full workload

Every scenario runs in its own namespace with join-timeouts: a hang reports
WEDGED instead of hanging the suite. Exit code 0 = all pass.

Run:  venv/bin/python easyglobals_020_doom_test.py [--quick]
"""
import argparse
import multiprocessing as mp
import os
import signal
import sys
import threading
import time

import numpy as np
from EasyGlobals import Globals, OwnershipError

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}",
          flush=True)


def seg_blocks(ns):
    try:
        return os.stat(f"/dev/shm/fg_{ns}_v2").st_blocks * 512 / 1e6
    except FileNotFoundError:
        return 0.0


def rss_mb():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


# ---------------------------------------------------------------- ownership --
def race_writer(ns, barrier, round_no, queue, done):
    g = Globals(ns)
    barrier.wait()
    try:
        setattr(g, f"race_{round_no}", os.getpid())
        queue.put(("won", os.getpid()))
    except OwnershipError:
        queue.put(("denied", os.getpid()))
    # Hold the handle until every racer has reported: since 0.2.1 close()
    # releases this process's ownership (a detached process can no longer
    # write), so closing right after the write would let a slower racer take
    # the variable over legitimately -- that is not the race under test.
    try:
        done.wait(30)
    except Exception:
        pass
    g.close()


def scenario_creation_race(rounds=30, procs=8):
    ns = "doom_race"
    g = Globals(ns); g.clear()
    ctx = mp.get_context("spawn")
    bad = 0
    for r in range(rounds):
        barrier = ctx.Barrier(procs)
        done = ctx.Barrier(procs + 1)
        queue = ctx.Queue()
        ps = [ctx.Process(target=race_writer,
                          args=(ns, barrier, r, queue, done))
              for _ in range(procs)]
        for p in ps:
            p.start()
        verdicts = [queue.get(timeout=30) for _ in ps]
        try:
            done.wait(30)              # all verdicts in: racers may close now
        except Exception:
            pass
        for p in ps:
            p.join(10)
        winners = [pid for v, pid in verdicts if v == "won"]
        final = getattr(g, f"race_{r}")
        if len(winners) != 1 or final != winners[0]:
            bad += 1
    report("creation race: exactly one winner, value = winner's",
           bad == 0, f"{rounds} rounds x {procs} procs, bad={bad}")
    g.clear(); g.close()


def owner_that_dies(ns, ready):
    g = Globals(ns)
    g.doomed_key = "owned-by-victim"
    ready.set()
    while True:
        g.doomed_key = "still-mine"
        time.sleep(0.001)


def scenario_sigkill_takeover():
    ns = "doom_kill"
    g = Globals(ns); g.clear()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    p = ctx.Process(target=owner_that_dies, args=(ns, ready))
    p.start()
    ready.wait(30)
    os.kill(p.pid, signal.SIGKILL)
    p.join(10)
    deadline = time.time() + 10
    took_over = False
    while time.time() < deadline:
        try:
            g.doomed_key = "taken-over"
            took_over = g.doomed_key == "taken-over"
            break
        except OwnershipError:
            time.sleep(0.2)
    report("takeover after owner SIGKILL", took_over)
    g.clear(); g.close()


def midwrite_victim(ns):
    g = Globals(ns)
    n = 0
    while True:
        g.victim_frame = np.full((1200, 1920, 3), n % 251, np.uint8)  # ~7 MB
        n += 1


def midwrite_prober(ns, queue):
    g = Globals(ns)
    good = torn = 0
    for _ in range(50):
        try:
            arr = g.victim_frame
            if isinstance(arr, np.ndarray):
                if int(arr.min()) == int(arr.max()):
                    good += 1
                else:
                    torn += 1
        except AttributeError:
            pass
    try:
        g.midwrite_recovery = "new-writer-ok"     # foreign name: we own it
        wrote = True
    except OwnershipError:
        wrote = False
    queue.put((good, torn, wrote))
    g.close()


def scenario_sigkill_midwrite(rounds=20):
    """SIGKILL a 7MB-frame writer at random offsets; the abandoned odd seq
    must not wedge readers, and takeover of the key must succeed."""
    ns = "doom_midwrite"
    g = Globals(ns); g.clear()
    ctx = mp.get_context("spawn")
    wedged = torn_total = 0
    for r in range(rounds):
        victim = ctx.Process(target=midwrite_victim, args=(ns,))
        victim.start()
        time.sleep(0.3 + (r % 7) * 0.013)          # land kills at varied offsets
        os.kill(victim.pid, signal.SIGKILL)
        victim.join(10)
        queue = ctx.Queue()
        prober = ctx.Process(target=midwrite_prober, args=(ns, queue))
        prober.start()
        try:
            good, torn, wrote = queue.get(timeout=20)
            torn_total += torn
            if not wrote:
                wedged += 1
        except Exception:
            wedged += 1                             # prober hung = WEDGED
            prober.terminate()
        prober.join(10)
        # the key itself must be takeable by a NEW owner after victim death
        deadline = time.time() + 10
        taken = False
        while time.time() < deadline:
            try:
                g.victim_frame = np.zeros((2, 2), np.uint8)
                taken = True
                break
            except OwnershipError:
                time.sleep(0.2)
        if not taken:
            wedged += 1
    report("SIGKILL mid-write storm: no reader wedge, no torn reads, "
           "key recoverable", wedged == 0 and torn_total == 0,
           f"{rounds} kills, wedged={wedged}, torn={torn_total}")
    g.clear(); g.close()


def foreign_hammer(ns, queue):
    g2 = Globals(ns)
    caught = 0
    for _ in range(1000):
        try:
            g2.mine = 999
        except OwnershipError:
            caught += 1
    queue.put(caught)
    g2.close()


def scenario_foreign_write_always_caught():
    ns = "doom_foreign"
    g = Globals(ns); g.clear()
    g.mine = 1
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    p = ctx.Process(target=foreign_hammer, args=(ns, queue))
    p.start()
    caught = queue.get(timeout=60)
    p.join(10)
    ok = caught == 1000 and g.mine == 1
    report("foreign writes: 1000/1000 caught, value untouched", ok,
           f"caught={caught}, value={g.mine}")
    g.clear(); g.close()


def scenario_owner_threads():
    ns = "doom_threads"
    g = Globals(ns); g.clear()
    errors = []

    def hammer(tid):
        try:
            for i in range(20000):
                g.thread_key = (tid, i)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    final = g.thread_key
    ok = not errors and isinstance(final, tuple) and final[1] == 19999
    report("4 sibling threads on one key: serialized, no OwnershipError",
           ok, f"errors={len(errors)}, final={final}")
    g.clear(); g.close()


# --------------------------------------------------------------- exhaustion --
def scenario_exhaustion():
    ns = "doom_exhaust"
    g = Globals(ns); g.clear()
    created = 0
    failure = None
    try:
        for i in range(10000):
            setattr(g, f"slot_{i}", i)
            created += 1
    except Exception as exc:
        failure = type(exc).__name__
    usable_after = False
    try:
        g.after_overflow = "ok"
        usable_after = g.after_overflow == "ok"
    except Exception:
        try:
            delattr(g, "slot_0")
            g.after_overflow = "ok"
            usable_after = True
        except Exception:
            pass
    report("slot exhaustion: loud failure + namespace stays usable",
           failure is not None and usable_after,
           f"created={created}, failure={failure}")
    for i in range(created):
        try:
            delattr(g, f"slot_{i}")
        except Exception:
            pass

    big_fail = None
    try:
        g.too_big = b"B" * (300 * 1024 * 1024)
    except Exception as exc:
        big_fail = type(exc).__name__
    small_ok = False
    try:
        g.small_after = 42
        small_ok = g.small_after == 42
    except Exception:
        pass
    report("capacity overflow: loud failure + namespace stays usable",
           big_fail is not None and small_ok, f"failure={big_fail}")
    g.clear(); g.close()


# -------------------------------------------------------------------- churn --
def scenario_churn(quick):
    ns = "doom_churn"
    g = Globals(ns); g.clear()
    cycles = 2000 if quick else 10000

    b0 = seg_blocks(ns); r0 = rss_mb()
    for i in range(cycles):                       # create/delete churn
        name = f"cd_{i % 500}"
        setattr(g, name, i)
        if i % 3 == 2:
            delattr(g, name)
    b1 = seg_blocks(ns); r1 = rss_mb()

    for i in range(cycles // 4):                  # grow/shrink churn
        g.gs_key = b"x" * (1024 if i % 2 == 0 else 2 * 1024 * 1024)
    b2 = seg_blocks(ns); r2 = rss_mb()

    attach_cycles = 300 if quick else 1500        # attach/detach churn
    for _ in range(attach_cycles):
        h = Globals(ns)
        h.close()
    b3 = seg_blocks(ns); r3 = rss_mb()

    ok = (b3 < 80) and (r3 - r0 < 60)             # bounded, not ratcheting
    report("churn: segment + RSS bounded",
           ok, f"blocks MB {b0:.0f}->{b1:.0f}->{b2:.0f}->{b3:.0f}, "
               f"rss {r0:.0f}->{r1:.0f}->{r2:.0f}->{r3:.0f}")
    g.clear(); g.close()


# ----------------------------------------------------------------- massacre --
def massacre_worker(ns, idx):
    g = Globals(ns)
    n = 0
    while True:
        setattr(g, f"mw_{idx}", np.full((300, 300), n % 251, np.uint8))
        setattr(g, f"mc_{idx}", n)
        n += 1


def scenario_massacre():
    """SIGKILL an entire worker fleet mid-traffic; a FRESH process must
    attach, detect the stale segment, and run a full workload."""
    ns = "doom_massacre"
    boot = Globals(ns); boot.clear(); boot.close()
    ctx = mp.get_context("spawn")
    ps = [ctx.Process(target=massacre_worker, args=(ns, i)) for i in range(4)]
    for p in ps:
        p.start()
    time.sleep(3)
    for p in ps:
        os.kill(p.pid, signal.SIGKILL)
    for p in ps:
        p.join(10)
    ok = False
    detail = ""
    try:
        g = Globals(ns)                            # fresh attach post-massacre
        g.post_massacre = "alive"
        for i in range(200):
            g.post_massacre = np.full((100, 100), i, np.uint8)
        arr = g.post_massacre
        ok = isinstance(arr, np.ndarray) and int(arr.min()) == int(arr.max()) == 199
        detail = "fresh attach + 200 writes ok"
        g.clear(); g.close()
    except Exception as exc:
        detail = f"RAISED {type(exc).__name__}: {str(exc)[:60]}"
    report("massacre: fresh attach recovers and serves traffic", ok, detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    t0 = time.time()

    print("== ownership enforcement")
    scenario_foreign_write_always_caught()
    scenario_creation_race(rounds=10 if args.quick else 30)
    scenario_sigkill_takeover()
    scenario_sigkill_midwrite(rounds=8 if args.quick else 20)
    scenario_owner_threads()
    print("== exhaustion")
    scenario_exhaustion()
    print("== churn / leaks")
    scenario_churn(args.quick)
    print("== massacre recovery")
    scenario_massacre()

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\nDOOM RESULT: {'ALL PASS' if not failed else 'FAILED: ' + ', '.join(failed)} "
          f"({len(RESULTS)} scenarios, {time.time() - t0:.0f}s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
