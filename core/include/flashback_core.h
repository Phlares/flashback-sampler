/* flashback_core — C ABI for the Zig audio ring engine.
 * Mirrors core/src/abi.zig; keep in lockstep. ctypes does not read this
 * file — it exists for future non-Python hosts (CLAP plugin, mobile). */
#ifndef FLASHBACK_CORE_H
#define FLASHBACK_CORE_H
#include <stddef.h>
#include <stdint.h>

typedef struct FbRing FbRing; /* opaque */

typedef enum FbStatus {
  FB_OK = 0,
  FB_OVERWRITTEN = 1,
  FB_OUT_OF_RANGE = 2,
  FB_IO_ERROR = 3,
  FB_INVALID_ARG = 4
} FbStatus;

typedef enum FbSubtype { FB_FLOAT32 = 0, FB_PCM_24 = 1, FB_PCM_16 = 2 } FbSubtype;

FbRing *fb_ring_create(uint32_t rate, uint16_t channels, double seconds);
void fb_ring_destroy(FbRing *);
void fb_ring_write(FbRing *, const float *frames, size_t n_frames);
uint64_t fb_ring_total_written(const FbRing *);
/* The READABLE window: what get_latest/get_segment-style calls clamp
 * against, what Python's buffer_size reports. Unaffected by the guard
 * band below. */
uint64_t fb_ring_capacity(const FbRing *);
/* The PHYSICAL frame count backing fb_ring_storage: capacity plus a
 * guard band (max_write_frames) that makes an accepted reader's span
 * provably disjoint from whatever the writer might currently be
 * mid-copy into. A caller building a zero-copy view over fb_ring_storage
 * MUST shape that view with THIS value, and any write position derived
 * by modulo must wrap at THIS value too — using fb_ring_capacity for
 * either silently corrupts fb_ring_summary_bins-adjacent code that
 * walks the raw buffer directly (e.g. a peak-bins reader). Use
 * fb_ring_capacity instead for "how much audio can I get back" /
 * clamping calls. The two answer different questions; neither
 * substitutes for the other. */
uint64_t fb_ring_storage_frames(const FbRing *);
const float *fb_ring_storage(const FbRing *);
void fb_ring_set_gain(FbRing *, float gain);
float fb_ring_gain(const FbRing *);
void fb_ring_flush(FbRing *);
FbStatus fb_ring_read(FbRing *, uint64_t abs_start, size_t n_frames, float *out);
FbStatus fb_ring_summary_bins(FbRing *, size_t n_bins, uint64_t n_samples,
                              uint64_t bin_span_frames, float *out_rms);
/* Serialized internally (a mutex guards the whole call) — safe to call
 * from multiple host threads concurrently, unlike every other fb_ring_*
 * export above, which assume single-writer/single-control-thread
 * discipline per the Ring's own concurrency model. */
FbStatus fb_wav_write(const char *path, const float *frames, size_t n_frames,
                      uint32_t rate, uint16_t channels, FbSubtype subtype);
#endif
