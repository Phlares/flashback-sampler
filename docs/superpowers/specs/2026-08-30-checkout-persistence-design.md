# Checkout persistence — scratch to disk, RAM as cache, slices as references, drag-out with handles

Date: 2026-08-30. Status: approved in brainstorm, spec under review.
Tracker: epic #53 (PRs g, h, i). Parent spec:
`2026-08-30-zig-core-phase2-d-f-design.md` (its standing rules apply).
Where this document differs from the parent, this document wins.

## Goal

A checkout survives an app crash and does not exhaust RAM. After PR i:

- Every checkout is written to a scratch WAV on creation, bit-exact at
  the capture rate, by a Zig writer thread that never blocks the UI or
  the audio threads.
- RAM holds one Zig-owned copy per checkout at most, kept under a
  global byte budget with LRU eviction. Python never holds audio.
- A slice is a reference `(parent file, start_frame, n_frames)`; it
  is materialised only on drag-out or save.
- A drag-out exports the parent span around the slice (up to a size
  cap) with WAV markers at the slice, so the DAW user can pull more of
  the clip than they sliced.
- The scratch dir is adopted whole at launch: files from a crash or a
  previous session come back as checkouts.

## Standing rules

All rules of the parent spec hold: Python is a shell, idiomatic
allocation-free Zig on hot paths, portability through `Backend.zig`,
Zig test count must rise per PR, every new file re-exported in
`root.zig`, instructional comments are contract. New in this epic:

- **No audio in numpy.** `Checkout.audio` (the numpy array) is deleted
  in PR h. Python holds a `*Checkout` handle and reads numbers.
- **One copy.** The checkout's frames exist in Zig once (taken from the
  ring at create). The writer streams from that copy; Playback reads
  from it or from the file, never from a second RAM copy of the clip.
- **Zero heap on the write path.** `wav.writeFile` keeps its 64 KiB
  stack buffer; the reader uses the same size; the writer thread
  allocates nothing after `start`.
- **One bin-edge reducer.** `Ring.peakBins`, the flat-buffer reducer,
  and the streaming file reducer share `peaks.zig`. `_peak_bins_from_audio`
  (numpy) dies with the array.
- **The scratch file is the checkout.** Every checkout is
  `(file, start_frame, n_frames)`. A root owns its file at `(0, all)`;
  a slice references its parent's file. A file lives while any
  checkout references it.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Writer | A Zig writer thread (`Scratch.zig`), one per process, intrusive FIFO of `*Checkout` jobs, `std.Io.Condition` wake, zero CPU idle. | Owner ruling 2026-08-30: no Python thread, no doubled buffer, lowest resource cost even at more surface. |
