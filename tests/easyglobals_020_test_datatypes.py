#!/usr/bin/env python3
"""EasyGlobals 0.2.0 (shared-memory rewrite) — datatype + semantics test.

Covers every datatype the P26 project stores in its Globals (bool/int/float/
str incl. the byte-map edge values), the extended types 0.2.0 fast-paths
(bytes, None, numpy arrays) or pickles (list/dict/tuple), cross-process
visibility, and the NEW single-writer ownership semantics (OwnershipError,
disown(), dead-owner transfer) that the production seeding pattern depends on.

Run (Jetson):  ~/Documents/easyglobals_test/venv/bin/python test_datatypes.py
"""
import multiprocessing
import sys
import time

import numpy as np
from EasyGlobals import Globals, OwnershipError

NS = "egtest_dt"

# --- every datatype/value shape the project's Project_Classes.Globals uses ---
PROJECT_CASES = {
    # bools (status/command bits)
    "b_false": False, "b_true": True,
    # ints: lifecounters (u16 wrap edge), conf %, lines_found, i16 extremes
    "i_zero": 0, "i_u16max": 65535, "i_conf": 87, "i_lines": 2,
    "i_i16min": -32768, "i_big": 99999,
    # floats: receive-time epoch, exposure readback
    "f_time": 1753699000.123456, "f_zero": 0.0, "f_exposure": 10.5,
    # strings: coil ID (<=20 ASCII incl - and space), line texts, empty
    "s_empty": "", "s_coil": "AB12-CD34 EF", "s_20": "X" * 20,
    "s_line": "220008300800",
}
EXTENDED_CASES = {
    "x_none": None,
    "x_list_str": ["220008300800", "", "WB1D"],
    "x_list_int": [0, 87, 65535],
    "x_dict": {"coil": "AB-12", "conf": 99},
    "x_tuple": (1, "a", 2.5),
    "x_bytes": b"\x00\x01\xfe\xff" * 100,
    "x_longstr": "L" * (1024 * 1024),
    "x_bigint": 2 ** 80,
}
ARRAY_CASES = {
    "a_frame": np.full((1200, 1920, 3), 173, np.uint8),   # camera-frame sized
    "a_f32": np.linspace(0, 1, 1000, dtype=np.float32).reshape(10, 100),
}


def check(name, got, want):
    if isinstance(want, np.ndarray):
        ok = (isinstance(got, np.ndarray) and got.dtype == want.dtype
              and got.shape == want.shape and np.array_equal(got, want))
    else:
        ok = got == want and type(got) is type(want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {type(got).__name__}")
    return ok


def child_read(ns, names, queue):
    g = Globals(ns)
    out = {}
    for n in names:
        out[n] = getattr(g, n)
    queue.put(out)
    g.close()


def child_write_expect_denied(ns, queue):
    g = Globals(ns)
    try:
        g.owned_by_parent = 123
        queue.put("WROTE (no OwnershipError!)")
    except OwnershipError:
        queue.put("OwnershipError")
    g.close()


def child_write_after_disown(ns, queue):
    g = Globals(ns)
    try:
        g.owned_by_parent = 456
        queue.put("wrote-ok")
    except OwnershipError:
        queue.put("still-denied")
    g.close()


def child_own_and_die(ns):
    g = Globals(ns)
    g.dying_owner_key = "owned-by-dead-child"
    # exit WITHOUT close: simulates a crashed process holding ownership


def main():
    multiprocessing.set_start_method("spawn")
    failures = 0
    g = Globals(NS)
    g.clear()

    print("== same-process roundtrip (project + extended + arrays)")
    all_cases = {**PROJECT_CASES, **EXTENDED_CASES, **ARRAY_CASES}
    for name, want in all_cases.items():
        setattr(g, name, want)
        failures += 0 if check(name, getattr(g, name), want) else 1

    print("== overwrite hot path (same key, many types)")
    for i, val in enumerate([1, 2.5, "s", True, None, b"b", [1], 7]):
        g.mutating_key = val
        failures += 0 if check(f"mutate#{i}", g.mutating_key, val) else 1

    print("== cross-process read")
    queue = multiprocessing.Queue()
    p = multiprocessing.Process(target=child_read,
                                args=(NS, list(all_cases), queue))
    p.start()
    got = queue.get(timeout=30)
    p.join(10)
    for name, want in all_cases.items():
        failures += 0 if check(f"xproc {name}", got[name], want) else 1

    print("== ownership: second process write must be denied")
    g.owned_by_parent = 1
    p = multiprocessing.Process(target=child_write_expect_denied, args=(NS, queue))
    p.start(); verdict = queue.get(timeout=30); p.join(10)
    print(f"  {'PASS' if verdict == 'OwnershipError' else 'FAIL'}  child write -> {verdict}")
    failures += 0 if verdict == "OwnershipError" else 1

    print("== ownership: disown() releases the variable")
    g.disown("owned_by_parent")
    p = multiprocessing.Process(target=child_write_after_disown, args=(NS, queue))
    p.start(); verdict = queue.get(timeout=30); p.join(10)
    print(f"  {'PASS' if verdict == 'wrote-ok' else 'FAIL'}  after disown -> {verdict}")
    failures += 0 if verdict == "wrote-ok" else 1

    print("== ownership: dead owner is transferable")
    p = multiprocessing.Process(target=child_own_and_die, args=(NS,))
    p.start(); p.join(30)
    time.sleep(0.5)
    try:
        g.dying_owner_key = "taken-over"
        ok = g.dying_owner_key == "taken-over"
    except OwnershipError:
        ok = False
    print(f"  {'PASS' if ok else 'FAIL'}  write to dead child's key")
    failures += 0 if ok else 1

    g.clear()
    g.close()
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
