# Introduction
EasyGlobals is an easy way to share variables between Python processes.

Make a Globals object called `g` and every variable you put on it is visible to all your other processes — full Python objects, with normal Python syntax. No Manager dicts, queues or other data pipelines, and since 0.2.0 no server either: values live in shared memory and reads are lock-free, so it runs at shared-memory speed on Windows and Linux with zero dependencies.

```python
from EasyGlobals import Globals

g = Globals()
g.test1 = 4
g.test2 = 'hello world'
g.test3 = {'dictkey1': g.test1, 'dictkey2': g.test2}

print(g.test1, g.test2, g.test3)
```

# Installation
```
pip install easyglobals
```
That's it. Memcached is no longer used or required. (Optional: `pip install easyglobals[numpy]` if you want the numpy fast path.)

# How it works
- One process writes a variable, every process can read it. The first process to write a name becomes its owner; only the owner may overwrite or delete it. This single-writer rule is what makes lock-free reading safe (and it turns the classic "two writers race each other" headache into a clear `OwnershipError` instead of silent data corruption). Call `g.disown('name')` to hand a variable over to another process, e.g. before the owner exits.
- Variables live as long as your program. When the last attached process exits, everything is discarded — no stale globals leak into the next run (this replaces the old "restart memcached to clear leftovers" chore). If a run is killed, the next run detects it and starts clean.
- Reads return copies. Mutating a nested field on a returned object doesn't change the shared value: retrieve the object, modify it, then assign it back (see `examples/example_objects.py`).
- Anything picklable works: classes, dicts, OpenCV images, numpy arrays. int, float, bool, str, bytes and numpy arrays skip pickle entirely for speed.

# Multiprocessing example
```python
from EasyGlobals import EasyGlobals
import multiprocessing

def write_to_globals():
    g = EasyGlobals.Globals()
    for i in range(1_000):
        g.testvar = i

def retrieve_from_globals(process_id):
    g = EasyGlobals.Globals()
    for i in range(10):
        result = g.wait_for('testvar', timeout=10)
        print(f'Process {process_id}, read: {result}')

if __name__ == '__main__':
    g = EasyGlobals.Globals()  # keep one handle open in the parent (see note)

    print('Start writing process')
    write_process = multiprocessing.Process(target=write_to_globals)
    write_process.start()

    print('Start reading with 5 simultaneous processes')
    processlist = []
    for i in range(5):
        processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
        processlist[i].start()

    for process in processlist:
        process.join()
    print('Done reading')
    write_process.join()
    print('Done writing')
```
Note: create a `Globals()` in your main process before spawning workers (or pass `g` straight into `Process(args=...)` — it reattaches in the child). Variables only live while at least one attached process is alive, so the parent handle keeps them available even if a writer finishes early.

# API
| | |
|---|---|
| `g.x = v` / `g.x` / `del g.x` | attribute style |
| `g['any name']`, `g.get('x', default)`, `'x' in g`, `len(g)`, `g.keys()`, `g.items()`, `g.values()`, iteration | dict style |
| `g.wait_for('x', timeout=None)` | block until `x` exists, return it |
| `g.wait_change('x', timeout=None)` | block until `x` is written again — the natural consumer loop for frames |
| `g.owner_of('x')` | pid of the owning process |
| `g.disown('x')` | give up ownership so another process may write it |
| `g.clear()` | drop every variable in the namespace |
| `Globals('mynamespace', capacity=..., slot_count=...)` | isolated namespace with its own sizing |

Reading a variable that doesn't exist raises `AttributeError`/`KeyError` (use `g.get('x', default)` for a soft read). Writing a variable owned by another live process raises `OwnershipError`.

# Speed 🚀
Measured on Windows 11 (Ryzen, 16 cores), Python 3.13, best of 5 (`python tests/benchmark.py`):

| workload | throughput |
|---|---|
| int writes, 1 process | ~1.4M op/s |
| int reads, 1 process | ~1.7M op/s |
| 1KB string writes | ~950k op/s |
| dict writes (pickle path) | ~520k op/s |
| 4 processes writing own keys | ~3.7M op/s combined |
| 4 processes reading one hot key | ~5.6M op/s combined |
| 24MB frame writes (bytes or numpy) | ~25 GB/s |
| 24MB frame reads | ~10 GB/s (memory-bandwidth-bound) |

That is one to two orders of magnitude above the 0.1.x memcached engine (every access there was a server round-trip), and readers scale with process count instead of collapsing onto a server socket. Local variables in-process are of course still faster than any sharing mechanism.

# Migrating from 0.1.x (memcached versions)
- No memcached needed anymore; `pip install` is the whole setup.
- Reading a missing variable now raises instead of returning `None`, and failed writes raise instead of being silently logged. Use `g.get('x')` if you want the old soft behavior.
- Only one process may write a given variable (the first writer). Multi-writer patterns need `disown()` or per-process variable names. In return, writes can no longer silently overwrite each other.
- Variables no longer persist after your program ends, and separate simultaneous programs only share if they use the same namespace name. Use `Globals('mynamespace')` to be explicit about sharing.
- `reset_all_globals()` is now `clear()`.
- Method names (`get`, `keys`, `items`, `values`, `clear`, `close`, `disown`, `owner_of`, `wait_for`, `wait_change`) can't be used as attribute names — use `g['get'] = ...` for those. Names starting with `_` are not shared (they're plain attributes on the handle).
- `bytearray`/`memoryview` values come back as `bytes`; Fortran-ordered numpy arrays come back C-ordered (values identical).

# Limitations
- Key count per namespace is fixed at creation (default fits ~5.7k variables; raise with `slot_count=`). Value space (default 256 MiB) recycles itself automatically; `MemoryError` if live data truly exceeds it. On Windows the full capacity counts toward system commit per namespace, so lower `capacity=` if you create many namespaces. On Linux it's demand-paged (free until touched).
- Up to 256 concurrently attached processes per namespace.
- Windows and Linux are first-class; macOS works best-effort via a slower fallback lock.
- Python 3.8+.
