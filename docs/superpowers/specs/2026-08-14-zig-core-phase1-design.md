# Zig core, phase 1 — lock-free memory engine + WAV writer

**Date:** 2026-08-14
**Status:** implemented (PRs #18, #19, #22, #24, #25, #27, #29)
**Arc:** first step of the native-core arc: Zig engine under the existing
Python/Qt app → CLAP plugin → mobile shells. This spec covers phase 1 only.

## Why

Three roadmap goals converge on one artifact — a native engine:

- **Portability.** PySide6 has no mobile story; the deferred VST arc already
  concluded a plugin needs native code. Both want the same core library.
- **Real-time correctness.** Today's `AudioCircularBuffer` takes a
  `threading.Lock` inside the audio callback — the callback can block on a
  UI reader holding the lock (the dropped-callback counter exists because
  this happens). The GIL adds UI-driven jitter on top.
- **Leanness.** The Python runtime + Qt + numpy cost ~150 MB and seconds of
  startup around a buffer that is itself the only irreducible allocation.

Secondary goal, explicit owner request: **learn Zig**. The code is written
idiomatically ("Zig-y, not Python-port-y") with instructional comments where
a Zig concept first load-bears, and each PR description carries a
"Zig concepts in this PR" section.

## Owner decisions (locked)

| Decision | Choice |
|---|---|
| Where the Zig code lives | In-repo `core/` subproject (monorepo: one tracker, one CI, parity harness free) |
| Phase-1 scope | Memory engine + hand-rolled WAV writer. **Zero external dependencies.** Capture stays Python/sounddevice (phase 2 = miniaudio). |
| FLAC | Deferred. WAV FLOAT32 is already the bit-perfect "purest pull" (file bytes == RAM bytes); FLAC stays on the Python/soundfile path for now. |
| Git flow | PRs straight to `main`; the app keeps working at every merge. Epic + sub-issues, portfolio convention. |
| Concurrency model | **Option B: seqlock ring** — lock-free writer, retry readers (see below). Rejected: A (mutex port — carries the RT flaw into Zig), C (full command-queue RT engine — YAGNI for phase 1; its one cheap idea, gain as an atomic, is folded in). |
| Shipped optimize mode | **ReleaseSafe** — bounds checks stay on; that is the memory-safety story. Debug for tests. |

## Layout

```
core/
  build.zig, build.zig.zon      # pinned minimum Zig 0.16.x
  include/flashback_core.h      # hand-written C header (future non-ctypes hosts)
  src/
    root.zig                    # library root: pub exports
    Ring.zig                    # seqlock ring (file-as-struct)
    Summary.zig                 # min/max/rms slot ring (file-as-struct)
    wav.zig                     # WAV encoder over any writer
    abi.zig                     # C ABI shim — thin, boring
```

Artifacts: shared library (`flashback_core.dll` / `.so` / `.dylib`) for the
ctypes host (`core/build.zig`'s only `addLibrary` call, `linkage = .dynamic`
— no static archive is built). Tests are colocated `test` blocks.

## Idiomatic commitments

- **File-as-struct** for `Ring.zig` / `Summary.zig` (top-level fields,
  `init` / `deinit`).
- **Caller-supplied allocator.** `Ring.init(allocator, .{ .sample_rate,
  .channels, .seconds })` allocates once — frame store + summary slots,
  contiguous, up front. Zero allocation ever again on write or read paths;
  `deinit` returns everything. No hidden global allocator: the ABI shim owns
  one allocator instance and passes it in.
- **Error sets, not sentinels**, internally: `error.Overwritten`,
  `error.OutOfRange`. Translated to integer status codes only at the ABI
  boundary (no error unions across a C ABI).
- **Composition:** `Ring` *has a* `Summary`; the write path feeds both, then
  publishes. `Summary` knows slot math, nothing about capture or files.
- Runtime config stays runtime (channels, rate — no speculative comptime).
  `comptime` earns its keep in WAV subtype dispatch and, later, SIMD.

## The seqlock ring

Single producer (the audio callback), any number of readers (UI waveform,
checkout render). One generation mechanism covers raw reads, summary
freshness, and stale detection.

**Writer** (RT-safe: no locks, no allocation, no failure path):
1. Copy incoming interleaved f32 frames into the ring — at most 2 memcpy
   spans on wraparound — applying gain during the copy. Gain is a
   `std.atomic.Value(f32)`; control changes need no lock and never block
   the writer.
2. Update the summary slots the span touched (incremental min/max/
   sum-of-squares/count, same scheme as the Python `_sum_*` arrays; a
   slot's absolute first-sample index is its generation tag).
3. Publish with a single release-store of `total_written`
   (`std.atomic.Value(u64)`).

**Reader** (seqlock dance):
1. `t1 = total_written.load(.acquire)`. If the requested span is older than
   `t1 - capacity` → `error.Overwritten` immediately.
2. Copy the span out.
3. `t2 = total_written.load(.acquire)`. If the writer lapped into the span
   mid-copy (`t2` invalidates `abs_start`) → retry. Bounded retries, then
   `error.Overwritten`. (A reader loses only if the writer wraps the entire
   buffer through the span mid-copy — at 15 min of buffer, effectively
   never; the retry is correctness insurance, not a hot path.)

Addressing is **absolute sample indices** (`total_written` coordinates)
everywhere — the Python code's `abs` convention, so the swap is mechanical.

**Stress test is a first-class deliverable:** writer thread at audio-rate
pacing vs. a hammering reader thread; every successful read's span is
checksum-verified for consistency (no torn reads); overwritten reads must
report as such, never return silently corrupt data.

## WAV writer (`wav.zig`)

- The API is three small functions, not one generic writer: `writeHeader`
  (fills a 44-byte RIFF/WAVE header buffer), `encodeSamples` (converts a
  slice of `f32` samples to the requested subtype's on-disk bytes), and
  `writeFile` (the path-based convenience — header + encode + write to
  disk — that the ABI's `fb_wav_write` thinly wraps). Golden-byte tests
  exercise `writeHeader`/`encodeSamples` directly against in-memory
  buffers, no `anytype` writer needed.
- Subtypes: `FLOAT32` (default — bit-perfect, file payload == RAM),
  `PCM_24`, `PCM_16` (explicit quantized options, matching today's
  `_VALID_SUBTYPES`). Dithered quantization is future flair, not phase 1.
- Plain RIFF/WAVE: `fmt ` (WAVE_FORMAT_IEEE_FLOAT or PCM) + `data`. No
  extra chunks.

## C ABI (`abi.zig` + `include/flashback_core.h`)

Small, flat, out-params, no allocation across the boundary:

```c
FbRing*      fb_ring_create(uint32_t rate, uint16_t channels, double seconds); // NULL on OOM
void         fb_ring_destroy(FbRing*);
void         fb_ring_write(FbRing*, const float* frames, size_t n_frames); // RT-safe
uint64_t     fb_ring_total_written(const FbRing*);
uint64_t     fb_ring_capacity(const FbRing*);        // in frames -- the READABLE window
uint64_t     fb_ring_storage_frames(const FbRing*);  // PHYSICAL frame count backing fb_ring_storage
                                                      // (capacity + guard band -- the whole two-sizes design)
const float* fb_ring_storage(const FbRing*);         // zero-copy view, shaped with storage_frames
void         fb_ring_set_gain(FbRing*, float);
float        fb_ring_gain(const FbRing*);
void         fb_ring_flush(FbRing*);
FbStatus     fb_ring_read(FbRing*, uint64_t abs_start, size_t n_frames, float* out);
FbStatus     fb_ring_summary_bins(FbRing*, size_t n_bins, uint64_t n_samples,
                                  uint64_t bin_span_frames, float* out_rms);
FbStatus     fb_wav_write(const char* path, const float* frames, size_t n_frames,
                          uint32_t rate, uint16_t channels, FbSubtype subtype);
```

`FbStatus`: `OK`, `OVERWRITTEN`, `OUT_OF_RANGE`, `IO_ERROR`, `INVALID_ARG`.
`FbSubtype`: `FLOAT32`, `PCM_24`, `PCM_16`.

**Refinements over the first draft** (from the code recon; each is the
smaller, truer surface):

- **Zero-copy storage view instead of a min/max summary query.** Python's
  `get_peak_bins` deliberately reads ring storage in place via numpy views
  (30 Hz polling on a ~345 MB ring — copying would saturate memory
  bandwidth) with its own seqlock verify. The native path keeps that
  algorithm in Python over `fb_ring_storage` + `fb_ring_total_written`
  (numpy view via `np.ctypeslib.as_array`), extracted into one shared
  function used by both implementations. It is visualization code that the
  eventual UI rewrite replaces anyway — porting it now is waste.
- **Summary query returns RMS bins only** (`fb_ring_summary_bins`, mirror
  of `get_summary_bins` semantics: `n_samples = 0` → all available,
  `bin_span_frames = 0` → window/n_bins), because that is the only summary
  consumer. YAGNI on per-bin min/max.
- **`total_written` is the single source of truth.** There is no stored
  `write_pos`; the writer derives its physical ring position as
  `total_written % storage_frames` — **not** `% capacity`. The ring
  allocates `storage_frames = capacity + max_write_frames` frames (a guard
  band sized to the largest single write), so an accepted reader's span and
  the writer's in-flight, not-yet-published block are always provably
  disjoint in physical storage. `capacity` — the smaller, readable window —
  stays what every clamp (`get_latest`, `is_full`, the overwritten check)
  is checked against; only the modulo for physical indexing uses the
  larger size. Readers never address beyond `total_written`, so stale
  bytes past it are unreachable — which makes…
- **…`fb_ring_flush` one release-store of `total_written = 0`** plus
  poisoning every summary slot generation (`slot_abs = -1`) plus a hygiene
  zeroing of storage (off the audio thread). Racing an active writer is
  NOT bounded to "one block of silence": a writer that already loaded
  `tw` before the flush will still publish `tw + n` afterward, silently
  UNDOING the reset — `total_written` lands back near its pre-flush value
  even though every readable frame is now zero, with no observable
  indication a flush happened at all. Up to a FULL CAPACITY of silence,
  not one block, can result. One summary slot may also transiently mix
  epochs (~85 ms, self-heals at the slot's next generation). This is a
  known race in the flush-vs-writer relationship, documented in
  `Ring.zig`'s `flush()` doc comment, tracked as a separate design
  question for the arc — not fixed here. See issue #20.
- **A single factory swaps the app**: five call sites construct buffers
  today (`app/state.py`, `core/capture.py`, `core/capture_slot.py`,
  `core/loopback_capture.py`, `core/mixed_capture.py`); the swap PR routes
  them through one `make_ring_buffer(...)` that returns the native
  implementation when the library loads, else the Python one.

## Python integration & parity harness

- New `flashback_sampler/core/native.py`: ctypes loader (package dir first,
  then `core/zig-out/lib` for dev) + `NativeAudioCircularBuffer` presenting
  the same interface as today's `AudioCircularBuffer`.
- **The existing buffer test suite becomes the parity harness**: a
  parametrized fixture runs every buffer test against both implementations.
  Mutation-checked per house rules (break the Zig impl, confirm red) —
  one mutation per clause on compound conditions.
- **WAV parity is decode-equality, not byte-equality**: write with both
  paths, read both back with soundfile, assert bit-identical samples +
  format. (libsndfile adds chunks — e.g. PEAK on float files — we don't
  replicate.)
- **Swap PR** (last in phase 1): the app constructs the native buffer when
  the library loads; the Python implementation stays as a fallback until
  capture moves in phase 2, then gets deleted. WAV checkout/drag-export
  routes through `fb_wav_write`; FLAC keeps routing through soundfile,
  untouched.

## CI & release

- New `zig` job in `test.yml` (same triggers/concurrency as pytest), matrix
  ubuntu/windows/macos: install pinned Zig version, `zig fmt --check`,
  `zig build test` (Debug — full safety checks), and cross-compile
  ReleaseSafe artifacts for all desktop targets from one runner as a
  build-health check.
- The existing Windows pytest job gains a build step so parity tests run
  against the real DLL. Parity tests are skip-guarded on the library's
  presence, so Zig-less local dev still works.
- `release.yml` builds the DLL (ReleaseSafe) before PyInstaller;
  `flashback_sampler.spec` bundles it.
- Zig version: pin exact in CI + `minimum_zig_version` in `build.zig.zon`.
  Pre-1.0 churn is expected; version bumps are deliberate PRs.

## Issue tracking

Epic + sub-issues on `Phlares/flashback-sampler`, portfolio convention
(write-at-the-moment, close by hand only if a PR's `Closes #NN` doesn't
fire — PRs target `main`, the default branch, so it should). Roughly one
sub-issue per PR:

1. Scaffold `core/` + CI (build.zig, empty lib, zig job green, DLL builds).
2. `Ring.zig` — seqlock ring + stress test.
3. `Summary.zig` — summary slots + generation-checked queries.
4. `wav.zig` — encoder + golden-byte tests.
5. `abi.zig` + `native.py` + parity harness.
6. Swap PR — app uses native buffer + native WAV; release bundles the DLL.

## Testing summary

- `zig test` colocated: ring math, wraparound, seqlock retry, summary slot
  freeze/generation, WAV golden bytes, allocator hygiene (Debug allocator
  catches leaks/UAF in tests by construction).
- Threaded stress test (torn-read checksum) — deliverable of PR 2.
- pytest parity suite over both buffer implementations; WAV decode-equality.
- TDD throughout; every test mutation-checked before trusted.

## Out of scope (phase 1)

Capture/miniaudio (phase 2), per-process loopback port, FLAC in Zig,
resampling, CLAP plugin, any UI work, deleting the Python buffer
implementation (kept as fallback until phase 2).
