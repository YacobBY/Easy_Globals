"""
EasyGlobals — share Python objects between processes at shared-memory speed.

    from EasyGlobals import Globals
    g = Globals()                 # attach to (or create) the "default" namespace
    g.counter = 0                 # this process becomes the sole writer of 'counter'
    print(g.counter)              # every process may read it

Design goals (in order): speed, ease of use, cross-platform (Windows + Linux).

Semantics
---------
* SINGLE WRITER per variable: the first process to write a name owns it.
  Writes from any other process raise OwnershipError while the owner lives.
  If the owner died, ownership is transferred automatically.
* EVERY process may read every variable.
* EPHEMERAL state: variables live only as long as the program run. The last
  attached process unlinks the segment on exit; if a whole run is killed,
  the next program detects that no registered attacher is alive and wipes
  the stale data before reusing the segment. Nothing persists across runs.

Why it is fast
--------------
* One shared-memory segment per namespace; values are read/written in place.
* Per-slot SEQLOCK (sequence counter: odd while writing, even when stable).
  Because each variable has exactly one writer, readers never take a lock:
  they copy the value and retry in the rare case a write overlapped.
* In-place overwrites (the hot path: counters, frames, states) take NO kernel
  lock at all — just two sequence bumps around a memcpy.
* A kernel mutex (named mutex on Windows, robust pthread mutex in shared
  memory on Linux) guards only rare structural ops: creating a variable,
  growing a value past its reserved capacity, delete, compaction, attach.
* Pickle is skipped entirely for hot types: int, float, bool, None, str,
  bytes/bytearray and C-contiguous numpy arrays (single memcpy each way).
  Everything else falls back to pickle protocol 5 — any object works.

Memory layout is fixed-width little-endian, deliberately C-friendly so a
native extension can later map the same structs without a format change.

Concurrency note: CPython cannot issue memory fences, so the protocol is
built to need none on x86-64 (strong load-load/store-store ordering):

* The seq and generation counters are read and written through an aligned
  ``memoryview.cast('Q')`` view (``self._mmq``), so each publish is a SINGLE
  atomic 8-byte store and each validating load a single atomic 8-byte load —
  a reader can never assemble a torn or stale counter, wherever a writer is
  preempted. (``struct.pack_into('<Q')`` stores byte by byte and *can* leave
  a stale even value briefly visible; that was a real torn-read source.)
* Readers validate every copy against BOTH the slot seq and the table
  generation, and structural ops bump the generation before re-releasing
  any slot, so relocated slots can never satisfy a stale binding.
* Writers re-validate generation + freeze AFTER bumping the seq odd, so a
  compaction that ran entirely inside an OS preemption window is detected,
  and one that starts later waits on the odd seq.

The atomic-store view requires every counter offset to be 8-byte aligned
(header fields and the 64-byte slots are) and the segment length to be a
multiple of 8 (capacity is rounded up at attach). On weakly-ordered ARM the
retry protocol plus interpreter overhead make violations practically
unobservable, but the protocol is not formally fenced there.
"""
from __future__ import annotations

import atexit
import mmap
import os
import pickle
import struct
import sys
import threading
import time
import warnings
import weakref
import zlib
from multiprocessing import shared_memory
from struct import Struct
from typing import Any, Iterator

try:
    import numpy as _np
except ImportError:
    _np = None

# The EasyGlobals = Globals alias is deliberately NOT exported here: the
# package __init__ re-exports Globals explicitly, and star-importing the
# alias would shadow this submodule on the package (breaking the documented
# `from EasyGlobals import EasyGlobals; EasyGlobals.Globals()` style).
__all__ = ["Globals", "OwnershipError"]

_PROTO = pickle.HIGHEST_PROTOCOL
_IS_WIN = sys.platform == "win32"


class OwnershipError(PermissionError):
    """Raised when writing a variable owned by another live process."""


# ---------------------------------------------------------------------------
# Layout constants (all little-endian, fixed width — C-portable)
# ---------------------------------------------------------------------------

_MAGIC = b"FASTGLOBALS_V2\0\0"          # 16 bytes
_LAYOUT_VERSION = 2

# ---- header ----
_OFF_MAGIC      = 0      # 16s
_OFF_VERSION    = 16     # u32
_OFF_STATE      = 20     # u32: 0=initialising 1=ready 2=dead (unlinked)
_OFF_CAPACITY   = 24     # u64 total segment size
_OFF_SLOT_COUNT = 32     # u32
_OFF_LIVE       = 36     # u32 live keys
_OFF_TOMB       = 40     # u32 tombstones
_OFF_SLOTS      = 48     # u64 offset of slot table
_OFF_VALUES     = 56     # u64 offset of values region
_OFF_NEXT_FREE  = 64     # u64 bump-allocator cursor
_OFF_GEN        = 72     # u64 table generation (bumped by delete/clear/compact)
_OFF_FREEZE     = 80     # u64: 1 while compaction/clear rewrites blobs.
                         # Adjacent to GEN so the write fast path validates
                         # both with a single 16-byte unpack.
_OFF_FREEZER    = 88     # pid u32, pad, token u64 of the freeze holder, so
                         # a flag leaked by a killed process is recoverable
_OFF_CREATOR    = 104    # pid u32, pad, token u64 of the segment creator, so
                         # a creator killed before readiness is detectable
_OFF_LOCK       = 128    # 64 bytes reserved for in-shm pthread mutex (Linux)
_OFF_REGISTRY   = 256    # attacher registry
_REG_ENTRIES    = 256    # entries of 16 bytes: pid u32, pad u32, starttime u64
_REG_ENTRY      = Struct("<IxxxxQ")
_HEADER_SIZE    = _OFF_REGISTRY + _REG_ENTRIES * 16   # 4352 -> slots start here

# ---- slot record: 64 bytes, cache-line sized ----
#  seq u64 | state u8 pad3 | key_hash u32 | key_off u64 | key_len u32 |
#  owner_pid u32 | owner_token u64 | val_off u64 | val_len u64 | val_cap u64
_SLOT = Struct("<QB3xIQIIQQQQ")
_SLOT_SIZE = 64
assert _SLOT.size == _SLOT_SIZE

_S_EMPTY, _S_OCCUPIED, _S_TOMB = 0, 1, 2

# Field accessors for partial reads/writes on the hot path.
_U32 = Struct("<I")
_U64 = Struct("<Q")
_I64 = Struct("<q")
_F64 = Struct("<d")
_QQ = Struct("<QQ")           # gen + freeze at header offset 72, in one read
_OFFV = 40                    # offset of val_off inside a slot
# Word indices for the per-instance memoryview.cast("Q") view (self._mmq).
# Atomic control-word (seq/gen) stores go through that view: a cast('Q')
# assignment compiles to a single aligned 8-byte move, whereas struct's
# byte-wise store leaves torn/stale intermediates a lock-free reader can
# observe. All indexed offsets are 8-byte aligned (header fields and the
# 64-byte slots), a precondition asserted once at attach.
_OFF_GEN_W    = _OFF_GEN >> 3
_OFF_FREEZE_W = _OFF_FREEZE >> 3
# Merged hot-path accessors: one C call instead of two or three. The seq
# word is always read FIRST (it is the leading field of both), preserving
# the seqlock's read-seq-before-data ordering on x86's ordered loads.
_SEQVAL = Struct("<Q32xQQQ")     # seq, val_off, val_len, val_cap (writer)
_STATEVAL = Struct("<8xB31xQQ")  # state, val_off, val_len        (reader)
_TAG_I64 = Struct("<Bq")         # tag + int64 payload in one pack_into
_TAG_F64 = Struct("<Bd")         # tag + float64 payload in one pack_into
assert _SEQVAL.size == 64 and _STATEVAL.size == 56

# ---- value tags (first byte of every blob) ----
_T_NONE, _T_TRUE, _T_FALSE = 1, 2, 3
_T_INT, _T_FLOAT, _T_BYTES, _T_STR = 4, 5, 6, 7
_T_NUMPY, _T_PICKLE = 8, 9
_T_NPSCALAR = 10                          # numpy scalar (np.float64, np.int32…)

_DEFAULT_CAPACITY = 256 * 1024 * 1024     # demand-paged; real RAM only when touched
_DEFAULT_SLOTS = 8192                     # ~5.7k live keys at 0.7 load factor
_LOAD_NUM, _LOAD_DEN = 7, 10
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _safe(s: str, limit: int = 64) -> str:
    """Namespace name -> segment-name stem.

    A name that is already a plain [A-Za-z0-9_] identifier of at most
    `limit` chars maps to ITSELF (segment names stay readable, and existing
    namespaces keep their exact segment). Anything else — folded
    punctuation, non-ASCII (which the OS segment name cannot carry at all),
    or a name long enough to be truncated — gets a digest of the ORIGINAL
    name appended, so distinct namespaces can never collapse onto one
    segment ('cam-1' vs 'cam_1' vs a 64-char shared prefix).
    """
    out = "".join(c if ("a" <= c <= "z" or "A" <= c <= "Z"
                        or "0" <= c <= "9" or c == "_") else "_" for c in s)
    if out == s and len(out) <= limit:
        return out or "default"
    import hashlib
    tag = hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{out[:limit - 9]}_{tag}"


def _round_cap(n: int) -> int:
    """Reserved blob capacity: small fixed values stay tight, others get
    25% headroom rounded to 64 so in-place overwrites rarely reallocate."""
    if n <= 16:
        return 16
    return (n + (n >> 2) + 63) & ~63


def _seq_publish(mm, so: int, new: int) -> None:
    """Atomically publish an 8-byte sequence value.

    A memoryview.cast('Q') assignment compiles to a single naturally-aligned
    8-byte store (one `mov`), which x86-64 and AArch64 execute as one
    indivisible transaction — so a concurrent lock-free reader observes
    either the whole old value or the whole new value, never a torn or
    stale byte mix. This replaces the earlier hand-rolled two-phase
    byte-store discipline, which tried to make struct's byte-wise '<Q'
    store safe but still admitted a torn-read window (empirically ~1 in 10^7
    reads under contention). `so` is 8-byte aligned (slot / header field)."""
    mm[so:so + 8].cast("Q")[0] = new


def _bump_gen(mm) -> None:
    """Advance the table generation to the next even value with a single
    atomic store (see _seq_publish). Bumps happen only under the structural
    mutex, so the read-modify-write cannot race another bumper; the store
    itself is atomic, so a lock-free binder can never capture an in-flight
    intermediate. The generation is therefore always even when observed."""
    g = _U64.unpack_from(mm, _OFF_GEN)[0]
    mm[_OFF_GEN:_OFF_GEN + 8].cast("Q")[0] = (g + 2) & ~1


# ---------------------------------------------------------------------------
# Segment attach / unlink
# ---------------------------------------------------------------------------

# On Linux a POSIX shared segment IS a file under /dev/shm, so it can be
# attached without multiprocessing.shared_memory (see _FileSegment).
_SHM_DIR = None if _IS_WIN else (
    "/dev/shm" if os.path.isdir("/dev/shm") else None)


def _untrack(name: str) -> None:
    """Drop a segment from the resource tracker: EasyGlobals manages the
    lifetime itself (the last attacher unlinks), and the tracker would
    otherwise unlink a live segment when its creator exits."""
    if _IS_WIN:
        return
    try:
        from multiprocessing import resource_tracker
        resource_tracker.unregister(f"/{name}", "shared_memory")
    except Exception:
        pass


def _safe_unlink(shm) -> None:
    """Unlink a segment without desynchronising the resource tracker.

    Segments are untracked at open (above), but CPython <= 3.12's
    SharedMemory.unlink() unregisters unconditionally — the tracker daemon
    then dies with a KeyError traceback and, worse, forgets a name it may
    legitimately hold for a segment recreated later. Re-register right
    before the unlink so that unregister balances out.
    """
    if _IS_WIN or not isinstance(shm, shared_memory.SharedMemory) \
            or sys.version_info >= (3, 13):
        shm.unlink()
        return
    reg = f"/{shm.name}"
    try:
        from multiprocessing import resource_tracker
        resource_tracker.register(reg, "shared_memory")
    except Exception:
        shm.unlink()
        return
    try:
        shm.unlink()
    except BaseException:
        try:
            resource_tracker.unregister(reg, "shared_memory")
        except Exception:
            pass
        raise