| RAM copy | Taken from the ring at create by `Ring.read` into a Zig allocation. The writer streams from it. | The ring can lap before a lazy write lands; the copy is the guarantee. Same copy Python made today, now Zig-owned. Direct ring→disk was rejected for the lap risk. |
| Cache | Global byte budget, LRU, in `Scratch.zig`. Pinned: the selected checkout (Python says which) and any checkout whose write is not `written`. Budget 0 = drop after write. | Counts are meaningless across rates (5 s at 48 kHz vs 15 min at 192 kHz). One cache across slots protects total RAM. |
| Cache budget default | Set by the PR h measurement (select→playable time of the largest clip at 192 kHz from the scratch disk), recorded on #53. | Ruling 3: numbers from measurement. |
| Preload on select | Selecting a clip pins it and enqueues an async `load` job on the writer thread (same queue, second job kind). `bind` from file remains only as the fallback when PLAY lands before the preload finished. | A from-file read inside `bind` runs on the UI thread; the preload keeps that read off the UI thread in the normal path. |
| Scratch dir | `platformdirs.user_cache_dir("flashback-sampler", appauthor=False)/scratch`; Preferences override (`scratch_dir`). | App-owned temp, separate from the user-facing exports pool. A second SSD is one click away. |
| Adoption | At launch every manifest in the scratch dir is adopted: into a slot with matching rate and channels, else into a new unarmed slot named from the manifest. A `.part` file is adopted with the frames it holds, flagged `partial`. | The crash case is the point. Quit and crash take the same path: one mechanism. |
| Scratch lifetime | Files survive quit and crash. Discard deletes the manifest at once and the WAV when its refcount reaches zero. Slot removal discards the slot's checkouts. | "The app cleans it" (ruling 1) means discard, not quit. |
| Peak bins at rest | 540 + 360 bins live in the per-checkout JSON manifest, written at create. Not a WAV chunk. | Adoption must draw the deck without reading gigabytes. The manifest exists anyway (identity, provenance, trim). |
| Playback source | `Playback.bind(ClipSource)` with `union(enum){ frames, file_range }`. From RAM it dupes as today; from a file it reads straight into the clip buffer. | One bind, one copy either way. |
| Export | `wav.copyRange(src, dst, start, n, subtype, markers)` file→file streaming. Save, drag and slice export all go through it once the scratch is `written`; before that, `wav.writeFile` from the RAM copy. | Reading the scratch file needs no reload of an evicted clip. |
| Slices | Trim stays the gesture. Dragging or saving a trimmed range mints a slice checkout in `saved` state (ruling 5) before the export. | The user can come back for more of it, in the app and in the DAW. |
| Handles | Export span = the slice expanded symmetrically inside the parent up to `drag_max_mb` (default 200 MB). Markers (`cue ` + `smpl`) mark the slice. | One formula: cap ∞ = whole parent, cap = slice size = slice only. Owner ruling: "whole parent up to size cap; the markers recover the audio". |
| DAW clip bounds | Markers are portable and harmless; no DAW documented turns them into clip start/end. PR i Task 0 spikes an Ableton `.alc` sidecar on the box; a preference ships only if the spike passes. | Only the box can answer it (arc lesson: measure, never assume). Other DAWs get a documented table, every row marked untested. |
| Count cap | `max_active_checkouts = 16` per slot stays. `max_total_ram_mb` per slot and the window's `_evict_oldest_saved_checkout` are deleted. | The deck draws one ring per checkout; the byte cache replaces the MB cap. |
| PR split | g `feat/zig-wav-read` (engine only), h `feat/zig-scratch` (the model change), i `feat/slices-handles`. Three PRs → `dev`. | g is testable without the app; h is the big seam; i needs the spike. |

## Architecture after PR i

```
Python (Qt shell, ctypes only)              Zig (flashback_core)
------------------------------              --------------------------------------------------
CheckoutManager ── fb_checkout_* ────────►  Checkout (frames?, file, start, n, write_state)
   manifests (<id>.json), refcounts              │ peakBins via peaks.zig (RAM or file)
   adoption at launch                            │ source() → ClipSource
AppState ── fb_scratch_* ────────────────►  Scratch: writer thread + FIFO + LRU byte cache
NativeScrubPlayer ── fb_playback_bind_checkout ► Playback.bind(ClipSource)
export / drag ── fb_checkout_export ─────►  wav.copyRange (+ cue/smpl markers)
tests / adoption ── fb_wav_info, fb_wav_read ► wav.zig read side
```

## Data model

```zig
// Checkout.zig (file-as-struct, like Ring.zig)
pub const WriteState = enum(u8) { queued, writing, written, failed, adopted };
const Checkout = @This();
// fields:
    allocator: std.mem.Allocator,
    frames: ?[]f32,            // the one RAM copy; null when evicted
    path_buf: [max_path]u8, path_len: usize,   // the file this checkout reads
    start_frame: u64,          // offset into the file (0 for a root)
    n_frames: u64,
    rate: u32,
    channels: u16,
    write_state: std.atomic.Value(WriteState),
    job: enum(u8) { none, write, load },   // what the queue link means while queued
    pinned: std.atomic.Value(bool),
    // Scratch bookkeeping: intrusive links, last-use tick.
    queue_next: ?*Checkout, lru_prev: ?*Checkout, lru_next: ?*Checkout,
    last_use: u64,
```

- A **root** is created from a ring span: `frames` allocated, `Ring.read`
  copies the span (3 attempts, `Overwritten`/`OutOfRange` map to the
  same `FbStatus` values `fb_ring_read` uses), `write_state = queued`,
  job enqueued. Its file is `<scratch>/<id>.wav`.
