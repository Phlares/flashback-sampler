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
  FB_INVALID_ARG = 4,
  FB_OUT_OF_MEMORY = 5
} FbStatus;

typedef enum FbSubtype { FB_FLOAT32 = 0, FB_PCM_24 = 1, FB_PCM_16 = 2 } FbSubtype;
typedef struct FbPeakBin { float min; float max; } FbPeakBin;

typedef struct FbCapture FbCapture; /* opaque */
typedef struct FbMixer FbMixer; /* opaque */
typedef struct FbDevice { uint8_t kind; uint8_t is_default; uint32_t mix_rate; uint16_t mix_channels; char id[128]; char name[128]; } FbDevice;
typedef struct FbCaptureSpec { uint8_t kind; uint32_t pid; uint32_t rate; uint16_t channels; const char *device_id; } FbCaptureSpec;
typedef struct FbCaptureStats { uint8_t running; uint64_t frames_written; uint32_t xruns; uint32_t mix_rate; } FbCaptureStats;
typedef struct FbProcess { uint32_t pid; uint32_t ppid; char name[128]; } FbProcess;

typedef struct FbPlayback FbPlayback; /* opaque */
typedef struct FbPlaybackState { uint8_t running; uint8_t playing; uint64_t cursor; uint64_t clip_frames; uint32_t mix_rate; } FbPlaybackState;

/* status is nullable. FB_INVALID_ARG: rejected config. FB_OUT_OF_MEMORY:
 * the reservation could not be made (issue #41). */
FbRing *fb_ring_create(uint32_t rate, uint16_t channels, double seconds, FbStatus *status);
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
 * either silently corrupts a host that walks the raw buffer directly --
 * the engine's own fb_ring_peak_bins uses storage_frames internally; a
 * host walking the raw buffer must too. Use
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
/* min/max per channel per bin over the newest n_frames (headroom-clamped).
 * out holds n_bins * channels FbPeakBin, out[bin * channels + ch].
 * FB_INVALID_ARG for n_bins == 0; FB_OVERWRITTEN (out zeroed) after three
 * torn attempts; FB_OK with out zeroed for an empty window. */
FbStatus fb_ring_peak_bins(FbRing *, uint64_t n_frames, size_t n_bins, FbPeakBin *out);
/* RMS per channel over the newest n_frames; out holds `channels` floats, zeroed on error. */
FbStatus fb_ring_rms(FbRing *, uint64_t n_frames, float *out);
/* Serialized internally (a mutex guards the whole call) — safe to call
 * from multiple host threads concurrently, unlike every other fb_ring_*
 * export above, which assume single-writer/single-control-thread
 * discipline per the Ring's own concurrency model. */
FbStatus fb_wav_write(const char *path, const float *frames, size_t n_frames,
                      uint32_t rate, uint16_t channels, FbSubtype subtype);

typedef struct FbWavInfo { uint32_t rate; uint16_t channels; uint8_t subtype; uint64_t frames; } FbWavInfo;
/* Reader side of wav.zig. FB_INVALID_ARG: not RIFF/WAVE, no fmt/data, or
 * an unsupported format (PCM32, float64, ...). FB_OUT_OF_RANGE: the span
 * runs past the frames the FILE holds (a crash-truncated file reports its
 * true prefix, not the header's claim). FB_IO_ERROR: OS errors. */
FbStatus fb_wav_info(const char *path, FbWavInfo *out);
/* out holds n_frames * channels floats (channels from fb_wav_info). */
FbStatus fb_wav_read(const char *path, uint64_t start_frame, size_t n_frames, float *out);
/* out holds n_bins * channels FbPeakBin, out[bin * channels + ch].
 * n_frames == 0 returns FB_OK with out zeroed even for an out-of-range
 * start (matches the ring's empty window); fb_wav_read validates the
 * span first. */
FbStatus fb_wav_peak_bins(const char *path, uint64_t start_frame, uint64_t n_frames, size_t n_bins, FbPeakBin *out);

size_t     fb_devices_list(FbDevice *out, size_t max);        /* 0 on non-Windows */
FbCapture *fb_capture_create(FbRing *, const FbCaptureSpec *);/* NULL on non-Windows or bad spec */
FbStatus   fb_capture_start(FbCapture *);                      /* FB_INVALID_ARG if already running, FB_IO_ERROR if spawn failed */
void       fb_capture_stop(FbCapture *);
void       fb_capture_destroy(FbCapture *);                    /* stops first */
void       fb_capture_stats(const FbCapture *, FbCaptureStats *out);
const char*fb_capture_last_error(const FbCapture *);           /* "" when none; valid until destroy */

/* N sources (1..8) summed into `target` by a Zig mixer thread. Staging
 * rings live inside the mixer. NULL: n outside 1..8, a bad spec, no
 * backend on this OS, or out of memory. */
FbMixer   *fb_mixer_create(FbRing *target, const FbCaptureSpec *specs, size_t n);
FbStatus   fb_mixer_start(FbMixer *);                          /* FB_INVALID_ARG if already running, FB_IO_ERROR otherwise */
void       fb_mixer_stop(FbMixer *);
void       fb_mixer_destroy(FbMixer *);                        /* stops first */
void       fb_mixer_stats(const FbMixer *, FbCaptureStats *out);
const char*fb_mixer_last_error(const FbMixer *);               /* own message, else the first source's; "" when none */

size_t     fb_processes_list(FbProcess *out, size_t max);      /* every running process, Toolhelp32; 0 on non-Windows */

/* One clip player: a Zig-owned render thread over a device stream. NULL:
 * non-Windows, or rate == 0, or channels outside 1..2. */
FbPlayback *fb_playback_create(const char *device_id, uint32_t rate, uint16_t channels);
/* Copies `frames` (n_frames * channels floats). FB_INVALID_ARG: channels
 * == 0, channels > 2, or rate == 0. (The core also rejects a sample count
 * not divisible by channels; this entry point always passes a multiple.)
 * FB_OUT_OF_MEMORY: the copy could not be allocated. */
FbStatus    fb_playback_bind(FbPlayback *, const float *frames, size_t n_frames, uint32_t rate, uint16_t channels);
FbStatus    fb_playback_play(FbPlayback *);           /* FB_IO_ERROR if the render thread could not spawn */
void        fb_playback_pause(FbPlayback *);
void        fb_playback_seek(FbPlayback *, uint64_t frames);
void        fb_playback_set_device(FbPlayback *, const char *device_id);
void        fb_playback_state(const FbPlayback *, FbPlaybackState *out);
const char *fb_playback_last_error(const FbPlayback *);  /* "" when none; valid until destroy */
void        fb_playback_destroy(FbPlayback *);            /* stops first, frees the clip */
#endif