class _FileSegment:
    """A segment attached by opening its /dev/shm file directly.

    multiprocessing.shared_memory.SharedMemory wraps its whole __init__ in
    `except OSError: self.unlink(); raise` — including the ATTACH path — so
    a single process whose mmap fails (RLIMIT_AS, ENOMEM, EMFILE) destroys
    the namespace for every other process. Opening the tmpfs file gives the
    identical mapping without that unlink, without the resource tracker,
    and without keeping a second fd open.
    """

    __slots__ = ("name", "size", "buf", "_map", "_path")

    def __init__(self, name: str, deadline: float = 0.0) -> None:
        path = os.path.join(_SHM_DIR, name)
        self._path = path
        fd = os.open(path, os.O_RDWR)
        try:
            size = os.fstat(fd).st_size
            while size < _HEADER_SIZE and time.monotonic() < deadline:
                # The creator ftruncates only AFTER the name appears, so a
                # zero/short size here means we attached inside the creation
                # window of a perfectly valid segment. Same fd, same inode:
                # re-stat until it grows instead of rejecting the segment.
                time.sleep(0.002)
                size = os.fstat(fd).st_size
            if size < _HEADER_SIZE:
                raise RuntimeError(
                    f"shared-memory name {name!r} exists but is not an "
                    f"EasyGlobals segment ({size} bytes)")
            self._map = mmap.mmap(fd, size)
        finally:
            os.close(fd)
        self.name = name
        self.size = size
        self.buf = memoryview(self._map)

    def close(self) -> None:
        try:
            self.buf.release()
        except Exception:
            pass
        self._map.close()

    def unlink(self) -> None:
        os.unlink(self._path)          # shm_unlink() on Linux is this unlink


def _attach_segment(name: str, deadline: float = 0.0):
    """Attach to an existing segment; never creates, never unlinks."""
    if _SHM_DIR is not None:
        return _FileSegment(name, deadline)
    try:
        return shared_memory.SharedMemory(name=name, track=False)
    except TypeError:                  # Python < 3.13: no track kwarg
        shm = shared_memory.SharedMemory(name=name)
        _untrack(name)
        return shm


class _Released:
    """Stands in for the mapping of a closed handle so every access reports
    the real cause instead of memoryview's 'operation forbidden on released
    memoryview' ValueError. Costs nothing while the handle is open."""

    __slots__ = ()

    def _fail(self, *_a):
        raise RuntimeError("handle is closed")

    __getitem__ = __setitem__ = __len__ = _fail


_RELEASED = _Released()


# ---------------------------------------------------------------------------
# Process identity: (pid, start time) — survives pid reuse
# ---------------------------------------------------------------------------

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(wintypes.DWORD)]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD

    _PROCESS_QUERY_LIMITED = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _WAIT_OBJECT_0 = 0x0

    def _proc_starttime(handle) -> int:
        times = (ctypes.c_uint64 * 4)()
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(times, i * 8)
                                              for i in range(4))):
            return 0
        return times[0]            # creation FILETIME, 100ns units

    def _my_token() -> int:
        return _proc_starttime(_k32.GetCurrentProcess())

    def _pid_alive(pid: int, token: int) -> bool:
        if not pid:
            return False           # pid 0 is never a valid attacher/owner
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED | _SYNCHRONIZE, False, pid)
        if not h:
            # A denied query (another user's process, SYSTEM, a service)
            # still PROVES the process exists — the DACL is only checked
            # after the pid resolves to a live object. Reporting it dead
            # would let another process steal ownership of its live variables
            # or wipe the segment, breaking the single-writer invariant.
            # Mirror the POSIX PermissionError->alive branch: only
            # ERROR_INVALID_PARAMETER (no such pid) means genuinely gone. We
            # cannot check the start token here, so pid-reuse defence is
            # forfeited for foreign processes — an accepted, unavoidable
            # limit of the OS permission model.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            # The handle is signaled the instant the process exits, whatever
            # its exit code — this closes the STILL_ACTIVE (259) collision
            # where a process that genuinely exited with code 259 would look
            # alive via GetExitCodeProcess alone.
            if _k32.WaitForSingleObject(h, 0) == _WAIT_OBJECT_0:
                return False
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            if code.value != _STILL_ACTIVE:
                return False
            return _proc_starttime(h) == token
        finally:
            _k32.CloseHandle(h)

else:
    def _read_stat(pid: int):
        """(state_char, starttime) from /proc/<pid>/stat; fields counted
        after the ')' that closes comm."""
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        rest = data[data.rindex(b")") + 2:].split()
        return rest[0], int(rest[19])   # state; starttime (field 22 overall)

    def _mac_proc(pid: int):
        """macOS: (start time, p_stat) via sysctl KERN_PROC_PID (best
        effort). kinfo_proc begins with extern_proc: the leading union
        overlays the run-queue pointers with `struct timeval
        __p_starttime` (tv_sec int64 @0, tv_usec int32 @8 — what ps(1)
        reads), and p_stat (char; SZOMB == 5) sits at offset 36 after the
        p_vmspace/p_sigacts pointers and p_flag."""
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)   # CTL_KERN KERN_PROC PID
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 \
                or size.value == 0:
            raise OSError("sysctl failed")
        sec = int.from_bytes(buf.raw[0:8], "little", signed=True)
        usec = int.from_bytes(buf.raw[8:12], "little", signed=True)
        if not 0 < sec < 4102444800:              # sanity: epoch..year 2100
            raise OSError("implausible start time")
        return sec * 1_000_000 + usec, buf.raw[36]

    def _my_token() -> int:
        try:
            return _read_stat(os.getpid())[1]
        except OSError:
            pass
        try:                           # non-Linux POSIX (macOS)
            return _mac_proc(os.getpid())[0]
        except Exception:
            import random
            return random.getrandbits(63) | 1

    def _pid_alive(pid: int, token: int) -> bool:
        if not pid:
            return False               # pid 0 is never a valid attacher/owner
        try:
            state, start = _read_stat(pid)
            # zombies/dead keep their /proc entry but are no longer attachers
            return state not in (b"Z", b"X") and start == token
        except (OSError, ValueError, IndexError):
            pass
        try:                           # macOS: start time + zombie state
            start, stat = _mac_proc(pid)
            return stat != 5 and start == token   # SZOMB == 5
        except Exception:
            pass
        # last resort — existence check via signal 0
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

_MY_PID = os.getpid()
_MY_TOKEN = _my_token()


def _refresh_identity() -> None:
    global _MY_PID, _MY_TOKEN
    _MY_PID = os.getpid()
    _MY_TOKEN = _my_token()


# ---------------------------------------------------------------------------
# Cross-process mutex for structural (slow-path) operations only
# ---------------------------------------------------------------------------

if _IS_WIN:
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.LPCWSTR]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.ReleaseMutex.argtypes = [wintypes.HANDLE]

    class _Mutex:
        __slots__ = ("_h",)

        def __init__(self, ns: str, buf, off: int) -> None:
            h = _k32.CreateMutexW(None, False, f"fg_{ns}_v2_mutex")
            if not h:
                raise ctypes.WinError(ctypes.get_last_error())
            self._h = h

        def init_in_shm(self) -> None:          # kernel object: nothing in shm
            pass

        def __enter__(self):
            r = _k32.WaitForSingleObject(self._h, 0xFFFFFFFF)
            if r in (0x00, 0x80):               # OBJECT_0 or ABANDONED
                return self
            raise OSError(f"WaitForSingleObject -> 0x{r:x}")

        def __exit__(self, *exc):
            _k32.ReleaseMutex(self._h)

        def close(self) -> None:
            if self._h:
                _k32.CloseHandle(self._h)
                self._h = None