- A **slice** is `(parent's path, s, n)` with `frames = null`,
  `write_state = adopted` (nothing to write). It never owns a file.
- An **adopted** checkout comes from a manifest: `frames = null`,
  `write_state = adopted`; `partial` is manifest metadata, and
  `n_frames` is what the file holds.
- Python keeps the refcount per file path in `CheckoutManager` (one
  dict); the Zig side never deletes files. Python deletes the WAV when
  the count reaches zero and the manifest on discard.

Manifest `<id>.json`:

```json
{"id": "...", "slot": "Main", "rate": 192000, "channels": 2,
 "abs_start": 123456, "abs_end": 234567, "created_at": 1756600000.0,
 "parent": null, "start_frame": 0, "n_frames": 111111,
 "trim_in": 0, "trim_out": 0, "state": "pending", "partial": false,
 "bins": {"ring_amp": [...540*2*ch floats...], "panel_bins": [...360*2*ch...]}}
```

`parent` is the parent id for a slice; `start_frame`/`n_frames` are
relative to the parent's file. The manifest is written at create,
before the audio, and rewritten on trim or state change (atomic
temp + replace, the `config.py` idiom).

## PR g — `wav.zig` read side, `peaks.zig`

### `wav.zig`

```zig
pub const Info = struct {
    rate: u32, channels: u16, subtype: Subtype,
    frames: u64,          // clamped to what the file holds (a .part reads its true prefix)
    data_offset: u64,     // byte offset of the first sample
};
pub const ReadError = error{ NotWave, MissingFmt, MissingData, Unsupported, Truncated } || std.Io.File.OpenError || std.Io.File.ReadPositionalError;
pub fn open(path: []const u8) ReadError!struct { file: std.Io.File, info: Info };
pub fn readFrames(file: std.Io.File, info: Info, start_frame: u64, out: []f32) ReadError!void;
pub fn copyRange(src: []const u8, dst: []const u8, start_frame: u64, n_frames: u64, st: Subtype, markers: ?Markers) !void;
```

- Chunk walk: `RIFF`/`WAVE`, then `fmt `, `data`, unknown chunks
  skipped, word alignment honoured. `fmt ` tag 1 (PCM) and 3 (FLOAT);
  tag 0xFFFE (EXTENSIBLE) takes the real tag from the SubFormat GUID's
  first two bytes (offset 24). Supported: FLOAT32, PCM16, PCM24. PCM32
  and anything else → `Unsupported`.
- `frames = min(data_len, file_len - data_offset) / block_align`: the
  clamp is what makes a crash-truncated `.part` readable.
- `readFrames` decodes to f32 with the `/2^(bits-1)` convention
  `tests/fixtures/wavread.py` pins (32767 reads as 32767/32768).
  Positional reads (`File.readPositionalAll(io, buf, offset)`) through
  a 64 KiB stack buffer, sample-aligned chunk boundaries as
  `writeFile`. FLOAT32 is a memcpy of the bits (little-endian comptime
  assert already in the file).
- `copyRange` streams src→dst through the same buffer and appends the
  marker chunks (PR i defines `Markers`; PR g ships `markers: null`).
  The RIFF size and `data` size are written up front from `n_frames`.
- `writeFile` is unchanged. Round-trip test: bytes written by
  `writeFile` read back sample-exact for every subtype.

### `peaks.zig`

```zig
pub fn binEdge(step: f64, i: usize, n: u64, n_bins: usize) u64;   // moved out of Ring.zig, same body
pub const Accumulator = struct { first: bool, ... };
pub fn reduceFrame(frame: []const f32, out_bin: []PeakBin, first: *bool) void;   // moved from Ring.zig
pub fn peakBinsFlat(frames: []const f32, channels: u16, n_bins: usize, out: []PeakBin) void;
pub fn peakBinsFile(file: std.Io.File, info: Info, start_frame: u64, n_frames: u64, n_bins: usize, out: []PeakBin) !void;
```

- `Ring.peakBins` keeps its seqlock loop and stride logic and calls
  `peaks.binEdge` / `peaks.reduceFrame`. `PeakBin` moves to `peaks.zig`;
  `Ring.PeakBin` stays as an alias so `abi.zig` is untouched.
