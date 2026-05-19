Looking at your Scalene profile, here are the bottlenecks I can identify:

## Key Observations

**1. I/O is the dominant cost (~0–40ms block)**
The timeline shows a solid blue I/O bar consuming roughly the first 40ms of your 170ms total run. This suggests the file read itself is a significant chunk — likely `__fread` / `_srefill` / `_sread` / `_aread` in the call stack. If you're doing unbuffered or small-chunk reads, that's low-hanging fruit.

**2. GIL contention is visible**
The GIL row in the timeline shows activity, which is notable for a Rust binding. This usually means your Rust extension is calling back into Python too frequently (e.g. building Python objects incrementally rather than bulk-returning), or you're not releasing the GIL during the heavy native work with `py.allow_threads(|| ...)`.

**3. `_Py_HandlePending` / `PyErr_CheckSignalsState` in the hot path**
These appear mid-stack and indicate Python's signal-checking mechanism is being hit repeatedly. This happens when Rust calls back into the Python C API in a tight loop — each re-entry triggers signal checks. Batching your Python object construction would eliminate most of these.

**4. Thread/semaphore wait (`_PySemaphore_Wait`, `pthread_cond_wait`)**
The right-hand call stack shows a thread sitting in `_PySemaphore_Wait` → `pthread_cond_wait`. This is a waiting thread, likely a GIL waiter or a thread pool worker stalled. If you're spawning worker threads in Rust, they may be contending more than helping for a single-file parse.

**5. `infinite_lru_cache_wrapper`**
This appears in the Python call stack. If you have a Python-side LRU cache wrapping something in the hot path, that's adding overhead — especially if it's being hit per-page or per-token.

---

## Suggested Fixes

- **Release the GIL** during your Rust parsing work: wrap the core parse loop in `py.allow_threads(|| { ... })` in PyO3. This is the single highest-impact change if you aren't already doing it.
- **Batch Python object construction** — parse everything in Rust, return a single large structure (Vec of structs), then convert to Python objects once at the end rather than per-element.
- **Buffered reads** — if you're opening the file in Rust, make sure you're using `BufReader` rather than raw file reads, which maps to the `_srefill`/`_sread` pattern you're seeing.
- **Profile the LRU cache** — check what `infinite_lru_cache_wrapper` is wrapping on the Python side and whether it can be moved into Rust or eliminated.
- **Avoid thread spawning per call** — if Rayon or a thread pool is being initialized per `parse()` invocation, use a lazy static pool instead.

The biggest win is almost certainly the GIL release + batched return pattern. pymupdf and pdftotext win here largely because their Python bindings do exactly that — all work happens natively, Python only touches the final result.