def _load_pthread():
    """Return a handle exposing process-shared pthread mutex functions, or
    None if none is available (e.g. musl/Alpine, where the classic soname
    'libc.so.6' does not exist) — the caller then uses the flock fallback
    instead of crashing at import time."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes as _c
    for _name in ("libc.so.6", "libpthread.so.0", None):
        try:
            lib = _c.CDLL(_name, use_errno=True)
        except (OSError, TypeError):
            continue
        if hasattr(lib, "pthread_mutex_init") \
                and hasattr(lib, "pthread_mutexattr_setpshared"):
            return lib
    return None


_PTHREAD = _load_pthread()

if _IS_WIN:
    pass   # Windows _Mutex defined above
elif _PTHREAD is not None:
    import ctypes

    _pth = _PTHREAD
    for _fn in ("pthread_mutexattr_init", "pthread_mutexattr_destroy",
                "pthread_mutexattr_setpshared", "pthread_mutexattr_setrobust",
                "pthread_mutex_init", "pthread_mutex_lock",
                "pthread_mutex_unlock", "pthread_mutex_consistent"):
        getattr(_pth, _fn).restype = ctypes.c_int

    _EOWNERDEAD = 130

    class _Mutex:
        __slots__ = ("_addr", "_keep")

        def __init__(self, ns: str, buf, off: int) -> None:
            self._keep = ctypes.c_char.from_buffer(buf, off)
            self._addr = ctypes.addressof(self._keep)

        def init_in_shm(self) -> None:
            attr = (ctypes.c_byte * 16)()
            ap = ctypes.cast(attr, ctypes.c_void_p)
            if _pth.pthread_mutexattr_init(ap):
                raise OSError("mutexattr_init failed")
            try:
                if _pth.pthread_mutexattr_setpshared(ap, 1):
                    raise OSError("setpshared failed")
                _pth.pthread_mutexattr_setrobust(ap, 1)   # best effort
                if _pth.pthread_mutex_init(ctypes.c_void_p(self._addr), ap):
                    raise OSError("mutex_init failed")
            finally:
                _pth.pthread_mutexattr_destroy(ap)

        def __enter__(self):
            if not self._addr:
                # Locking a NULL address (a closed handle) would segfault;
                # fail cleanly instead of crashing the interpreter.
                raise RuntimeError("Globals handle is closed")
            r = _pth.pthread_mutex_lock(ctypes.c_void_p(self._addr))
            if r == _EOWNERDEAD:
                _pth.pthread_mutex_consistent(ctypes.c_void_p(self._addr))
            elif r:
                raise OSError(f"pthread_mutex_lock={r}")
            return self

        def __exit__(self, *exc):
            _pth.pthread_mutex_unlock(ctypes.c_void_p(self._addr))

        def close(self) -> None:
            self._keep = None
            self._addr = 0

else:
    import fcntl
    import tempfile

    class _Mutex:                               # macOS / other POSIX fallback
        __slots__ = ("_f",)

        def __init__(self, ns: str, buf, off: int) -> None:
            path = os.path.join(tempfile.gettempdir(), f"fg_{ns}_v2.lock")
            self._f = open(path, "ab")

        def init_in_shm(self) -> None:
            pass

        def __enter__(self):
            fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)

        def close(self) -> None:
            try:
                self._f.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Value encoding — pickle only as a last resort
# ---------------------------------------------------------------------------

def _encode(value: Any):
    """Return (tag, header_bytes, payload) where blob = tag|header|payload.

    header is b"" except for numpy (dtype/shape metadata). payload may be a
    buffer-protocol object (numpy array) to avoid an intermediate copy.
    """
    t = type(value)
    if t is int:
        if _INT64_MIN <= value <= _INT64_MAX:
            return _T_INT, b"", _I64.pack(value)
        return _T_PICKLE, b"", pickle.dumps(value, _PROTO)
    if t is float:
        return _T_FLOAT, b"", _F64.pack(value)
    if t is str:
        try:
            return _T_STR, b"", value.encode("utf-8")
        except UnicodeEncodeError:      # lone surrogates (PEP 383 paths)
            return _T_PICKLE, b"", pickle.dumps(value, _PROTO)
    if t is bytes:
        return _T_BYTES, b"", value
    if t is bool:
        return (_T_TRUE if value else _T_FALSE), b"", b""
    if value is None:
        return _T_NONE, b"", b""
    if t is bytearray:
        return _T_BYTES, b"", value     # buffer protocol: no copy here
    if t is memoryview:
        # A view isn't picklable; store its bytes (contiguous or not).
        return _T_BYTES, b"", value.tobytes()
    if _np is not None and isinstance(value, _np.generic):
        # numpy scalars (np.float64, np.int32, np.bool_, np.datetime64…) are
        # NOT ndarrays, so without this they fall to pickle: ~7x slower and
        # far larger. Fixed-size numeric/temporal kinds encode as dtype +
        # raw bytes and decode back to the exact scalar type; the rest
        # (variable-width U/S string scalars, void) still pickle.
        dt = value.dtype
        if dt.kind in "biufcMm":
            db = dt.str.encode("ascii")
            return _T_NPSCALAR, _U32.pack(len(db)) + db, value.tobytes()
    if _np is not None and type(value) is _np.ndarray:
        # Only plain ndarrays with simple dtypes take the raw-memcpy path:
        # bool/int/uint/float/complex/bytes/str. Everything else round-trips
        # through pickle instead, because the raw path breaks it: dtype.str
        # cannot represent structured dtypes (field info lost -> garbage on
        # decode), object dtypes would memcpy raw PyObject pointers across
        # processes, datetime64/timedelta64/StringDType cannot export via
        # the buffer protocol (would raise MID-write, wedging the seqlock
        # odd forever), and ndarray subclasses (masked arrays, np.matrix)
        # would silently lose their extra state.
        dt = value.dtype
        if dt.kind in "biufcSU":
            try:
                arr = value if value.flags["C_CONTIGUOUS"] \
                    else _np.ascontiguousarray(value)
                payload = memoryview(arr).cast("B")   # fail HERE, not in shm
                meta = pickle.dumps((dt.str, arr.shape), _PROTO)
                return _T_NUMPY, _U32.pack(len(meta)) + meta, payload
            except (ValueError, TypeError, BufferError):
                pass
    return _T_PICKLE, b"", pickle.dumps(value, _PROTO)


def _decode(blob: bytearray) -> Any:
    tag = blob[0]
    if tag == _T_INT:
        return _I64.unpack_from(blob, 1)[0]
    if tag == _T_STR:
        return str(memoryview(blob)[1:], "utf-8")
    if tag == _T_BYTES:
        return bytes(memoryview(blob)[1:])
    if tag == _T_FLOAT:
        return _F64.unpack_from(blob, 1)[0]
    if tag == _T_PICKLE:
        return pickle.loads(memoryview(blob)[1:])
    if tag == _T_NUMPY:
        mlen = _U32.unpack_from(blob, 1)[0]
        dtype, shape = pickle.loads(memoryview(blob)[5:5 + mlen])
        arr = _np.frombuffer(blob, dtype=dtype, offset=5 + mlen)  # writable view
        return arr.reshape(shape)
    if tag == _T_NPSCALAR:
        mlen = _U32.unpack_from(blob, 1)[0]
        dtype = bytes(memoryview(blob)[5:5 + mlen]).decode("ascii")
        # frombuffer(...)[0] returns a numpy scalar of the exact dtype.
        return _np.frombuffer(bytes(memoryview(blob)[5 + mlen:]), dtype=dtype)[0]
    if tag == _T_TRUE:
        return True
    if tag == _T_FALSE:
        return False
    if tag == _T_NONE:
        return None
    raise ValueError(f"corrupt value blob (tag={tag})")


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

_MISSING = object()            # sentinel: variable not present
_REBOUND = object()            # sentinel: slot binding moved, re-resolve

# Live handles (fork re-registration + the interpreter-exit sweep below).
# WEAK refs: a strong registry — or an atexit hook bound to a method — makes
# every handle immortal, so a dropped handle never runs __del__ and leaks its
# fd and mapping until the process hits EMFILE.
_INSTANCES: "weakref.WeakSet" = weakref.WeakSet()
# One write lock per namespace per process: serializes sibling threads (and
# sibling Globals instances) of the owning process through the seqlock's
# bump-write-bump window, which is interruptible at every bytecode. Readers
# never touch it; cross-process writers never contend on it.
_NS_LOCKS: dict = {}
# Attach refcount per namespace per process: a process registers once in the
# segment, so only the LAST local handle may deregister/mark-dead/unlink.
_NS_REFS: dict = {}
# Guards _NS_REFS and the _closed flip: close() may run concurrently from a
# worker thread, atexit and __del__, and must decrement exactly once.
_LIFE = threading.Lock()


class _Structural:
    """Gate around a handle's structural (mutex-guarded) operations.

    A thread blocked in pthread_mutex_lock() holds nothing but a raw address
    into the mapping; if close() unmaps meanwhile, the hand-over writes into
    unmapped memory and the process dies with SIGSEGV. close() therefore
    flips _closed FIRST (so threads that have not entered fail fast, and can
    never starve the closer) and then waits here for the in-flight operation
    to finish before releasing the mapping.

    Not on the lock-free fast path: these callers already pay a kernel
    futex, so the uncontended Python lock is noise.
    """

    __slots__ = ("_ref",)

    def __init__(self, g: "Globals") -> None:
        # WEAK: a strong back-reference would make every handle part of a
        # cycle, so __del__ would wait for the cyclic collector instead of
        # releasing the mapping when the last user reference goes away.
        self._ref = weakref.ref(g)

    def __enter__(self):
        g = self._ref()
        if g is None or g._closed:
            raise RuntimeError("handle is closed")
        g._slock.acquire()
        try:
            if g._closed:
                raise RuntimeError("handle is closed")
            g._lock.__enter__()
        except BaseException:
            g._slock.release()
            raise
        return self

    def __exit__(self, *exc):
        g = self._ref()                # alive: our caller is inside a method
        try:
            g._lock.__exit__(*exc)
        finally:
            g._slock.release()


class Globals:
    """Attribute-style shared variables across processes on one machine.

    g = Globals()              -> default namespace
    g = Globals("vision")      -> isolated namespace
    g.frame = ndarray          -> claim 'frame', write it (lock-free if it fits)
    x = g.frame                -> lock-free read from any process
    del g.frame                -> owner (or anyone once owner died)
    'frame' in g, g.keys(), len(g), g['odd name'], g.get('x', default)
    g.wait_for('frame')        -> block until another process publishes it
    """

    def __init__(self, namespace: str = "default",
                 capacity: int = _DEFAULT_CAPACITY,
                 slot_count: int = _DEFAULT_SLOTS) -> None:
        ns = _safe(namespace)
        # Round the segment size up to a 64-byte multiple so the whole
        # buffer can be viewed as aligned 8-byte words (self._mmq) without
        # the trailing bytes tripping memoryview.cast("Q").
        capacity = (int(capacity) + 63) & ~63
        d = object.__setattr__
        d(self, "_ns", ns)
        d(self, "_capacity_arg", capacity)
        d(self, "_slot_count_arg", slot_count)
        d(self, "_cache", {})          # name -> [slot_off, gen, hash, owned]
        d(self, "_seen", {})           # name -> seq wait_change last delivered
        d(self, "_closed", True)       # not attached yet
        d(self, "_wlock", _NS_LOCKS.setdefault(ns, threading.Lock()))
        d(self, "_slock", threading.RLock())   # structural ops vs close()
        d(self, "_sync", _Structural(self))

        # Validate geometry BEFORE creating anything: a creator that fails
        # mid-init would otherwise leave a never-ready segment behind.
        # slot_count < 4 is unusable — 0 divides by zero on every probe
        # (h % slot_count), and the 0.7 load factor rejects the first insert
        # for 1..2 — so reject it up front with a clear message.
        if not isinstance(slot_count, int) or slot_count < 4:
            raise ValueError(f"slot_count must be an int >= 4, got "
                             f"{slot_count!r}")
        slots_off = (_HEADER_SIZE + 63) & ~63
        if ((slots_off + slot_count * _SLOT_SIZE + 63) & ~63) + 1024 \
                > capacity:
            raise ValueError(f"capacity {capacity} too small for "
                             f"{slot_count} slots")

        for _attempt in range(64):
            shm, created = self._open_segment(f"fg_{ns}_v2", capacity)
            d(self, "_shm", shm)
            d(self, "_mm", shm.buf)
            # Every handle acquired for THIS attempt is set on self before the
            # try, so any exception below (a raising _await_ready, a full
            # registry, a version mismatch) detaches them instead of leaking
            # the kernel mutex handle / the mapping.
            try:
                mm = shm.buf
                # Aligned 8-byte-word view over the SAME buffer, used for
                # atomic seq/gen loads and stores (single `mov`, no torn
                # or stale bytes).
                d(self, "_mmq", mm.cast("Q"))
                lock = _Mutex(ns, mm, _OFF_LOCK)
                d(self, "_lock", lock)
                if created:
                    # Stamp our (live) identity into the fresh, zero-filled
                    # segment BEFORE any preemptible init work, so the tiny
                    # create->init gap is never observed as a (0,0) stillborn:
                    # a racer sees cpid alive and waits for readiness instead
                    # of reaping/claiming a segment a live creator still owns.
                    _REG_ENTRY.pack_into(mm, _OFF_CREATOR, _MY_PID, _MY_TOKEN)
                    try:
                        if _IS_WIN:
                            # Also hold the (kernel, out-of-segment) mutex
                            # across the whole init so a _claim_stillborn racer
                            # — which takes the same mutex when it sees a (0,0)
                            # stamp during our internal header-zeroing window —
                            # blocks until we publish state==1 instead of
                            # re-initing the segment under us and rewinding the
                            # allocator. (POSIX can't: the pthread mutex isn't
                            # initialised until _init_segment runs; there the
                            # stillborn path unlinks rather than claims in
                            # place, so no racer re-inits a live segment.)
                            with lock:
                                self._init_segment(slot_count)
                        else:
                            self._init_segment(slot_count)
                    except BaseException:
                        # Never leave a half-initialised (state==0) segment
                        # behind: nobody can ever resurrect it. Nobody is
                        # registered yet (state never reached 1), so marking
                        # dead + unlinking is safe; waiters detach and retry.
                        try:
                            _U32.pack_into(mm, _OFF_STATE, 2)
                            _safe_unlink(self._shm)
                        except Exception:
                            pass
                        raise                   # outer except detaches
                elif not self._await_ready():
                    self._detach_handles()      # segment died while attaching
                    time.sleep(0.005)
                    continue
                d(self, "_slot_count", _U32.unpack_from(mm, _OFF_SLOT_COUNT)[0])
                d(self, "_slots_off", _U64.unpack_from(mm, _OFF_SLOTS)[0])
                d(self, "_values_off", _U64.unpack_from(mm, _OFF_VALUES)[0])
                d(self, "_capacity", _U64.unpack_from(mm, _OFF_CAPACITY)[0])
                with lock:
                    # Serialized against close(): the last closer marks the
                    # segment dead under this same lock, so either we register
                    # first (the closer then sees a live other and does not
                    # mark it dead) or we observe the mark here and retry.
                    if _U32.unpack_from(mm, _OFF_STATE)[0] == 2:
                        dead = True
                    else:
                        dead = False
                        self._prune_and_maybe_wipe()
                        self._recover_freeze_locked()
                        if _U64.unpack_from(mm, _OFF_GEN)[0] & 1:
                            _bump_gen(mm)   # finish a dead bumper's transit
                        self._register_self()
                        # Count this instance while STILL holding the
                        # structural mutex. A sibling thread's close() decides
                        # "last local handle" from _NS_REFS and then
                        # deregisters the whole process under this same mutex
                        # (re-checking _NS_REFS there): counting inside the
                        # hold closes the window where an instance was
                        # registered in the segment but not yet in the local
                        # refcount — a sibling close could otherwise
                        # deregister/mark-dead beneath a live handle. Also
                        # keeps the account-before-anything-that-may-raise
                        # property (the geometry warning below may be
                        # configured as an error): an accounted instance is
                        # always cleanly closed by its atexit hook.
                        with _LIFE:
                            _NS_REFS[ns] = _NS_REFS.get(ns, 0) + 1
                            _INSTANCES.add(self)
            except BaseException:
                self._detach_handles()
                raise
            if dead:
                self._detach_handles()
                time.sleep(0.005)
                continue
            break
        else:
            raise RuntimeError(f"could not attach to namespace {ns!r}: "
                               f"segment kept dying during attach")
        d(self, "_closed", False)

        if not created and (capacity != _DEFAULT_CAPACITY
                            or slot_count != _DEFAULT_SLOTS) \
                and (self._capacity != capacity
                     or self._slot_count != slot_count):
            warnings.warn(
                f"namespace {ns!r} already exists with capacity="
                f"{self._capacity}, slot_count={self._slot_count}; the "
                f"requested capacity={capacity}, slot_count={slot_count} "
                f"are ignored (the first attacher fixes the geometry)",
                RuntimeWarning, stacklevel=2)

    def _detach_handles(self) -> None:
        """Release this instance's mapping/handles, swallowing errors."""
        try:
            self._lock.close()
        except Exception:
            pass
        # Release the derived 8-byte-word view before the base buffer, or
        # SharedMemory.close()'s mmap.close() raises "cannot close exported
        # pointer" while this second export is still alive.
        try:
            self._mmq.release()
        except Exception:
            pass
        try:
            self._mm.release()
        except Exception:
            pass
        try:
            self._shm.close()
        except Exception:
            pass
        # Any further access now reports "handle is closed" instead of a raw
        # released-memoryview ValueError from deep inside a read path.
        d = object.__setattr__
        d(self, "_mm", _RELEASED)
        d(self, "_mmq", _RELEASED)

    # ---- segment lifecycle ------------------------------------------------

    @staticmethod
    def _open_segment(name: str, capacity: int):
        """Attach by name or create; retry around races and dead segments."""
        last_err = None
        # A segment that is still 0 bytes / short is a creator mid-ftruncate,
        # not a foreign name: _attach_segment waits it out until this deadline
        # rather than rejecting a perfectly valid namespace (simultaneous
        # starts used to lose ~2.5% of their workers to that race).
        deadline = time.monotonic() + 2.0
        for _ in range(64):
            shm = None
            try:
                shm = _attach_segment(name, deadline)
            except FileNotFoundError:
                pass
            except ValueError:     # POSIX creator between shm_open and
                time.sleep(0.002)  # ftruncate: "cannot mmap an empty file"
                continue
            if shm is not None:
                if shm.size < _HEADER_SIZE:
                    # Name collision with a foreign (non-EasyGlobals) segment
                    # too small to hold our header: reading the state field
                    # would raise struct.error and leak this mapping.
                    sz = shm.size
                    shm.close()
                    raise RuntimeError(
                        f"shared-memory name {name!r} exists but is not an "
                        f"EasyGlobals segment ({sz} bytes)")
                if _U32.unpack_from(shm.buf, _OFF_STATE)[0] != 2:
                    return shm, False
                # Marked dead. Normally the closer unlinks it right after,
                # but if the closer was killed in that window the name
                # would stay dead forever (Linux). Never unlink it here —
                # another process may have already recreated the name, and
                # unlink-by-name could destroy the fresh segment. Instead
                # resurrect the dead segment in place under its own mutex.
                if Globals._try_resurrect(shm):
                    return shm, False
                shm.close()
                time.sleep(0.005)
                continue
            try:
                try:
                    shm = shared_memory.SharedMemory(name=name, create=True,
                                                     size=capacity, track=False)
                except TypeError:
                    shm = shared_memory.SharedMemory(name=name, create=True,
                                                     size=capacity)
                    _untrack(name)
                return shm, True
            except FileExistsError as e:
                last_err = e       # lost the creation race; attach on next spin
                time.sleep(0.002)
        raise RuntimeError(f"could not open shared segment {name!r}: {last_err}")

    @staticmethod
    def _try_resurrect(shm) -> bool:
        """Recover a state==2 (dead-marked) segment whose closer was killed
        between mark-dead and unlink. Returns True only when the caller may
        keep using the still-attached shm (Windows in-place revival); on
        POSIX it never revives in place — it only finishes the abandoned
        unlink and returns False, so the caller recreates a fresh segment."""
        mm = shm.buf
        if bytes(mm[_OFF_MAGIC:_OFF_MAGIC + 16]) != _MAGIC:
            return False
        # Reconstruct the mutex name exactly as __init__ does: fg_<ns>_v2.
        ns = shm.name.lstrip("/")[3:-3]
        if _IS_WIN:
            # A Windows section object is name-stable while any handle is
            # open (unlink is a no-op), so reviving IN PLACE is both safe and
            # the only way to reclaim it — there is no name to free.
            mutex = None
            try:
                mutex = _Mutex(ns, mm, _OFF_LOCK)
                with mutex:
                    state = _U32.unpack_from(mm, _OFF_STATE)[0]
                    if state == 1:
                        return True        # someone else resurrected it
                    if state != 2:
                        return False
                    for i in range(_REG_ENTRIES):
                        pid, tok = _REG_ENTRY.unpack_from(
                            mm, _OFF_REGISTRY + i * 16)
                        if pid and _pid_alive(pid, tok):
                            return False   # genuinely still in use
                    base = _OFF_REGISTRY
                    mm[base:base + _REG_ENTRIES * 16] = bytes(_REG_ENTRIES * 16)
                    Globals._wipe_table(mm)
                    _U64.pack_into(mm, _OFF_FREEZE, 0)
                    _U32.pack_into(mm, _OFF_STATE, 1)
                    return True
            except Exception:
                return False
            finally:
                if mutex is not None:
                    try:
                        mutex.close()
                    except Exception:
                        pass
        # POSIX: shm_unlink() truly frees the name even while old mappings
        # stay open, so holding this mapping cannot prove the name still
        # refers to this inode — flipping state 2->1 in place could revive a
        # nameless orphan, or (worse) a name already rebound to a healthy
        # successor. Only make progress on the original motivating case
        # (closer died between mark-dead and unlink) by finishing that unlink
        # ourselves, gated by a fresh-attach same-corpse re-verification.
        mutex = None
        try:
            mutex = _Mutex(ns, mm, _OFF_LOCK)
            with mutex:
                if _U32.unpack_from(mm, _OFF_STATE)[0] != 2:
                    return False
                for i in range(_REG_ENTRIES):
                    pid, tok = _REG_ENTRY.unpack_from(
                        mm, _OFF_REGISTRY + i * 16)
                    if pid and _pid_alive(pid, tok):
                        return False       # genuinely still in use
                fingerprint = bytes(mm[_OFF_CREATOR:_OFF_CREATOR + 16])
            probe = None
            try:
                try:
                    probe = _attach_segment(shm.name)
                except FileNotFoundError:
                    return False           # already unlinked: caller recreates
                if (_U32.unpack_from(probe.buf, _OFF_STATE)[0] == 2 and
                        bytes(probe.buf[_OFF_CREATOR:_OFF_CREATOR + 16])
                        == fingerprint):
                    with mutex:            # re-check under lock, then unlink
                        if _U32.unpack_from(mm, _OFF_STATE)[0] == 2:
                            try:
                                _safe_unlink(shm)
                            except Exception:
                                pass
            finally:
                if probe is not None:
                    try:
                        probe.close()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if mutex is not None:
                try:
                    mutex.close()
                except Exception:
                    pass
        return False                       # POSIX never revives in place

    def _init_segment(self, slot_count: int) -> None:
        mm, shm = self._mm, self._shm
        mm[:_HEADER_SIZE] = bytes(_HEADER_SIZE)
        _REG_ENTRY.pack_into(mm, _OFF_CREATOR, _MY_PID, _MY_TOKEN)
        self._lock.init_in_shm()

        slots_off = (_HEADER_SIZE + 63) & ~63
        values_off = (slots_off + slot_count * _SLOT_SIZE + 63) & ~63
        if values_off + 1024 > shm.size:
            raise ValueError(f"capacity {shm.size} too small for "
                             f"{slot_count} slots")

        _U32.pack_into(mm, _OFF_VERSION, _LAYOUT_VERSION)
        _U64.pack_into(mm, _OFF_CAPACITY, shm.size)
        _U32.pack_into(mm, _OFF_SLOT_COUNT, slot_count)
        _U64.pack_into(mm, _OFF_SLOTS, slots_off)
        _U64.pack_into(mm, _OFF_VALUES, values_off)
        _U64.pack_into(mm, _OFF_NEXT_FREE, values_off)
        mm[_OFF_MAGIC:_OFF_MAGIC + 16] = _MAGIC
        _U32.pack_into(mm, _OFF_STATE, 1)      # publish readiness last

    def _await_ready(self) -> bool:
        """True when the segment is initialised and live; False if it died
        or its creator did (caller detaches and retries)."""
        mm = self._mm
        deadline = time.monotonic() + 5.0
        while True:
            state = _U32.unpack_from(mm, _OFF_STATE)[0]
            if state == 2:
                return False
            if (bytes(mm[_OFF_MAGIC:_OFF_MAGIC + 16]) == _MAGIC
                    and state == 1):
                break
            if state == 0:
                cpid, ctok = _REG_ENTRY.unpack_from(mm, _OFF_CREATOR)
                stillborn = False
                if cpid:
                    # Creator identity present but dead: this segment can
                    # never become ready and (on Linux) would brick the
                    # namespace forever.
                    stillborn = not _pid_alive(cpid, ctok)
                elif time.monotonic() > deadline:
                    # Stamp still (0, 0) at the deadline: no real _init_segment
                    # takes ~5s, so either the creator died before it even
                    # identified itself, or a CPython/Win32 attach TOCTOU
                    # manufactured a zero-filled "ghost" under this name.
                    stillborn = True
                if stillborn:
                    if _IS_WIN:
                        # unlink() is a no-op on Windows; reclaim in place.
                        if self._claim_stillborn(cpid, ctok):
                            continue        # now state==1: re-read -> ready
                    elif not self._reap_stillborn(cpid, ctok):
                        raise RuntimeError(
                            f"shared-memory name {self._shm.name!r} exists "
                            f"but is not an EasyGlobals segment")
                    return False
            if time.monotonic() > deadline:
                raise RuntimeError("shared segment never became ready")
            time.sleep(0.001)
        ver = _U32.unpack_from(mm, _OFF_VERSION)[0]
        if ver != _LAYOUT_VERSION:
            raise RuntimeError(f"layout version mismatch ({ver}); "
                               f"restart all processes")
        return True

    def _reap_stillborn(self, cpid: int, ctok: int) -> bool:
        """Unlink a segment whose creator died before it became ready.
        The name is re-verified through a fresh attach right before the
        unlink so a recreated segment is never destroyed (a freshly
        created segment is zero-filled, so its creator stamp can't match
        a dead one); the remaining verify->unlink window is negligible
        against the seconds-old corpse.

        Returns False when the segment is provably NOT ours — no creator
        stamp and no magic, i.e. some other program's segment that merely
        shares the name. (A genuine stillborn does lack the magic, which is
        published at the end of _init_segment, so the stamp is what
        identifies it; the caller must not retry against a foreign one.)"""
        name = self._shm.name
        probe = None
        try:
            try:
                probe = _attach_segment(name)
            except FileNotFoundError:
                return True                 # already gone: caller recreates
            if _U32.unpack_from(probe.buf, _OFF_STATE)[0] == 0 \
                    and _REG_ENTRY.unpack_from(
                        probe.buf, _OFF_CREATOR) == (cpid, ctok):
                if not cpid and bytes(
                        probe.buf[_OFF_MAGIC:_OFF_MAGIC + 16]) != _MAGIC:
                    return False
                try:
                    _safe_unlink(self._shm)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass
        return True

    def _claim_stillborn(self, cpid: int, ctok: int) -> bool:
        """Windows: re-initialise a stillborn (state==0) segment IN PLACE
        under the structural named mutex. unlink() is a no-op on Windows, so
        a corpse can never be removed by name while any handle stays open —
        this mirrors _try_resurrect's claim-in-place strategy for state==2.
        The named kernel mutex lives outside the segment, so it is valid even
        though the creator never finished init. Racing claimers serialise on
        the mutex (the loser sees state==1 and just attaches). Called only
        once the creator is known dead (cpid!=0) or the readiness deadline
        elapsed with the stamp still (0, 0) — a stamp that can never revive.
        Returns True when the segment is ready afterwards."""
        try:
            mm = self._mm
            with self._lock:
                state = _U32.unpack_from(mm, _OFF_STATE)[0]
                if state == 1:
                    return True             # a racing sibling already published
                if state != 0:
                    return False            # marked dead meanwhile: retry fresh
                if _REG_ENTRY.unpack_from(mm, _OFF_CREATOR) != (cpid, ctok):
                    return False            # someone else took over the corpse
                if cpid and _pid_alive(cpid, ctok):
                    return False            # creator actually alive: don't stomp
                slots_off = (_HEADER_SIZE + 63) & ~63
                max_slots = (self._shm.size - slots_off - 1024) // _SLOT_SIZE
                if max_slots < 4:
                    return False            # too small to hold any variable
                self._init_segment(min(self._slot_count_arg, max_slots))
                return True
        except Exception:
            return False

    def _registry_iter(self):
        base = _OFF_REGISTRY
        for i in range(_REG_ENTRIES):
            off = base + i * 16
            pid, tok = _REG_ENTRY.unpack_from(self._mm, off)
            yield off, pid, tok

    def _prune_and_maybe_wipe(self, wipe: bool = True) -> None:
        """Under lock: drop dead attachers; wipe data unless some registered
        attacher is still alive (stale state must never leak into a new run
        — even when the previous run's last process died with an already
        empty registry, e.g. killed between deregistering and unlinking).
        `wipe=False` only prunes: a forked child continues the SAME run and
        inherits its parent's variables even after the parent exits."""
        mm = self._mm
        any_live = not wipe
        for off, pid, tok in self._registry_iter():
            if pid == 0:
                continue
            if (pid == _MY_PID and tok == _MY_TOKEN) or _pid_alive(pid, tok):
                any_live = True
            else:
                _REG_ENTRY.pack_into(mm, off, 0, 0)
        if not any_live:
            Globals._wipe_table(mm)        # no-op on a fresh segment

    @staticmethod
    def _wipe_table(mm) -> None:
        """Reset all variables (header bookkeeping + slot table)."""
        slots_off = _U64.unpack_from(mm, _OFF_SLOTS)[0]
        sc = _U32.unpack_from(mm, _OFF_SLOT_COUNT)[0]
        mm[slots_off:slots_off + sc * _SLOT_SIZE] = bytes(sc * _SLOT_SIZE)
        _U32.pack_into(mm, _OFF_LIVE, 0)
        _U32.pack_into(mm, _OFF_TOMB, 0)
        _U64.pack_into(mm, _OFF_NEXT_FREE, _U64.unpack_from(mm, _OFF_VALUES)[0])
        _bump_gen(mm)

    def _register_self(self) -> None:
        for off, pid, tok in self._registry_iter():
            if pid == _MY_PID and tok == _MY_TOKEN:
                return
        for off, pid, tok in self._registry_iter():
            if pid == 0:
                _REG_ENTRY.pack_into(self._mm, off, _MY_PID, _MY_TOKEN)
                return
        raise RuntimeError(f"attacher registry full ({_REG_ENTRIES} processes)")

    def close(self) -> None:
        """Detach. Ownership of every variable this process owns is released
        (a detached process must not wedge keys it can no longer write), and
        the last attached process unlinks the segment so no state outlives
        the program. Runs at interpreter exit automatically; idempotent."""
        ns = self._ns
        with _LIFE:
            if object.__getattribute__(self, "_closed"):
                return
            # Flip _closed BEFORE waiting on _slock below: threads that have
            # not entered a structural op yet now fail fast instead of
            # queueing ahead of us forever.
            object.__setattr__(self, "_closed", True)
            _INSTANCES.discard(self)
            counted = ns in _NS_REFS
            last_local = False
            if counted:
                remaining = _NS_REFS[ns] - 1
                if remaining > 0:
                    _NS_REFS[ns] = remaining
                else:
                    _NS_REFS.pop(ns, None)
                    last_local = True
        # _slock keeps the mapping alive until any in-flight structural op
        # (possibly parked in the in-shm mutex) has finished; unmapping under
        # such a thread faults when the mutex is handed over.
        with self._slock:
            if not last_local:
                # Either another Globals handle in this process still uses the
                # namespace, or this instance was never counted (its __init__
                # failed after attaching): keep the process registered, only
                # drop our mapping.
                self._detach_handles()
                return
            mm = self._mm
            try:
                with self._lock:
                    # Re-verify "last local handle" now that we hold the mutex:
                    # a sibling thread's __init__ registers AND counts under
                    # this same mutex, so if _NS_REFS has an entry again, a live
                    # handle (re)appeared after our decision above —
                    # deregistering the process now would pull the segment out
                    # from under it. Just drop our mapping instead.
                    with _LIFE:
                        revived = ns in _NS_REFS
                    if not revived:
                        self._release_ownership_locked()
                        others = False
                        for off, pid, tok in self._registry_iter():
                            if pid == 0:
                                continue
                            if pid == _MY_PID and tok == _MY_TOKEN:
                                _REG_ENTRY.pack_into(mm, off, 0, 0)
                            elif _pid_alive(pid, tok):
                                others = True
                        if not others \
                                and _U32.unpack_from(mm, _OFF_STATE)[0] != 2:
                            # (Already-dead means some other closer marked it
                            # and owns the unlink; re-unlinking BY NAME here
                            # could destroy a fresh segment that reused the
                            # name.)
                            _U32.pack_into(mm, _OFF_STATE, 2)   # mark dead
                            # Unlink while still holding the mutex: a
                            # resurrector must take this same mutex, so it can
                            # never revive the segment between our mark and our
                            # unlink (a deferred unlink-by-name could otherwise
                            # destroy the name of a segment someone just
                            # resurrected -> split-brain).
                            try:
                                _safe_unlink(self._shm)      # no-op on Windows
                            except Exception:
                                pass
            except Exception:
                pass
            self._detach_handles()

    def _release_ownership_locked(self) -> None:
        """Mutex held: give up every variable this process owns. A process
        that detached but keeps running must not wedge its keys against
        write/delete/disown by anyone else — it can no longer write them."""
        mm = self._mm
        base, sc = self._slots_off, self._slot_count
        for i in range(sc):
            so = base + i * _SLOT_SIZE
            f = _SLOT.unpack_from(mm, so)
            if f[1] == _S_OCCUPIED and f[5] == _MY_PID and f[6] == _MY_TOKEN:
                self._disown_locked(so, f)

    def __del__(self):
        # Reachable again since _INSTANCES stopped holding strong refs: a
        # handle dropped without close() releases its mapping here instead of
        # leaking it (and the process's registration) for the whole run.
        try:
            self.close()
        except Exception:
            pass                       # interpreter teardown: partial state

    # Pickle support: passing g to a child process reattaches by namespace,
    # carrying the geometry so a child that ends up re-creating the segment
    # (parent already gone) at least gets the intended sizing.
    def __reduce__(self):
        return (Globals, (self._ns, self._capacity_arg, self._slot_count_arg))

    # ---- lock-free machinery ------------------------------------------------

    def _read_slot_stable(self, so: int):
        """Read one slot, retrying while a write is in flight (odd seq)."""
        mm = self._mm
        mmq = self._mmq
        sw = so >> 3
        unpack = _SLOT.unpack_from
        spins = 0
        while True:
            f = unpack(mm, so)
            # Atomic re-read of the seq word: matching even value means no
            # write overlapped the struct read above.
            if not (f[0] & 1) and mmq[sw] == f[0]:
                return f
            spins += 1
            if spins > 200:
                time.sleep(0 if spins < 2000 else 0.0001)
                if spins > 50000:
                    self._heal_stuck(so)
                    spins = 0

    def _heal_stuck(self, so: int) -> None:
        """A slot stayed odd for seconds: its writer died mid-write. Under
        the mutex, tombstone the torn value so the namespace stays usable."""
        mm = self._mm
        with self._sync:
            f = _SLOT.unpack_from(mm, so)
            if not (f[0] & 1):
                return                          # recovered by itself
            if f[5]:
                if _pid_alive(f[5], f[6]):
                    return                      # writer alive, keep waiting
                self._tombstone_locked(so)      # dead writer: the blob is torn
                return
            # Owner 0: claiming a slot stamps ownership under this mutex, so
            # no write can be in flight here — the odd bit is a stray store
            # from a binding a relocation invalidated, and the blob itself is
            # whole. Republish ABOVE the stray odd instead of destroying a
            # perfectly good (disowned) variable.
            _seq_publish(mm, so, f[0] + 1)

    def _gen_stable(self) -> int:
        """Current generation, waiting out an in-flight bump (odd transit).
        Binders must never cache an in-flux value; if the bumper died
        mid-transit, finish its bump under the mutex."""
        mm = self._mm
        mmq = self._mmq
        spins = 0
        while True:
            g = mmq[_OFF_GEN_W]
            if not (g & 1):
                return g
            spins += 1
            if spins > 200:
                time.sleep(0.0001)
                if spins > 5000:
                    with self._sync:
                        if mmq[_OFF_GEN_W] & 1:
                            _bump_gen(mm)      # folds the dead transit in
                    spins = 0

    def _find(self, nb: bytes, h: int):
        """Lock-free probe. Returns (slot_off, fields) or (None, None).
        Retries if the table generation moved underneath the probe."""
        mm = self._mm
        sc = self._slot_count
        base = self._slots_off
        for _attempt in range(64):
            gen0 = self._gen_stable()
            idx = h % sc
            for _ in range(sc):
                so = base + idx * _SLOT_SIZE
                f = self._read_slot_stable(so)
                state = f[1]
                if state == _S_EMPTY:
                    break
                if (state == _S_OCCUPIED and f[2] == h and f[4] == len(nb)
                        and bytes(mm[f[3]:f[3] + f[4]]) == nb):
                    return so, f, gen0
                idx += 1
                if idx == sc:
                    idx = 0
            if self._mmq[_OFF_GEN_W] == gen0:
                return None, None, gen0
        raise RuntimeError("lookup livelock (generation kept changing)")

    def _probe_locked(self, nb: bytes, h: int):
        """Under mutex: returns (slot_off, found, fields, insert_off)."""
        mm = self._mm
        sc = self._slot_count
        base = self._slots_off
        idx = h % sc
        first_tomb = -1
        for _ in range(sc):
            so = base + idx * _SLOT_SIZE
            f = _SLOT.unpack_from(mm, so)
            state = f[1]
            if state == _S_EMPTY:
                return so, False, f, (first_tomb if first_tomb >= 0 else so)
            if state == _S_TOMB:
                if first_tomb < 0:
                    first_tomb = so
            elif f[2] == h and f[4] == len(nb) \
                    and bytes(mm[f[3]:f[3] + f[4]]) == nb:
                return so, True, f, so
            idx += 1
            if idx == sc:
                idx = 0
        if first_tomb >= 0:
            return first_tomb, False, None, first_tomb
        raise RuntimeError("hash table full; recreate with larger slot_count")

    # ---- allocator + compaction (mutex held) --------------------------------

    def _alloc_locked(self, n: int) -> int:
        mm = self._mm
        nf = _U64.unpack_from(mm, _OFF_NEXT_FREE)[0]
        if nf + n > self._capacity:
            self._compact_locked()
            nf = _U64.unpack_from(mm, _OFF_NEXT_FREE)[0]
            if nf + n > self._capacity:
                raise MemoryError(
                    f"namespace {self._ns!r} out of space: need {n} more "
                    f"bytes, capacity {self._capacity}")
        _U64.pack_into(mm, _OFF_NEXT_FREE, nf + n)
        return nf

    def _unalloc_locked(self, off: int) -> None:
        """Undo the bump the _alloc_locked() call that returned `off` just
        made, when we discard the allocation instead of linking it into a
        slot. Valid ONLY as the next thing done with that return value:
        next_free is still off+n (the mutex is held, nothing else runs), so
        this restores exactly the cursor that call left — no leak. Without it,
        _set_slow's compaction-restart loop abandons a fresh allocation on
        every retry and can spuriously exhaust capacity even single-process."""
        _U64.pack_into(self._mm, _OFF_NEXT_FREE, off)

    def _freeze_and_settle(self) -> None:
        """Stop lock-free writers: raise the freeze flag, give in-flight
        fast-path writes far longer than any store-buffer drain to either
        finish or notice the flag, then wait until every slot is even."""
        mm = self._mm
        _REG_ENTRY.pack_into(mm, _OFF_FREEZER, _MY_PID, _MY_TOKEN)
        _U64.pack_into(mm, _OFF_FREEZE, 1)
        time.sleep(0.002)
        base, sc = self._slots_off, self._slot_count
        mmq = self._mmq
        for i in range(sc):
            so = base + i * _SLOT_SIZE
            deadline = time.monotonic() + 5.0
            while mmq[so >> 3] & 1:               # atomic seq load
                f = _SLOT.unpack_from(mm, so)
                if not f[5]:
                    # Unowned: no write can be in flight (claiming a slot
                    # stamps ownership under this mutex), so this is a stray
                    # odd store from an invalidated binding, not a torn value
                    # — republish above it instead of dropping the variable.
                    _seq_publish(mm, so, f[0] + 1)
                    break
                if not _pid_alive(f[5], f[6]):
                    _SLOT.pack_into(mm, so, f[0], _S_TOMB,
                                    0, 0, 0, 0, 0, 0, 0, 0)
                    _bump_gen(mm)                # pre-publish replay immunity
                    _seq_publish(mm, so, f[0] + 3)   # skip-publish: f[0] is odd
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("writer stuck while freezing")
                time.sleep(0.0001)

    def _recover_freeze_locked(self) -> None:
        """Under mutex: a process killed during compaction/clear leaves the
        freeze flag up forever, silently demoting every fast-path write to
        the mutex path. If the recorded freezer is dead, repair: tombstone
        odd slots with dead writers, recount, bump gen, lift the flag."""
        mm = self._mm
        if not _U64.unpack_from(mm, _OFF_FREEZE)[0]:
            return
        fpid, ftok = _REG_ENTRY.unpack_from(mm, _OFF_FREEZER)
        if fpid and _pid_alive(fpid, ftok):
            return                         # legitimately frozen right now
        base, sc = self._slots_off, self._slot_count
        nlive = ntomb = 0
        for i in range(sc):
            so = base + i * _SLOT_SIZE
            f = _SLOT.unpack_from(mm, so)
            if f[0] & 1 and not f[5]:
                _seq_publish(mm, so, f[0] + 1)   # stray bump, intact blob
                f = _SLOT.unpack_from(mm, so)
            elif f[0] & 1 and not _pid_alive(f[5], f[6]):
                _SLOT.pack_into(mm, so, f[0], _S_TOMB,
                                0, 0, 0, 0, 0, 0, 0, 0)
                _bump_gen(mm)                # pre-publish replay immunity
                _seq_publish(mm, so, f[0] + 3)   # skip-publish: f[0] is odd
                f = _SLOT.unpack_from(mm, so)
            if f[1] == _S_OCCUPIED:
                nlive += 1
            elif f[1] == _S_TOMB:
                ntomb += 1
        _U32.pack_into(mm, _OFF_LIVE, nlive)
        _U32.pack_into(mm, _OFF_TOMB, ntomb)
        _bump_gen(mm)
        _U64.pack_into(mm, _OFF_FREEZE, 0)
        _REG_ENTRY.pack_into(mm, _OFF_FREEZER, 0, 0)

    def _compact_locked(self) -> None:
        """Rewrite all live key/value blobs contiguously, dropping garbage
        from overwrites and deletes. Slot seqs only ever increase.

        Cannot fail for lack of space: re-packed capacities never exceed
        their previous reservation (min of the old cap and a fresh
        rounding), so the rebuilt region is at most the sum of the live
        blocks that were already allocated — no allocator calls, no
        recursion. If an async exception (KeyboardInterrupt) lands
        mid-rebuild, the in-memory snapshot is replayed so no variable is
        lost; the outer finally lifts the freeze flag even when settling
        itself failed."""
        mm = self._mm
        settled = False
        try:
            self._freeze_and_settle()
            settled = True
            base, sc = self._slots_off, self._slot_count
            live = []
            for i in range(sc):
                so = base + i * _SLOT_SIZE
                f = _SLOT.unpack_from(mm, so)
                if f[1] == _S_OCCUPIED:
                    live.append((f, bytes(mm[f[3]:f[3] + f[4]]),
                                 bytes(mm[f[7]:f[7] + f[8]])))
            done = False
            try:
                self._rebuild_locked(live)
                done = True
            finally:
                if not done:
                    try:
                        self._rebuild_locked(live)   # replay the snapshot
                    except BaseException:
                        # last resort: keep the table readable/consistent
                        for i in range(sc):
                            so = base + i * _SLOT_SIZE
                            s = _U64.unpack_from(mm, so)[0]
                            if s & 1:
                                _seq_publish(mm, so, s + 1)
                        nlive = ntomb = 0
                        for i in range(sc):
                            st = mm[base + i * _SLOT_SIZE + 8]
                            if st == _S_OCCUPIED:
                                nlive += 1
                            elif st == _S_TOMB:
                                ntomb += 1
                        _U32.pack_into(mm, _OFF_LIVE, nlive)
                        _U32.pack_into(mm, _OFF_TOMB, ntomb)
                        _bump_gen(mm)
                        raise
        finally:
            if not settled:
                # _freeze_and_settle raised (a genuinely-stuck live writer):
                # it may have force-tombstoned dead-writer slots without
                # touching the counters. Recount from the state bytes only —
                # never republish an odd slot, which could be the stuck live
                # writer's in-flight value.
                base, sc = self._slots_off, self._slot_count
                nlive = ntomb = 0
                for i in range(sc):
                    st = mm[base + i * _SLOT_SIZE + 8]
                    if st == _S_OCCUPIED:
                        nlive += 1
                    elif st == _S_TOMB:
                        ntomb += 1
                _U32.pack_into(mm, _OFF_LIVE, nlive)
                _U32.pack_into(mm, _OFF_TOMB, ntomb)
            _U64.pack_into(mm, _OFF_FREEZE, 0)
            _REG_ENTRY.pack_into(mm, _OFF_FREEZER, 0, 0)

    def _rebuild_locked(self, live) -> None:
        """Mark every slot odd, wipe the table, bump gen (BEFORE any slot
        is re-released, so a reader validating gen after its copy can
        never accept a value from a relocated slot), then re-pack the
        snapshot with a local cursor and release. Idempotent: safe to
        replay after a partial run."""
        mm = self._mm
        base, sc = self._slots_off, self._slot_count
        for i in range(sc):
            so = base + i * _SLOT_SIZE
            s = _U64.unpack_from(mm, so)[0]
            sodd = s if s & 1 else s + 1
            _SLOT.pack_into(mm, so, sodd, _S_EMPTY, 0, 0, 0, 0, 0, 0, 0, 0)
        _bump_gen(mm)
        cursor = self._values_off
        nlive = 0
        for f, kb, vb in live:
            koff = cursor
            cursor += len(kb)
            mm[koff:koff + len(kb)] = kb
            cap = _round_cap(len(vb))
            if f[9] and cap > f[9]:
                cap = f[9]                # never inflate: guarantees the fit
            voff = cursor
            cursor += cap
            mm[voff:voff + len(vb)] = vb
            ins = self._probe_locked(kb, f[2])[3]
            s = _U64.unpack_from(mm, ins)[0]          # odd right now
            sodd = s if s & 1 else s + 1
            _SLOT.pack_into(mm, ins, sodd, _S_OCCUPIED, f[2], koff,
                            f[4], f[5], f[6], voff, f[8], cap)
            _seq_publish(mm, ins, sodd + 1)
            nlive += 1
        _U64.pack_into(mm, _OFF_NEXT_FREE, cursor)
        # Release the slots that stayed empty back to even.
        for i in range(sc):
            so = base + i * _SLOT_SIZE
            s = _U64.unpack_from(mm, so)[0]
            if s & 1:
                _seq_publish(mm, so, s + 1)
        _U32.pack_into(mm, _OFF_LIVE, nlive)
        _U32.pack_into(mm, _OFF_TOMB, 0)

    # ---- write ---------------------------------------------------------------

    def _write_blob(self, voff: int, tag: int, header: bytes, payload,
                    n: int) -> None:
        # payload is bytes, bytearray or a 1-D byte memoryview (numpy). `n`
        # is the payload length captured when the slot's capacity was sized;
        # we copy EXACTLY n bytes regardless of what len(payload) reports
        # now, so a bytearray (or the buffer behind a numpy view) that another
        # thread resizes mid-write can never overflow the reservation and
        # trample the neighbouring variable's blob. A payload that shrank is
        # zero-filled to n (no stale tail); one that grew is clamped to n.
        mm = self._mm
        mm[voff] = tag
        p = voff + 1
        if header:
            mm[p:p + len(header)] = header
            p += len(header)
        if n:
            cur = len(payload)
            if cur >= n:
                mm[p:p + n] = payload[:n]
            else:
                mm[p:p + cur] = payload[:cur]
                mm[p + cur:p + n] = bytes(n - cur)

    def _set(self, name: str, value: Any, *,
             _u64p=_U64.pack_into, _seqval=_SEQVAL.unpack_from,
             _gen_w=_OFF_GEN_W, _frz_w=_OFF_FREEZE_W, _vlen_off=_OFFV + 8,
             _int=int, _float=float, _enc=_encode) -> None:
        mm = self._mm
        mmq = self._mmq                    # atomic 8-byte-word view
        with self._wlock:
            # Small fixed-width values (the hottest case: counters, states)
            # skip _encode entirely: tag+payload go into shm in one C call.
            t = type(value)
            if t is _int and _INT64_MIN <= value <= _INT64_MAX:
                small, tag = _TAG_I64, _T_INT
            elif t is _float:
                small, tag = _TAG_F64, _T_FLOAT
            else:
                small = None
            if small is not None:
                ent = self._cache.get(name)
                if ent is not None and ent[3] and ent[1] == mmq[_gen_w]:
                    so = ent[0]
                    sw = so >> 3
                    s, voff, vlen, vcap = _seqval(mm, so)
                    if not (s & 1) and vcap:     # vcap >= 16 >= 9 if intact
                        # ---- lock-free fast path ----
                        mmq[sw] = s + 1                      # seq -> odd (atomic)
                        if mmq[_gen_w] != ent[1]:
                            self._back_out(name, sw, s)      # see helper
                        elif mmq[_frz_w]:
                            mmq[sw] = s        # ours: settle awaits evenness
                        else:
                            small.pack_into(mm, voff, tag, value)
                            if vlen != 9:
                                _u64p(mm, so + _vlen_off, 9)
                            mmq[sw] = s + 2                  # seq -> even (atomic)
                            return
                self._set_slow(name, tag, b"",
                               (_I64 if tag == _T_INT else _F64).pack(value),
                               9)
                return

            tag, header, payload = _enc(value)
            # Capture the payload length ONCE so the allocation size and the
            # write extent agree even if `payload` (a bytearray, or the buffer
            # behind a numpy view) is mutated concurrently mid-write.
            n0 = len(payload)
            blob_len = 1 + len(header) + n0
            ent = self._cache.get(name)
            if ent is not None and ent[3] and ent[1] == mmq[_gen_w]:
                so = ent[0]
                sw = so >> 3
                s, voff, vlen, vcap = _seqval(mm, so)
                if not (s & 1) and blob_len <= vcap:
                    # ---- lock-free fast path ----
                    mmq[sw] = s + 1                          # seq -> odd (atomic)
                    # Re-validate gen AND freeze only now: a compaction that
                    # ran entirely inside a preemption window since the gen
                    # check above shows up as a gen change; one in progress
                    # shows up as freeze; one starting later is held off by
                    # our odd seq (its settle waits for us).
                    if mmq[_gen_w] != ent[1]:
                        self._back_out(name, sw, s)          # see helper
                    elif mmq[_frz_w]:
                        mmq[sw] = s            # ours: settle awaits evenness
                    else:
                        try:
                            self._write_blob(voff, tag, header, payload, n0)
                        except BaseException:
                            # mid-write failure with the slot odd: the old
                            # value is already destroyed — tombstone so
                            # readers don't spin on an odd seq forever
                            self._tombstone_torn(name, so)
                            raise
                        if vlen != blob_len:
                            _u64p(mm, so + _vlen_off, blob_len)
                        mmq[sw] = s + 2                      # seq -> even (atomic)
                        return
            self._set_slow(name, tag, header, payload, blob_len)

    def _back_out(self, name: str, sw: int, s: int) -> None:
        """The generation moved between our cached check and the odd bump: a
        structural op COMPLETED inside our preemption window, so the slot may
        belong to a different variable now. NOTHING has been written yet, so
        restore the sequence we found and drop the binding; the caller
        re-resolves under the mutex.

        Restoring immediately is what matters: a slot left odd while its
        writer waits for the structural mutex deadlocks against
        _freeze_and_settle (which holds that mutex until every slot is even)
        and invites the recovery paths to read 'odd, no live owner' as a torn
        value. Readers merely retry across the odd blip; the store order
        (odd, validate, restore) needs no fence on x86-64's ordered stores.
        """
        self._mmq[sw] = s
        self._cache.pop(name, None)

    def _tombstone_torn(self, name: str, so: int) -> None:
        """Best effort: tombstone a slot we left odd after a failed write."""
        try:
            with self._sync:
                self._tombstone_locked(so)
            self._cache.pop(name, None)
        except Exception:
            pass

    def _tombstone_locked(self, so: int) -> None:
        mm = self._mm
        s = _U64.unpack_from(mm, so)[0]
        # Same replay defenses as _set_slow's found path: a foreign odd seq
        # may be a regressed counter, so sodd+1 could replay an even value the
        # slot already exposed with different (possibly half-written) bytes —
        # skip past it AND advance the generation before the publish.
        if s & 1:
            sodd, spub = s, s + 3
            _bump_gen(mm)                # pre-publish replay immunity
        else:
            sodd, spub = s + 1, s + 2
        _SLOT.pack_into(mm, so, sodd, _S_TOMB, 0, 0, 0, 0, 0, 0, 0, 0)
        _seq_publish(mm, so, spub)
        _U32.pack_into(mm, _OFF_LIVE,
                       max(0, _U32.unpack_from(mm, _OFF_LIVE)[0] - 1))
        _U32.pack_into(mm, _OFF_TOMB,
                       _U32.unpack_from(mm, _OFF_TOMB)[0] + 1)
        _bump_gen(mm)

    def _set_slow(self, name: str, tag, header, payload, blob_len) -> None:
        nb = name.encode("utf-8")
        h = zlib.crc32(nb)
        n = blob_len - 1 - len(header)      # payload length sized into the slot
        mm = self._mm
        with self._sync:
            g0, frz = _QQ.unpack_from(mm, _OFF_GEN)
            if frz:
                self._recover_freeze_locked()   # heal a leaked freeze flag
            if g0 & 1:
                _bump_gen(mm)                   # finish a dead bumper's transit
            # _alloc_locked may compact, which rebuilds the slot table and
            # reallocates every key/value blob — invalidating the probe
            # result and any offsets taken from it. A gen change is the
            # tell; restart the whole locked section when one happens (rolling
            # back the just-made allocation first, or it leaks and a growth
            # near capacity spuriously exhausts space even single-process).
            for _attempt in range(8):
                gen0 = _U64.unpack_from(mm, _OFF_GEN)[0]
                so, found, f, ins = self._probe_locked(nb, h)
                if found:
                    owner_pid, owner_tok = f[5], f[6]
                    mine = owner_pid == _MY_PID and owner_tok == _MY_TOKEN
                    if not mine and owner_pid \
                            and _pid_alive(owner_pid, owner_tok):
                        raise OwnershipError(
                            f"{name!r} is owned by pid {owner_pid}; only the "
                            f"owning process may write it (see disown())")
                    # A genuine transfer from a (presumed-dead) other owner
                    # must invalidate that owner's still-cached owned=True
                    # binding in every process — same reason disown() bumps
                    # gen — or a stale writer keeps taking the lock-free path
                    # and two writers race the slot.
                    took_over = bool(owner_pid) and not mine
                    voff, vlen, vcap = f[7], f[8], f[9]
                    if blob_len > vcap:
                        vcap = _round_cap(blob_len)
                        voff = self._alloc_locked(vcap)
                        if _U64.unpack_from(mm, _OFF_GEN)[0] != gen0:
                            self._unalloc_locked(voff)
                            continue            # compacted: so/f are stale
                    s = _U64.unpack_from(mm, so)[0]
                    # Adopt the odd phase of a dead writer — but NEVER publish
                    # the value right above a foreign odd seq. An odd seq here
                    # can also be a fast-path writer's blind s+1 store that
                    # REGRESSED a relocated slot (see _repair_bumped) while
                    # its repair waits on this same mutex; publishing sodd+1
                    # would then replay an even value the slot already exposed,
                    # with different data, and a reader whose copy spanned the
                    # relocation could validate a torn mix. Two layers close
                    # this: skip to sodd+3 (past the directly regressed
                    # publish), and bump the generation BEFORE the publish so
                    # a reader matching any replayed counter from a deeper
                    # history still fails its gen validation and re-resolves.
                    if s & 1:
                        sodd, spub = s, s + 3
                    else:
                        sodd, spub = s + 1, s + 2
                    _SLOT.pack_into(mm, so, sodd, _S_OCCUPIED, h, f[3], f[4],
                                    _MY_PID, _MY_TOKEN, voff, blob_len, vcap)
                    try:
                        self._write_blob(voff, tag, header, payload, n)
                    except BaseException:
                        self._tombstone_locked(so)   # bumps gen itself
                        self._cache.pop(name, None)
                        raise
                    if s & 1:
                        _bump_gen(mm)            # pre-publish replay immunity
                    _seq_publish(mm, so, spub)
                    if took_over and not (s & 1):
                        _bump_gen(mm)            # invalidate the old owner's cache
                    if took_over or (s & 1):
                        gen0 = _U64.unpack_from(mm, _OFF_GEN)[0]
                else:
                    live = _U32.unpack_from(mm, _OFF_LIVE)[0]
                    if (live + 1) * _LOAD_DEN > self._slot_count * _LOAD_NUM:
                        raise RuntimeError(
                            f"namespace {self._ns!r} is full ({live} keys); "
                            f"recreate with a larger slot_count")
                    koff = self._alloc_locked(len(nb))
                    if _U64.unpack_from(mm, _OFF_GEN)[0] != gen0:
                        self._unalloc_locked(koff)
                        continue                # compacted: ins is stale
                    mm[koff:koff + len(nb)] = nb
                    vcap = _round_cap(blob_len)
                    voff = self._alloc_locked(vcap)
                    if _U64.unpack_from(mm, _OFF_GEN)[0] != gen0:
                        self._unalloc_locked(voff)
                        continue                # compacted: ins/koff are stale
                    self._write_blob(voff, tag, header, payload, n)
                    so = ins
                    prev_state = _SLOT.unpack_from(mm, so)[1]
                    s = _U64.unpack_from(mm, so)[0]
                    sodd = s if s & 1 else s + 1
                    _SLOT.pack_into(mm, so, sodd, _S_OCCUPIED, h, koff, len(nb),
                                    _MY_PID, _MY_TOKEN, voff, blob_len, vcap)
                    _seq_publish(mm, so, sodd + 1)
                    _U32.pack_into(mm, _OFF_LIVE, live + 1)
                    if prev_state == _S_TOMB:
                        _U32.pack_into(
                            mm, _OFF_TOMB,
                            max(0, _U32.unpack_from(mm, _OFF_TOMB)[0] - 1))
                self._cache[name] = [so, gen0, h, True]
                return
            raise RuntimeError(
                f"namespace {self._ns!r}: write kept racing compaction")

    # ---- read ----------------------------------------------------------------

    def _read_value(self, so: int, gen: int, *,
                    _sv=_STATEVAL.unpack_from,
                    _i64u=_I64.unpack_from, _f64u=_F64.unpack_from,
                    _gen_w=_OFF_GEN_W, _occ=_S_OCCUPIED,
                    _sleep=time.sleep):
        """Lock-free value read from one slot. Returns the decoded value,
        _REBOUND if the binding moved (caller re-resolves), or never returns
        torn data:

        A copied value is accepted only if BOTH the slot seq is unchanged
        (no write overlapped the copy) AND the table generation still equals
        `gen`, the generation under which the name->slot binding was made
        (no delete/clear/compaction relocated the slot — without this a
        reader preempted across a compaction could return the value of
        whatever variable now lives at this slot offset). Compaction bumps
        gen before re-releasing any slot, so the pair of checks suffices.
        Seq and gen are read through the atomic word view (self._mmq) so the
        validating comparisons can never accept a torn/stale control word.
        """
        mm = self._mm
        mmq = self._mmq
        sw = so >> 3
        cap = self._capacity
        spins = 0
        while True:
            s1 = mmq[sw]
            if s1 & 1:
                spins += 1
                if spins > 200:
                    _sleep(0 if spins < 2000 else 0.0001)
                    if spins > 50000:
                        self._heal_stuck(so)
                        spins = 0
                continue
            state, voff, vlen = _sv(mm, so)
            if state != _occ:
                return _REBOUND          # deleted/relocated: re-resolve
            if not vlen or voff + vlen > cap:
                # Normally a torn read of an in-flight write (every real blob
                # is >= 1B and within capacity) — the next spin sees the odd
                # seq and retries. But a genuinely corrupt OCCUPIED slot with
                # an even seq would loop forever here, so bound it and hand
                # back to the caller to re-resolve rather than busy-hang.
                spins += 1
                if spins > 50000:
                    return _REBOUND
                if spins > 200:
                    _sleep(0 if spins < 2000 else 0.0001)
                continue                 # torn read
            tag = mm[voff]               # may be torn; seq check below
            if tag == _T_INT and vlen == 9:
                v = _i64u(mm, voff + 1)[0]
                if mmq[sw] == s1:
                    if mmq[_gen_w] == gen:
                        return v
                    return _REBOUND
            elif tag == _T_BYTES:
                v = bytes(mm[voff + 1:voff + vlen])      # single copy
                if mmq[sw] == s1:
                    if mmq[_gen_w] == gen:
                        return v
                    return _REBOUND
            elif tag == _T_STR:
                try:                     # decode straight from shm: 1 pass
                    v = str(mm[voff + 1:voff + vlen], "utf-8")
                except UnicodeDecodeError:
                    if mmq[sw] != s1:
                        continue         # torn buffer mid-decode: retry
                    raise
                if mmq[sw] == s1:
                    if mmq[_gen_w] == gen:
                        return v
                    return _REBOUND
            elif tag == _T_FLOAT and vlen == 9:
                v = _f64u(mm, voff + 1)[0]
                if mmq[sw] == s1:
                    if mmq[_gen_w] == gen:
                        return v
                    return _REBOUND
            elif tag in (_T_NONE, _T_TRUE, _T_FALSE) and vlen == 1:
                if mmq[sw] == s1:
                    if mmq[_gen_w] == gen:
                        return (None, True, False)[tag - 1]
                    return _REBOUND
            else:
                blob = bytearray(mm[voff:voff + vlen])
                if mmq[sw] == s1 and blob:
                    if mmq[_gen_w] == gen:
                        return _decode(blob)
                    return _REBOUND

    def _peek(self, name: str, *, _gen_w=_OFF_GEN_W):
        """Lock-free read; returns _MISSING if not present."""
        ent = self._cache.get(name)
        if ent is not None and ent[1] == self._mmq[_gen_w]:
            v = self._read_value(ent[0], ent[1])
            if v is not _REBOUND:
                return v
        # full lookup; restart from _find whenever the table moves under us
        nb = name.encode("utf-8")
        h = zlib.crc32(nb)
        for _attempt in range(64):
            so, f, gen0 = self._find(nb, h)
            if so is None:
                return _MISSING
            self._cache[name] = [so, gen0, h,
                                 f[5] == _MY_PID and f[6] == _MY_TOKEN]
            v = self._read_value(so, gen0)
            if v is not _REBOUND:
                return v
        raise RuntimeError("read livelock (table kept changing)")

    # ---- delete / disown ------------------------------------------------------

    def _delete(self, name: str) -> bool:
        nb = name.encode("utf-8")
        h = zlib.crc32(nb)
        mm = self._mm
        with self._wlock, self._sync:
            so, found, f, _ = self._probe_locked(nb, h)
            if not found:
                return False
            if f[5] and not (f[5] == _MY_PID and f[6] == _MY_TOKEN) \
                    and _pid_alive(f[5], f[6]):
                raise OwnershipError(
                    f"{name!r} is owned by pid {f[5]}; only the owner "
                    f"may delete it")
            self._tombstone_locked(so)
            # Heavy create/delete churn accumulates tombstones, degrading
            # absent-key probes toward full-table scans; compaction is the
            # operation that clears them. Amortized: at most one compaction
            # per slot_count/4 deletes.
            if _U32.unpack_from(mm, _OFF_TOMB)[0] * 4 > self._slot_count:
                self._compact_locked()
        self._cache.pop(name, None)
        return True

    def disown(self, name: str) -> None:
        """Give up write ownership so another process may claim the variable."""
        nb = name.encode("utf-8")
        h = zlib.crc32(nb)
        with self._wlock, self._sync:
            so, found, f, _ = self._probe_locked(nb, h)
            if not found:
                raise KeyError(name)
            if f[5] and not (f[5] == _MY_PID and f[6] == _MY_TOKEN) \
                    and _pid_alive(f[5], f[6]):
                raise OwnershipError(f"{name!r} is owned by pid {f[5]}")
            self._disown_locked(so, f)
        self._cache.pop(name, None)

    def _disown_locked(self, so: int, f) -> None:
        """Mutex held: clear one slot's owner fields, keeping the value."""
        mm = self._mm
        s = _U64.unpack_from(mm, so)[0]
        if s & 1:
            if f[5] and not _pid_alive(f[5], f[6]):
                # The owner was killed mid-write: the blob is torn.
                # Republishing it as stable would hand readers garbage —
                # tombstone it instead; the variable's last value is lost.
                self._tombstone_locked(so)
                return
            # Unowned (or ours, and we hold _wlock): no write is in flight,
            # so this is a stray odd store from an invalidated binding and
            # the blob is whole — just step over it.
            s += 1
        _SLOT.pack_into(mm, so, s + 1, _S_OCCUPIED, f[2], f[3], f[4],
                        0, 0, f[7], f[8], f[9])
        _seq_publish(mm, so, s + 2)
        # Ownership changed: invalidate every process's cached owned=True
        # binding (a sibling handle of the old owner would otherwise keep
        # writing lock-free after someone else claims the variable).
        _bump_gen(mm)

    # ---- public dunder / dict-style API ---------------------------------------

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        elif name in _RESERVED:
            # g.get = 5 would store the value but g.get would still return
            # the bound method — fail loudly at the write site instead.
            raise AttributeError(
                f"{name!r} collides with a Globals method; use "
                f"g[{name!r}] = ... and g[{name!r}] for this name")
        else:
            self._set(name, value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        v = self._peek(name)
        if v is _MISSING:
            raise AttributeError(
                f"no shared variable {name!r} in namespace {self._ns!r}")
        return v

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            object.__delattr__(self, name)
            return
        if not self._delete(name):
            raise AttributeError(
                f"no shared variable {name!r} in namespace {self._ns!r}")

    def __setitem__(self, name: str, value: Any) -> None:
        if not isinstance(name, str):
            raise TypeError(f"shared-variable names must be str, not "
                            f"{type(name).__name__}")
        self._set(name, value)

    def __getitem__(self, name: str) -> Any:
        if not isinstance(name, str):
            raise TypeError(f"shared-variable names must be str, not "
                            f"{type(name).__name__}")
        v = self._peek(name)
        if v is _MISSING:
            raise KeyError(name)
        return v

    def __delitem__(self, name: str) -> None:
        if not isinstance(name, str):
            raise TypeError(f"shared-variable names must be str, not "
                            f"{type(name).__name__}")
        if not self._delete(name):
            raise KeyError(name)

    def get(self, name: str, default: Any = None) -> Any:
        if not isinstance(name, str):
            raise TypeError(f"shared-variable names must be str, not "
                            f"{type(name).__name__}")
        v = self._peek(name)
        return default if v is _MISSING else v

    def __contains__(self, name: str) -> bool:
        if not isinstance(name, str):
            return False
        return self._peek(name) is not _MISSING

    def __len__(self) -> int:
        # LIVE is the high half of the aligned 8-byte word at offset 32
        # (SLOT_COUNT is the low half): one atomic load removes read-side
        # tearing. (Mutex holders still store the counter byte-wise, so the
        # value stays advisory — momentarily stale, never garbage on read.)
        return self._mmq[_OFF_SLOT_COUNT >> 3] >> 32

    def keys(self) -> list[str]:
        mm = self._mm
        out = []
        with self._sync:
            base, sc = self._slots_off, self._slot_count
            for i in range(sc):
                f = _SLOT.unpack_from(mm, base + i * _SLOT_SIZE)
                if f[1] == _S_OCCUPIED:
                    out.append(bytes(mm[f[3]:f[3] + f[4]]).decode("utf-8"))
        return out

    def items(self) -> list:
        """Snapshot of (name, value) pairs. Race-tolerant: a name deleted
        between the key scan and its read is skipped rather than raising."""
        out = []
        for k in self.keys():
            v = self._peek(k)
            if v is not _MISSING:
                out.append((k, v))
        return out

    def values(self) -> list:
        """Snapshot of values (see items() for the race semantics)."""
        return [v for _, v in self.items()]

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def owner_of(self, name: str) -> int | None:
        """Pid of the process that owns the variable, or None when it is
        unowned, missing, or its recorded owner has died — i.e. None means
        'this process may write it', matching what the write paths enforce."""
        nb = name.encode("utf-8")
        so, f, _ = self._find(nb, zlib.crc32(nb))
        if so is None or not f[5] or not _pid_alive(f[5], f[6]):
            return None
        return f[5]

    def wait_for(self, name: str, timeout: float | None = None) -> Any:
        """Block until another process publishes `name`; return its value.

        The delivered value becomes wait_change()'s reference point, so the
        usual `wait_for` then `wait_change` loop cannot swallow a publish
        that lands while the caller is still processing the first value."""
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = 0.00005
        while True:
            v = self._peek(name)
            if v is not _MISSING:
                ent = self._cache.get(name)
                if ent is not None:
                    self._seen[name] = self._mmq[ent[0] >> 3]
                return v
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {name!r}")
            time.sleep(delay)
            delay = min(delay * 2, 0.002)

    def wait_change(self, name: str, timeout: float | None = None) -> Any:
        """Block until `name` is written (or first published) after this
        call starts, then return the fresh value. A consumer loop is just:

            while True:
                frame = g.wait_change("frame")

        Detection is the slot's seqlock counter: every publish advances it.
        The reference point is the counter of the value this handle last
        RETURNED, not the counter at call entry, so a publish that lands
        while the caller is still processing the previous value is delivered
        by the next call instead of being swallowed. Rarely (when the
        namespace compacts, or any variable is deleted, between polls) a
        wakeup may deliver an unchanged value.
        """
        mmq = self._mmq
        seen = self._seen
        deadline = None if timeout is None else time.monotonic() + timeout
        ref_gen = None                  # None -> any publish wakes us
        ent = self._cache.get(name)
        if ent is None or ent[1] != mmq[_OFF_GEN_W]:
            if self._peek(name) is not _MISSING:
                ent = self._cache.get(name)
            else:
                ent = None
        if ent is not None:
            ref_gen, ref_so = ent[1], ent[0]
            ref_seq = seen.get(name)
            if ref_seq is None:         # nothing delivered yet: wait for the
                ref_seq = mmq[ref_so >> 3]      # next publish, as documented
        delay = 0.00005
        while True:
            if ref_gen is None:
                v = self._peek(name)
                if v is not _MISSING:
                    ent = self._cache.get(name)
                    if ent is not None:
                        seen[name] = mmq[ent[0] >> 3]
                    return v
            elif mmq[_OFF_GEN_W] != ref_gen \
                    or mmq[ref_so >> 3] != ref_seq:
                v = self._peek(name)
                if v is not _MISSING:
                    ent = self._cache.get(name)
                    if ent is not None:
                        seen[name] = mmq[ent[0] >> 3]
                    return v
                ref_gen = None          # deleted: wait for a republish
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {name!r} to change")
            time.sleep(delay)
            delay = min(delay * 2, 0.002)

    def clear(self) -> None:
        """Drop every variable in the namespace (any process may call)."""
        mm = self._mm
        with self._wlock, self._sync:
            base, sc = self._slots_off, self._slot_count
            settled = False
            try:
                # Force slots EMPTY only once the freeze has SETTLED — every
                # slot even. If a live writer is genuinely stuck,
                # _freeze_and_settle raises and `settled` stays False, so the
                # finally never force-publishes that writer's in-flight odd
                # slot (which would hand readers a torn value).
                self._freeze_and_settle()
                settled = True
                _bump_gen(mm)
                for i in range(sc):
                    so = base + i * _SLOT_SIZE
                    s = _U64.unpack_from(mm, so)[0]
                    sodd = s if s & 1 else s + 1
                    _SLOT.pack_into(mm, so, sodd, _S_EMPTY,
                                    0, 0, 0, 0, 0, 0, 0, 0)
                    _seq_publish(mm, so, sodd + 1)
            finally:
                # Recount LIVE/TOMB from the actual table state — correct
                # whether we emptied everything, aborted mid-loop, or settling
                # itself raised (which may have force-tombstoned dead-writer
                # slots without updating the counters). Only force an odd slot
                # back to even when settling succeeded: then any odd slot is
                # one THIS loop just made odd before an async exception, never
                # a live writer's.
                nlive = ntomb = 0
                for i in range(sc):
                    so = base + i * _SLOT_SIZE
                    s = _U64.unpack_from(mm, so)[0]
                    if settled and (s & 1):
                        _seq_publish(mm, so, s + 1)
                    st = mm[so + 8]
                    if st == _S_OCCUPIED:
                        nlive += 1
                    elif st == _S_TOMB:
                        ntomb += 1
                _U32.pack_into(mm, _OFF_LIVE, nlive)
                _U32.pack_into(mm, _OFF_TOMB, ntomb)
                if not nlive:
                    _U64.pack_into(mm, _OFF_NEXT_FREE, self._values_off)
                # Always lift the freeze, even if settling failed, so a stuck
                # writer can never wedge the namespace forever.
                _U64.pack_into(mm, _OFF_FREEZE, 0)
                _REG_ENTRY.pack_into(mm, _OFF_FREEZER, 0, 0)
        self._cache.clear()

    def __repr__(self) -> str:
        return f"Globals(namespace={self._ns!r}, vars={len(self)})"


EasyGlobals = Globals          # compat alias (kept out of __all__ on purpose)

# Attribute names that would shadow the API; usable via g[name] only.
_RESERVED = frozenset(n for n in dir(Globals) if not n.startswith("_"))


def _close_all() -> None:
    """Interpreter exit: close whatever handles are still alive. ONE hook for
    the whole module — an atexit callback bound to each instance would keep
    every handle alive forever (and its fds with it)."""
    for g in list(_INSTANCES):
        try:
            g.close()
        except Exception:
            pass


def _after_fork() -> None:
    global _FORK_WARNED_PID
    _refresh_identity()
    if _FORK_WARNED_PID != _MY_PID:
        _FORK_WARNED_PID = _MY_PID
        warnings.warn(
            "EasyGlobals does not support the 'fork' start method: a fork "
            "that happens while any process holds the structural mutex "
            "leaves the child blocked on it forever, and the child shares "
            "the parent's file descriptors. Use spawn "
            "(multiprocessing.get_context('spawn')) instead.",
            RuntimeWarning, stacklevel=2)
    for ns in list(_NS_LOCKS):
        _NS_LOCKS[ns] = threading.Lock()   # parent thread may hold the old one
    for g in list(_INSTANCES):
        try:
            g._cache.clear()
            g._seen.clear()
            object.__setattr__(g, "_wlock", _NS_LOCKS[g._ns])
            object.__setattr__(g, "_slock", threading.RLock())
            # Recreate the kernel mutex: the flock fallback's lock lives on
            # the open file description, which fork SHARES — parent and
            # child would otherwise pass through each other's exclusion.
            # (Harmless re-bind for the in-shm pthread mutex on Linux.)
            old = g._lock
            object.__setattr__(g, "_lock", _Mutex(g._ns, g._mm, _OFF_LOCK))
            try:
                old.close()
            except Exception:
                pass
            with g._lock:
                if _U32.unpack_from(g._mm, _OFF_STATE)[0] == 2:
                    # The parent's last handle closed (mark-dead, unlink) in
                    # the fork -> re-register window: this mapping is a dead
                    # orphan. Registering into it would split-brain the child
                    # against every future attacher of the same name.
                    raise RuntimeError("segment died across fork")
                # Prune WITHOUT wiping, then register: the child inherits the
                # parent's variables and is itself a live attacher, so it must
                # never hit the "no attacher left alive" case and wipe them (a
                # daemonizing parent exits immediately after the fork).
                g._prune_and_maybe_wipe(False)
                g._register_self()
        except Exception:
            # Never keep using a handle that could not re-register (dead
            # segment, registry full, mutex rebuild failure): a live but
            # unregistered process would later be judged gone — its data
            # wiped and the segment unlinked under its own feet. Closing
            # turns that silent corruption into clean closed-handle errors.
            try:
                g.close()
            except Exception:
                pass


_FORK_WARNED_PID = None        # warn once per process, not once per fork chain

atexit.register(_close_all)

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