- `peakBinsFlat` is the numpy `_peak_bins_from_audio` semantics
  exactly (empty bins copy the previous bin; last edge = n).
- `peakBinsFile` walks the file in chunks and advances the bin index
  by `binEdge` as it goes; no stride (files are read once, at
  create-from-adoption or on a cache miss).

### ABI

```c
typedef struct FbWavInfo { uint32_t rate; uint16_t channels; uint8_t subtype; uint64_t frames; } FbWavInfo;
FbStatus fb_wav_info(const char *path, FbWavInfo *out);
FbStatus fb_wav_read(const char *path, uint64_t start_frame, size_t n_frames, float *out);
FbStatus fb_wav_peak_bins(const char *path, uint64_t start_frame, uint64_t n_frames, size_t n_bins, FbPeakBin *out);
```

`native.py` gains `wav_info(path)`, `wav_read(path, start, n)`,
`wav_peak_bins(path, start, n, n_bins)`. No production caller in PR g;
tests and PR h's adoption use them.

### Tests (PR g)

- Zig: header parse for plain and EXTENSIBLE fmt chunks built in-test
  (byte arrays), odd-sized skipped chunk, missing `data`, PCM32 →
  `Unsupported`, truncated data clamps `frames`, decode values for
  PCM16/24 rails, `writeFile` → `readFrames` sample-exact for the three
  subtypes, `copyRange` sub-span, `peakBinsFlat` parity with
  `Ring.peakBins` on the same frames (n = 30 / 22 bins, the case G
  edge), `peakBinsFile` parity with `peakBinsFlat`.
- Python: `wav_read` vs `wavread.py` on files written by `wav_write`
  (the two readers agree), EXTENSIBLE fixture built with `struct` in
  the test.
- Mutation pins: the EXTENSIBLE offset (24 → 22 reddens), the
  truncation clamp (remove → `Truncated` on a `.part`), one `binEdge`
  order mutation in `peakBinsFlat`.

## PR h — `Checkout.zig`, `Scratch.zig`, the cache, manifests, adoption

### `Checkout.zig`

```zig
pub fn createFromRing(alloc, ring: *Ring, abs_start: u64, abs_end: u64, path: []const u8) !*Checkout;
pub fn slice(parent: *const Checkout, start: u64, n: u64) !*Checkout;   // PR i uses it; shipped here for tests
pub fn adopt(alloc, path: []const u8, start: u64, n: u64, info: wav.Info) !*Checkout;
pub fn peakBins(self: *Checkout, n_bins: usize, out: []PeakBin) !void;   // RAM if resident, else file
pub fn source(self: *Checkout) ClipSource;
pub fn evict(self: *Checkout) void;      // frees frames; caller checks write_state == written/adopted and !pinned
pub fn load(self: *Checkout) !void;      // readFrames into a fresh allocation
pub fn destroy(self: *Checkout) void;
```

`createFromRing` reads the span in `Ring.max_write_frames` pieces
through `Ring.read` into the new allocation (so a torn read retries
the piece, not the clip). A failed read frees and returns the error.

### `Scratch.zig`

```zig
pub const Scratch = struct {
    io = std.Io.Threaded.global_single_threaded.io(),
    mutex: std.Io.Mutex = .init, cond: std.Io.Condition = .init,
    queue_head/tail: ?*Checkout, lru_head/tail: ?*Checkout,
    resident_bytes: u64, budget_bytes: u64, tick: u64,
    thread: ?std.Thread, stop_flag: std.atomic.Value(bool),
    pub fn start(self: *Scratch) !void;   // spawn; same control-thread ownership as Capture
    pub fn stop(self: *Scratch) void;     // stop_flag, signal, join; the loop drains the queue first
    pub fn submit(self: *Scratch, co: *Checkout, job: Job) void;   // lock, set co.job, append, signal
    pub fn preload(self: *Scratch, co: *Checkout) void;  // pin + submit(.load) when not resident
    pub fn touch(self: *Scratch, co: *Checkout) void;    // move to LRU head; evict tail while over budget
    pub fn setBudget(self: *Scratch, bytes: u64) void;
    pub fn pin(self: *Scratch, co: *Checkout, on: bool) void;
    pub fn forget(self: *Scratch, co: *Checkout) void;   // unlink from both lists (destroy path)
};
```

