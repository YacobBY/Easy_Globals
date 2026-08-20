#!/usr/bin/env python3
"""EasyGlobals 0.2.0 full-load stress test (Jetson / weakly-ordered ARM).

The 0.2.0 docstring admits its lock-free seqlock protocol "is not formally
fenced" on ARM — this hammer hunts exactly that class of bug: torn reads,
stale values, lost updates, growth/compaction races and leaks, under
production-plus load while the GPU pipeline may run concurrently.

Workers (each owns ONLY its own keys — single-writer semantics respected):
  counter   tight-loop int/float/bool writes (the no-pickle hot path)
  frame     camera-sized numpy frames (1200x1920x3 uint8) at ~30 Hz, filled
            with ONE value per frame -> any reader seeing a mixed-value copy
            has proven a torn read
  payload   bytes/str payloads cycling 1 KB..2 MB (forces slot growth and
            structural ops under the kernel mutex)
  plc       production-like mix: 40 bool/int/float/str keys at 20 Hz
  reader x3 continuously read EVERYTHING and validate invariants:
            counter monotonic, frame uniform, payload uniform+sane length

Run:  venv/bin/python stress_test.py --seconds 600
Exit code 1 if any validation failed. Stats printed every 5 s.
"""
import argparse
import multiprocessing
import os
import sys
import time

import numpy as np
from EasyGlobals import Globals

NS = "egstress"
PAYLOAD_SIZES = [1024, 65536, 524288, 2097152]


def rss_mb():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


def w_counter(ns, stop_at):
    g = Globals(ns)
    n = 0
    err = 0
    while time.time() < stop_at:
        try:
            g.ctr = n
            g.ctr_f = n * 0.5
            g.ctr_b = (n & 1) == 0
            n += 1
        except Exception:
            err += 1
        if n % 2000 == 0:
            g.stats_counter = (n, err, rss_mb())
    g.stats_counter = (n, err, rss_mb())
    g.close()


def w_frame(ns, stop_at, hz):
    g = Globals(ns)
    n = 0
    err = 0
    period = 1.0 / hz
    while time.time() < stop_at:
        t0 = time.time()
        try:
            g.frame = np.full((1200, 1920, 3), n % 251, np.uint8)
            g.frame_no = n
            n += 1
        except Exception:
            err += 1
        if n % 30 == 0:
            g.stats_frame = (n, err, rss_mb())
        time.sleep(max(0.0, period - (time.time() - t0)))
    g.stats_frame = (n, err, rss_mb())
    g.close()


def w_payload(ns, stop_at):
    g = Globals(ns)
    n = 0
    err = 0
    while time.time() < stop_at:
        size = PAYLOAD_SIZES[n % len(PAYLOAD_SIZES)]
        fill = n % 256
        try:
            if n % 2 == 0:
                g.payload = bytes([fill]) * size
            else:
                g.payload = chr(65 + (n % 26)) * size
            n += 1
        except Exception:
            err += 1
        if n % 50 == 0:
            g.stats_payload = (n, err, rss_mb())
        time.sleep(0.02)
    g.stats_payload = (n, err, rss_mb())
    g.close()


def w_plc(ns, stop_at):
    g = Globals(ns)
    n = 0
    err = 0
    while time.time() < stop_at:
        try:
            for k in range(10):
                setattr(g, f"plc_b{k}", (n + k) % 2 == 0)
                setattr(g, f"plc_i{k}", (n + k) % 65536)
                setattr(g, f"plc_f{k}", time.time())
                setattr(g, f"plc_s{k}", f"COIL-{n % 100000:06d}"[:20])
            n += 1
        except Exception:
            err += 1
        if n % 20 == 0:
            g.stats_plc = (n, err, rss_mb())
        time.sleep(0.05)
    g.stats_plc = (n, err, rss_mb())
    g.close()


def r_reader(ns, stop_at, idx):
    g = Globals(ns)
    reads = 0
    torn_frames = bad_counter = bad_payload = exceptions = 0
    last_ctr = -1
    while time.time() < stop_at:
        try:
            try:
                c = g.ctr
                if isinstance(c, int):
                    if c < last_ctr:
                        bad_counter += 1
                    last_ctr = c
            except AttributeError:
                pass
            try:
                arr = g.frame
                if isinstance(arr, np.ndarray):
                    lo, hi = int(arr.min()), int(arr.max())
                    if lo != hi or not (0 <= lo <= 250):
                        torn_frames += 1
            except AttributeError:
                pass
            try:
                p = g.payload
                if isinstance(p, (bytes, str)):
                    if len(p) not in PAYLOAD_SIZES or len(set(p)) > 1:
                        bad_payload += 1
            except AttributeError:
                pass
            for k in range(10):
                for prefix in ("plc_b", "plc_i", "plc_f", "plc_s"):
                    try:
                        getattr(g, f"{prefix}{k}")
                    except AttributeError:
                        pass
            reads += 1
        except Exception:
            exceptions += 1
        if reads % 500 == 0:
            setattr(g, f"stats_reader{idx}",
                    (reads, torn_frames, bad_counter, bad_payload, exceptions,
                     rss_mb()))
    setattr(g, f"stats_reader{idx}",
            (reads, torn_frames, bad_counter, bad_payload, exceptions, rss_mb()))
    g.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--frame-hz", type=float, default=30.0)
    args = ap.parse_args()

    multiprocessing.set_start_method("spawn")
    stop_at = time.time() + args.seconds
    g = Globals(NS)
    g.clear()

    procs = [
        multiprocessing.Process(target=w_counter, args=(NS, stop_at)),
        multiprocessing.Process(target=w_frame, args=(NS, stop_at, args.frame_hz)),
        multiprocessing.Process(target=w_payload, args=(NS, stop_at)),
        multiprocessing.Process(target=w_plc, args=(NS, stop_at)),
    ] + [multiprocessing.Process(target=r_reader, args=(NS, stop_at, i))
         for i in range(3)]
    for p in procs:
        p.start()

    t0 = time.time()
    while time.time() < stop_at:
        time.sleep(5)
        row = [f"t={time.time() - t0:5.0f}s"]
        for key in ("stats_counter", "stats_frame", "stats_payload", "stats_plc",
                    "stats_reader0", "stats_reader1", "stats_reader2"):
            try:
                row.append(f"{key.split('_', 1)[1]}={getattr(g, key)}")
            except AttributeError:
                row.append(f"{key.split('_', 1)[1]}=?")
        print("  ".join(row), flush=True)

    for p in procs:
        p.join(30)
        if p.is_alive():
            p.terminate()

    print("\n=== FINAL ===", flush=True)
    fails = 0
    for key in ("stats_counter", "stats_frame", "stats_payload", "stats_plc"):
        ops, err, rss = getattr(g, key)
        print(f"{key}: ops={ops} errors={err} rss={rss:.0f}MB")
        fails += err
    for i in range(3):
        reads, torn, badc, badp, exc, rss = getattr(g, f"stats_reader{i}")
        print(f"reader{i}: reads={reads} TORN_FRAMES={torn} "
              f"NON_MONOTONIC_CTR={badc} BAD_PAYLOAD={badp} exceptions={exc} "
              f"rss={rss:.0f}MB")
        fails += torn + badc + badp + exc
    shm = [f for f in os.listdir("/dev/shm") if "egstress" in f]
    for f in shm:
        print(f"/dev/shm/{f}: {os.path.getsize('/dev/shm/' + f) / 1e6:.1f} MB")
    g.clear()
    g.close()
    print("STRESS RESULT:", "PASS" if fails == 0 else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