- Writer loop: lock; while queue empty and not stopping →
  `cond.waitUncancelable(io, &mutex)`; pop head; unlock; dispatch on
  `co.job`. `.write`: `write_state = writing`; `wav.writeFile(path +
  ".part", frames, …)`; on success `Dir.rename` to `path`,
  `write_state = written`; on error `write_state = failed` (the
  checkout stays pinned by state). `.load`: `co.load()` (a fresh
  allocation + `readFrames`), then `touch`. A `load` for a checkout
  that became resident meanwhile is a no-op. Loop.
  `stop()` sets the flag and signals; the loop finishes the queue
  before exiting, so quitting mid-write never leaves a `.part` behind
  unless the process dies.
- The mutex guards the queue and the LRU lists only. The writer holds
  no lock during the file write except `wav.write_mutex`: PR h moves
  `abi.zig`'s `wav_write_mutex` into `wav.zig` so the writer thread,
  `fb_wav_write` and `copyRange` serialise under one rule (`Scratch`
  must not import `abi`).
- Eviction: `touch` walks the LRU tail while `resident_bytes >
  budget_bytes`, skipping pinned and non-`written` entries; `evict`
  frees and subtracts. Reload (`load`) adds and moves to head.
- Zero heap after `start`: lists are intrusive; the writer's buffer is
  `writeFile`'s stack buffer.

### `Playback.bind(ClipSource)`

```zig
pub const ClipSource = union(enum) { frames: []const f32, file: struct { path: []const u8, start: u64, n: u64 } };
pub fn bind(self: *Playback, src: ClipSource, rate: u32, channels: u16) !void;
```

The handshake with `fill()` and the `reopen` logic are unchanged. The
`frames` arm is today's body (`dupe`). The `file` arm allocates
`n * channels` and calls `wav.readFrames` into it. `fb_playback_bind`
keeps its signature (wraps `.frames`); `fb_playback_bind_checkout(pb,
co)` passes `co.source()` and touches the cache; when a `load` job for
`co` is still queued or running it waits for it under the mutex/cond
rather than reading the file a second time.

### ABI

```c
typedef struct FbScratch FbScratch; typedef struct FbCheckout FbCheckout;
typedef struct FbCheckoutInfo { uint32_t rate; uint16_t channels; uint64_t n_frames; uint64_t start_frame; uint8_t write_state; uint64_t resident_bytes; } FbCheckoutInfo;
FbScratch  *fb_scratch_create(uint64_t budget_bytes, FbStatus *status);
FbStatus    fb_scratch_start(FbScratch *); void fb_scratch_stop(FbScratch *); void fb_scratch_destroy(FbScratch *);
void        fb_scratch_set_budget(FbScratch *, uint64_t bytes);
uint64_t    fb_scratch_resident_bytes(const FbScratch *);
FbCheckout *fb_checkout_create(FbScratch *, FbRing *, uint64_t abs_start, uint64_t abs_end, const char *path, FbStatus *status);
FbCheckout *fb_checkout_slice(FbScratch *, const FbCheckout *parent, uint64_t start, uint64_t n, FbStatus *status);
FbCheckout *fb_checkout_open(FbScratch *, const char *path, uint64_t start, uint64_t n, FbStatus *status);   // adoption
void        fb_checkout_info(const FbCheckout *, FbCheckoutInfo *out);
FbStatus    fb_checkout_peak_bins(FbScratch *, FbCheckout *, size_t n_bins, FbPeakBin *out);
void        fb_checkout_pin(FbScratch *, FbCheckout *, uint8_t on);       /* on = pin + preload */
FbStatus    fb_checkout_export(FbScratch *, FbCheckout *, const char *dst, uint64_t start, uint64_t n, FbSubtype, const FbMarkers *);   // PR h passes NULL markers
void        fb_checkout_destroy(FbScratch *, FbCheckout *);
FbStatus    fb_playback_bind_checkout(FbPlayback *, FbScratch *, FbCheckout *);
```

`fb_checkout_export` reads from the file when `write_state` is
`written`/`adopted`, else from `frames` via `writeFile`. Every call
that uses audio calls `Scratch.touch`.

### Python

- `CheckoutManager` keeps ids, states, trims, per-file refcounts and
  the manifests. `Checkout` dataclass fields: `id, handle, path,
  parent_id, start_frame, n_frames, sample_rate, channels,
  abs_sample_start, abs_sample_end, created_at, trim_in_samples,
  trim_out_samples, state, partial`. `duration_seconds` and
  `ram_bytes` read `fb_checkout_info`. `audio`, `trimmed_audio`,
  `temp_path` are deleted.
- `create`/`create_from_abs_range` → `fb_checkout_create` (the caps
  check keeps the count cap only), manifest written with bins from
  `fb_checkout_peak_bins` (540 and 360), then the handle is returned.
- `save`/`render_drag_file` → `fb_checkout_export(start, n)` from the
  trim; `drag_export.py` is unchanged in shape.
- `discard` → `fb_checkout_destroy`, manifest deleted, WAV deleted when
  the path's refcount hits zero.
- `adopt(scratch_dir)`: for every `*.json`, read the manifest, resolve
  `.wav` or `.part` (rename `.part` → `.wav`, set `partial`),
  `fb_wav_info` for the real frame count, `fb_checkout_open`; slices
  resolve their parent's path (a manifest whose parent is missing is
  skipped and logged). Bins come from the manifest; a manifest without
  bins gets them from `fb_checkout_peak_bins` once.
- `AppState` owns the one `FbScratch` (`fb_scratch_create` at
  construction, `start` after slots exist, `stop` in `shutdown`).
  `total_project_ram_bytes` = rings + `fb_scratch_resident_bytes`.
  Adoption runs in `AppState.__init__` after the initial slot: matching
  slots receive their checkouts, others get a new unarmed slot named
  from the manifest (`CaptureSlot.from_quality_preset` with a CUSTOM
  preset).
- Window: `_peak_bins_from_audio` deleted; `_clip_bins_cache` fills
  from the manifest bins; selecting a clip calls `fb_checkout_pin`
  (previous selection unpinned); `_evict_oldest_saved_checkout` and
  the RAM-cap retry loop in `_on_buffer_drag_out` deleted; play binds
  through `scrub_player.bind_checkout(handle)`.
- Preferences: `scratch_dir` (browse button, same shape as the export
  pool row). Changing it applies at next launch (the running writer
  keeps its paths).
- `config.py`: `load_scratch_dir` / `save_scratch_dir`,
  `load_checkout_cache_mb` / `save_checkout_cache_mb`.

### Measurement task (ruling 3)

What is measured: **select→playable** — the wall time from
`fb_checkout_pin(on)` on an evicted clip to `frames != null`, i.e. the
disk read of the scratch file into the Zig copy on the writer thread.
This is not audio-engine latency: the render thread does not run until
`bind` completes, so no xrun or glitch is possible; the cost is a wait
before PLAY can start (and, only in the fallback path, a frozen UI).
It is recorded, not asserted; a timing test would be `perf`-marked and
outside the gates.

On the box, before the budget default is committed: create the
largest clip the ring allows at 192 kHz stereo (900 s = 1,382 MB),
wait for `written`, evict, then time (a) the preload and (b) the
fallback `bind` from file; repeat with a 3 min clip at 48 kHz stereo
(69 MB). Record both on #53 with the scratch disk named.

The number decides the policy shape: if the 3 min / 48 kHz preload is
under the owner's "feels instant" bound (proposed 100 ms; the owner
sets it at measurement time), `DEFAULT_CHECKOUT_CACHE_MB = 0` and RAM
holds only pinned and in-flight clips. Otherwise the default budget is
the smallest value that keeps the last two selected clips of that
size resident. If from-file bind is within tolerance at
every size, the default is 0 and RAM holds only in-flight clips.

### Tests (PR h)

- Zig: `createFromRing` copies the span (ramp) and enqueues; a
  `Scratch` with a fake-slow writer (a `comptime`-injected write fn)
  proves the queue is FIFO, `stop` drains, `.part` → rename, `failed`
  on an unwritable path; cache: budget 0 drops after `written`,
  pinned survives, LRU order, `load` restores the same bytes;
  `Playback.bind(.file)` reads the same clip as `.frames`.
- Python: manager over handles (create, list, discard, refcount on a
  shared path), manifest round-trip, adoption cases (complete,
  `.part`, missing wav, corrupt json, slice with missing parent,
  rate mismatch → new slot), `total_project_ram_bytes` counts
  resident bytes, window pins the selected clip.
- Mutation pins: remove the `pinned` skip in eviction; remove the
  `rename` (a `.part` survives); swap FIFO for LIFO; drop the
  refcount decrement (file deleted while a slice references it).

## PR i — slices as references, markers, handles, spike

### Task 0: the spike (on the box, before code)

Export a 30 s WAV with `cue `/`smpl` markers at 10–15 s, and an
`.alc` sidecar (gzipped XML with a sample reference and the clip's
start/end) built by hand from a Live-saved clip. Drop each into
Ableton Live and record: does the WAV show markers, does the `.alc`
open as a 5 s clip whose edge drags out to 30 s. Result on #53 and in
this spec's table. Ships `.alc` behind a preference only if the second
answer is yes. Anything that cannot be tested on the box is
documented as untested, never assumed.

### Slice creation

- Clip deck drag (trimmed) and "Save trimmed": `fb_checkout_slice
  (parent, trim_in, trim_out - trim_in)` → a new `Checkout` with
  `parent_id`, state `saved`, its own manifest; then the export.
- Buffer deck drag: the root pulls `selection ± handle` from the ring
  (clamped to what the ring holds) and the slice is the selection.
- Deleting a parent leaves its file while a slice references it
  (refcount); the deck shows the slice with the parent's bins cropped
  by `peakBinsFile` on the slice's range.

### Export span

```
half   = (cap_frames - n_slice) / 2          cap_frames = drag_max_mb * 2^20 / (channels * bytes_per_sample)
lo     = max(0, slice_start - half)
hi     = min(parent_frames, slice_end + half)
```

Markers: `cue ` point 1 at `slice_start - lo`, point 2 at
`slice_end - lo`; `smpl` loop 1 `[slice_start - lo, slice_end - lo -
1]`; `LIST/adtl` labels "slice start" / "slice end". `Markers` is a
small struct `wav.copyRange` serialises after the `data` chunk; the
RIFF size covers it. `drag_max_mb` default 200 (2.2 min at 192 kHz
stereo float32, 17 min at 48 kHz). Cap below the slice size is clamped
to the slice.

### Chunk layouts (RIFF/WAVE, verified against the spec in the plan)

- `cue `: `dwCuePoints u32`, then per point 24 bytes: `dwName u32,
  dwPosition u32, fccChunk "data", dwChunkStart u32 = 0, dwBlockStart
  u32 = 0, dwSampleOffset u32`.
- `smpl`: `dwManufacturer 0, dwProduct 0, dwSamplePeriod = 1e9 / rate,
  dwMIDIUnityNote 60, dwMIDIPitchFraction 0, dwSMPTEFormat 0,
  dwSMPTEOffset 0, cSampleLoops, cbSamplerData 0`, then per loop:
  `dwIdentifier, dwType 0, dwStart, dwEnd (inclusive), dwFraction 0,
  dwPlayCount 0`.
- `LIST` size `"adtl"` then `labl` chunks: `dwName u32` + NUL-terminated
  text, padded to even.

### DAW table (documentation, untested unless the spike says so)

| DAW | Markers on drop | Clip bounds from file | Container |
|---|---|---|---|
| Ableton Live | not from WAV cues (restores its own `.asd`) | spike | `.alc` (gzipped XML, sample reference) — spike |
| Reaper | `cue ` → project markers/regions (documented) | no | — |
| Logic | `cue ` → markers (documented) | no | — |
| Cubase | `cue ` → markers | no | — |
| Bitwig | untested | untested | `.bwclip` (undocumented) |
| FL Studio, Studio One, Pro Tools | untested | untested | — |

### Preferences

`drag_max_mb` (spin box, 0 = slice only). `drag_alc_sidecar` only if
the spike passes.

### Tests (PR i)

- Zig: `copyRange` with markers → chunk walk finds `cue `, `smpl`,
  `LIST` after `data` with the right offsets; RIFF size includes them;
  `wav.open` on that file still reports the right `frames`
  (unknown-chunk skip covers it); `slice` shares the parent's path
  and never writes.
- Python: export span formula table (cap ∞, cap = slice, cap
  smaller than slice, slice at the parent's edge — asymmetric clamp),
  a slice mints a `saved` checkout with `parent_id`, parent discard
  keeps the file, the last reference deletes it, buffer drag pulls
  `selection ± half`.
- Mutation pins: `hi` clamp removed → span past the parent; marker
  offsets not rebased by `lo`.

## Error handling

| Case | Behaviour |
|---|---|
| Disk full / io error during the scratch write | `write_state = failed`; the checkout stays resident and unevictable; manifest `state` unchanged; status bar "Scratch write failed: <name>"; the clip works as today (RAM only). No automatic retry. |
| Ring lapped during `createFromRing` | `overwritten` → the same `RuntimeError` the UI reports today. Nothing enqueued. |
| Scratch dir not writable at launch | `fb_scratch_start` still runs; every create fails its write as above; the Preferences row shows the error. |
| Corrupt manifest / missing file at adoption | Skipped, logged, left on disk. |
| `.part` with a header longer than the file | Adopted with the clamped frame count, `partial = true`. |
| Cache miss during play with the file gone | `fb_playback_bind_checkout` → `io_error`; UI reports; the checkout is not destroyed. |
| Quit with jobs queued | `Scratch.stop` drains before join; the window's close waits (a status message shows "Finishing scratch writes…" when the drain exceeds 500 ms). |

## Out of scope

- The ring-to-disk continuous writer (ruling 7): separate epic if ever.
- Any format but WAV; PCM32/ADPCM reading.
- #46, #47, #48, #16 (UI arc, output picker, buffer presets).
- Resampling, per-DAW containers other than the Ableton spike.
- Re-encoding the scratch (it is always FLOAT32 at the capture rate).

## Risks to measure, not assume

- Reload and bind-from-file latency at 192 kHz (the budget default).
- Whether Live honours `.alc` on drop (the spike).
- `std.Io.Threaded.global_single_threaded` from a second thread:
  `writeFile` already runs from Python threads today under the
  `abi.zig` mutex; the writer thread takes the same mutex. Verify
  with the fake-slow writer test that a concurrent `fb_wav_write`
  from Python (a save) and the writer serialise, not deadlock.
- Adoption cost at launch with many manifests (bins are in the
  manifest, so it is JSON parse time only — measure with 100).
- Windows `rename` over an existing target fails; the writer's
  target never exists (ids are unique), and adoption's `.part` →
  `.wav` rename runs only when `.wav` is absent.

## Zig concepts per PR

- g: chunk walking with positional reads, `union(enum)` error
  mapping, sharing a reducer across three callers by passing slices.
- h: intrusive linked lists (zero allocation queues), `std.Io.Mutex` +
  `std.Io.Condition` producer/consumer, control-thread ownership of a
  worker (start/stop symmetry from `Capture`), tagged unions as a
  "source" abstraction for `bind`.
- i: serialising RIFF sub-chunks, integer span arithmetic with
  clamps that fall out of one formula.

## Deviations recorded by the plan (2026-08-30)

- `wav.copyRange` ships in PR g without a `markers` parameter; PR i adds
  it (plan P1).
- The window's `_evict_oldest_saved_checkout` stays for the **count**
  cap (16 per slot); only its RAM-cap branch dies (plan P5).
- No `last_use` field: the LRU is an intrusive list with move-to-head
  (plan P7).
- Python owns the per-file refcount and every file deletion; Zig never
  deletes (plan P8).
- `export_span` is a pure Python function — policy, like `drag_filename`
  (plan P9).
- A slice is minted only after its parent's file is on disk
  (`written`/`adopted`); a `failed` parent cannot be sliced (plan P13).
- Buffer-deck drag mints a **root** = selection ± handles with the
  selection as its trim (no slice); the clip-deck trimmed drag mints a
  slice (plan PR i table).
- A slot recreated for adopted checkouts gets a 60 s ring.
- Quit with jobs queued blocks `shutdown` until the drain completes; the
  ">500 ms" status message is deferred to the PR h hand-off.
