"""
Unit tests for the Checkout workflow.

Checkout = pull an immutable snapshot of the live ring buffer, preserve it
in RAM for scrubbing/preview, and optionally save to WAV or FLAC. The ring
buffer must keep writing throughout.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.checkout import Checkout, CheckoutManager
from tests.fixtures.sine_source import ramp_block, sine_block


# ─────────────────────────────────────────────────────────────────────────
# Snapshot correctness
# ─────────────────────────────────────────────────────────────────────────


def test_create_checkout_snapshots_latest_n_seconds():
    buf = AudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.5)  # last 500 samples

    assert isinstance(co, Checkout)
    assert co.audio.shape == (500, 1)
    assert co.audio.dtype == np.float32
    # Most recent 500 samples are 1000..1499
    assert co.audio[0, 0] == pytest.approx(1000.0)
    assert co.audio[-1, 0] == pytest.approx(1499.0)
    assert co.state == "pending"
    assert co.sample_rate == 1000
    assert co.channels == 1
    assert co.abs_sample_end == 1500
    assert co.abs_sample_start == 1000


def test_checkout_id_is_unique():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    mgr = CheckoutManager(buffer=buf)
    a = mgr.create(duration_s=0.2)
    b = mgr.create(duration_s=0.2)
    assert a.id != b.id


def test_list_returns_all_active_checkouts():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))
    mgr = CheckoutManager(buffer=buf)
    a = mgr.create(duration_s=0.2)
    b = mgr.create(duration_s=0.2)
    items = mgr.list()
    assert len(items) == 2
    assert {c.id for c in items} == {a.id, b.id}


def test_checkout_anchor_offset_pulls_earlier_range():
    """
    anchor_offset_s shifts the trailing edge of the slice earlier in time.
    With a 2 s buffer at 1 kHz containing samples 0..1999, a 0.5 s checkout
    ending 0.5 s ago should yield samples 1000..1499 (not the most recent).
    """
    buf = AudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 2000, channels=1))
    mgr = CheckoutManager(buffer=buf)

    co = mgr.create(duration_s=0.5, anchor_offset_s=0.5)
    assert co.audio.shape == (500, 1)
    assert co.audio[0, 0] == pytest.approx(1000.0)
    assert co.audio[-1, 0] == pytest.approx(1499.0)
    # Metadata: abs_sample_end should be total_written - offset_samples
    assert co.abs_sample_end == 2000 - 500  # 2000 total - 500 offset
    assert co.abs_sample_start == co.abs_sample_end - 500


def test_checkout_anchor_offset_zero_matches_default_path():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))
    mgr = CheckoutManager(buffer=buf)

    a = mgr.create(duration_s=0.3, anchor_offset_s=0.0)
    # The fast path should yield identical audio to calling with anchor_offset 0
    assert a.audio.shape == (300, 1)
    assert a.audio[0, 0] == pytest.approx(500.0)
    assert a.audio[-1, 0] == pytest.approx(799.0)


def test_checkout_anchor_offset_clamped_when_past_buffered():
    """
    If the user asks for a clip ending further in the past than the
    buffer has seen, clamp the effective anchor so the slice still
    contains audio. Matches the UI expectation: dragging the rotary
    past the rolling edge anchors "as far back as the buffer allows."
    """
    # Buffer capacity is 10 s; only 2 s of audio buffered so far.
    buf = AudioCircularBuffer(duration_seconds=10.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 2000, channels=1))
    mgr = CheckoutManager(buffer=buf)

    # Ask for a 0.5 s clip ending 5 s ago. There's only 2 s in the ring.
    # effective_offset clamps to just under buffered_s (≈1.999s). That
    # gives get_segment a window of [≈2.499s ago, ≈1.999s ago]; the older
    # bound clamps down to 2.0s, leaving a ~1-sample span. Not useful
    # but non-empty — the UI is expected to prevent this edge case by
    # clamping the rotary max to buffered_s on every tick; this test
    # only verifies the core doesn't return zero-length from a valid
    # user gesture.
    co = mgr.create(duration_s=0.5, anchor_offset_s=5.0)
    assert co.audio.shape[0] > 0


def test_checkout_anchor_offset_just_inside_buffered_pulls_earliest_audio():
    """
    With the rotary dialed all the way back (offset ≈ buffered_s), the
    returned clip points at the OLDEST audio in the ring. A ramp
    starting at 0 should yield sample values near 0 at the clip's head.
    """
    buf = AudioCircularBuffer(duration_seconds=10.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 3000, channels=1))  # 3 s buffered
    mgr = CheckoutManager(buffer=buf)

    co = mgr.create(duration_s=1.0, anchor_offset_s=3.0)
    assert co.audio.shape[0] > 0
    # First sample should come from the start of the ramp (~0.0)
    assert co.audio[0, 0] == pytest.approx(0.0, abs=3.0)


def test_checkout_anchor_offset_mid_buffer_pulls_middle_audio():
    """
    Rotary halfway back: offset = 1.5 s on a 3 s buffer, duration = 1 s.
    Clip window = [2.5 s ago, 1.5 s ago]. With a 0..2999 ramp at 1 kHz,
    that maps to samples [500..1500). First sample ≈ 500, last ≈ 1499.
    """
    buf = AudioCircularBuffer(duration_seconds=10.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 3000, channels=1))
    mgr = CheckoutManager(buffer=buf)

    co = mgr.create(duration_s=1.0, anchor_offset_s=1.5)
    assert co.audio.shape == (1000, 1)
    assert co.audio[0, 0] == pytest.approx(500.0)
    assert co.audio[-1, 0] == pytest.approx(1499.0)


def test_create_from_abs_range_pulls_exact_samples():
    buf = AudioCircularBuffer(duration_seconds=5.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 3000, channels=1))
    mgr = CheckoutManager(buffer=buf)

    co = mgr.create_from_abs_range(abs_start=1000, abs_end=2500)
    assert co.audio.shape == (1500, 1)
    assert co.audio[0, 0] == pytest.approx(1000.0)
    assert co.audio[-1, 0] == pytest.approx(2499.0)
    assert co.abs_sample_start == 1000
    assert co.abs_sample_end == 2500


def test_create_from_abs_range_rejects_inverted():
    buf = AudioCircularBuffer(duration_seconds=5.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 3000, channels=1))
    mgr = CheckoutManager(buffer=buf)
    with pytest.raises(ValueError):
        mgr.create_from_abs_range(abs_start=2000, abs_end=1000)


def test_create_from_abs_range_rejects_past_head():
    buf = AudioCircularBuffer(duration_seconds=5.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1000, channels=1))
    mgr = CheckoutManager(buffer=buf)
    with pytest.raises(RuntimeError, match="past current head"):
        mgr.create_from_abs_range(abs_start=500, abs_end=2000)


def _chunked_write(buf, total_samples: int, chunk_size: int = 500) -> None:
    """Write a sequential ramp in chunks smaller than the ring so the
    buffer's single-wrap write() contract is respected."""
    pos = 0
    while pos < total_samples:
        n = min(chunk_size, total_samples - pos)
        buf.write(ramp_block(pos, n, channels=1))
        pos += n


def test_create_from_abs_range_rejects_overwritten():
    """
    Start of the requested range is older than the ring capacity —
    it's already been overwritten by newer audio.
    """
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    _chunked_write(buf, 3000)  # total_written=3000, only samples 2000..3000 still live
    mgr = CheckoutManager(buffer=buf)
    with pytest.raises(RuntimeError, match="already been overwritten"):
        mgr.create_from_abs_range(abs_start=500, abs_end=1500)


def test_create_from_abs_range_succeeds_within_live_ring():
    """Pull the last 300 ms from a ring that's wrapped several times."""
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    _chunked_write(buf, 2500)  # total_written=2500, samples 1500..2500 still live
    mgr = CheckoutManager(buffer=buf)

    co = mgr.create_from_abs_range(abs_start=2200, abs_end=2500)
    assert co.audio.shape == (300, 1)
    assert co.audio[0, 0] == pytest.approx(2200.0)
    assert co.audio[-1, 0] == pytest.approx(2499.0)


def test_checkout_anchor_offset_rejects_negative():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    mgr = CheckoutManager(buffer=buf)
    with pytest.raises(ValueError):
        mgr.create(duration_s=0.2, anchor_offset_s=-0.5)


def test_checkout_duration_clamped_to_available():
    buf = AudioCircularBuffer(duration_seconds=5.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 200, channels=1))  # only 200 samples buffered
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=3.0)  # ask for 3000, should clamp to 200
    assert co.audio.shape == (200, 1)


# ─────────────────────────────────────────────────────────────────────────
# Non-blocking — writer must keep running during checkout creation
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.timeout(20)
@pytest.mark.perf
def test_checkout_create_does_not_stall_writer():
    """
    Create checkouts while a writer thread is pounding the buffer.

    The writer is paced to ~48 kHz real-time (one 512-sample block every
    ~10.67 ms) — this is what a real audio device does. Writer max
    in-write time must stay under 1 ms, which proves checkout creation
    does not block the audio callback thread.
    """
    buf = AudioCircularBuffer(duration_seconds=30.0, sample_rate=48_000, channels=2)
    mgr = CheckoutManager(buffer=buf, max_active_checkouts=1024, max_total_ram_mb=4096)
    stop = threading.Event()
    results = {}

    def writer():
        max_t = 0.0
        count = 0
        block = np.zeros((512, 2), dtype=np.float32)
        # ~48 kHz real-time: 512 samples / 48000 Hz ≈ 10.67 ms/block
        interval = 512 / 48_000
        next_tick = time.monotonic()
        while not stop.is_set():
            t0 = time.monotonic()
            buf.write(block)
            t1 = time.monotonic()
            dt = t1 - t0
            if dt > max_t:
                max_t = dt
            count += 1
            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = time.monotonic()
        results["max_t"] = max_t
        results["count"] = count

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    # Prime ring with real-time audio — need ~5 s of buffer to take 3s checkouts
    time.sleep(5.0)

    checkouts = []
    for _ in range(15):
        co = mgr.create(duration_s=3.0)
        assert co.audio.shape[0] > 0, (
            f"checkout returned empty; total_written={buf.total_written}"
        )
        checkouts.append(co)

    stop.set()
    t.join(timeout=1.0)
    # ~5.5 s of real-time audio → at least 500 writer iterations
    assert results["count"] > 400, f"writer count too low: {results['count']}"
    assert results["max_t"] < 0.001, (
        f"writer stalled during checkout creation: "
        f"{results['max_t']*1000:.2f}ms"
    )


# ─────────────────────────────────────────────────────────────────────────
# Save — WAV
# ─────────────────────────────────────────────────────────────────────────


def test_save_as_wav_writes_correct_samples(tmp_path: Path):
    """
    WAV is written as 32-bit float by default, enabling bit-perfect round-trip
    for float audio. We use a normalized sine wave and verify exact sample
    equality (within floating-point precision).
    """
    buf = AudioCircularBuffer(duration_seconds=0.5, sample_rate=48_000, channels=1)
    buf.write(sine_block(0, 24_000, freq_hz=440.0, sample_rate=48_000, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.2)  # last 9600 samples

    target = tmp_path / "clip.wav"
    mgr.save(co.id, target, fmt="WAV")

    assert target.exists()
    data, sr = sf.read(str(target), dtype="float32", always_2d=True)
    assert sr == 48_000
    assert data.shape == (9600, 1)
    # WAV defaults to float32 subtype (bit-perfect round-trip)
    assert sf.info(str(target)).subtype == "FLOAT"
    assert np.allclose(data, co.audio, atol=1e-7)
    assert co.state == "saved"


def test_save_as_flac_round_trips(tmp_path: Path):
    buf = AudioCircularBuffer(duration_seconds=0.5, sample_rate=48_000, channels=2)
    buf.write(sine_block(0, 12000, freq_hz=440.0, sample_rate=48_000, channels=2))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.2)  # last 9600 samples
    assert co.audio.shape == (9600, 2)

    target = tmp_path / "clip.flac"
    mgr.save(co.id, target, fmt="FLAC")
    assert target.exists()
    data, sr = sf.read(str(target), dtype="float32", always_2d=True)
    assert sr == 48_000
    assert data.shape == (9600, 2)
    # FLAC is lossless for 16-bit and higher; float32 → FLAC round-trips
    # exactly at the sample values we care about here.
    assert np.allclose(data, co.audio, atol=1e-4)
    assert co.state == "saved"


def test_save_invalid_format_raises(tmp_path: Path):
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.2)
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "clip.mp3", fmt="MP3")


def test_save_unknown_id_raises(tmp_path: Path):
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    mgr = CheckoutManager(buffer=buf)
    with pytest.raises(KeyError):
        mgr.save("nonsense-id", tmp_path / "x.wav", fmt="WAV")


# ─────────────────────────────────────────────────────────────────────────
# Discard
# ─────────────────────────────────────────────────────────────────────────


def test_flushing_buffer_does_not_invalidate_existing_checkouts():
    """
    Checkouts are immutable in-RAM snapshots. Flushing the ring buffer
    must not touch a checkout's audio. This guards the isolation boundary
    between the Checkout lifecycle and the buffer lifecycle.
    """
    buf = AudioCircularBuffer(duration_seconds=0.5, sample_rate=48_000, channels=2)
    buf.write(sine_block(0, 24_000, freq_hz=440.0, sample_rate=48_000, channels=2))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.2)
    snapshot = co.audio.copy()

    buf.flush()
    assert buf.buffered_seconds == 0.0

    # Checkout's audio ndarray is untouched
    assert np.array_equal(co.audio, snapshot)
    # Manager still reports it as active
    assert len(mgr.list()) == 1


def test_discard_removes_from_active_list():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.2)
    mgr.discard(co.id)
    assert co.state == "discarded"
    assert mgr.list() == []


def test_discard_unknown_id_raises():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    mgr = CheckoutManager(buffer=buf)
    with pytest.raises(KeyError):
        mgr.discard("nope")


# ─────────────────────────────────────────────────────────────────────────
# RAM cap
# ─────────────────────────────────────────────────────────────────────────


def test_ram_cap_refuses_new_checkouts_when_exceeded():
    # Buffer: 10 s @ 48k stereo = ~3.7 MB. Each 10s checkout = ~3.7 MB.
    # Cap at 4 MB: second checkout should refuse.
    buf = AudioCircularBuffer(duration_seconds=10.0, sample_rate=48_000, channels=2)
    buf.write(np.zeros((48_000 * 10, 2), dtype=np.float32))
    mgr = CheckoutManager(buffer=buf, max_total_ram_mb=4)
    a = mgr.create(duration_s=10.0)
    assert a is not None
    with pytest.raises(RuntimeError, match="RAM"):
        mgr.create(duration_s=10.0)
    # After discarding, it should work again
    mgr.discard(a.id)
    b = mgr.create(duration_s=10.0)
    assert b is not None


# ─────────────────────────────────────────────────────────────────────────
# Save — subtype & mark_saved
# ─────────────────────────────────────────────────────────────────────────


def _mgr_with_checkout(tmp_path=None):
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.5)
    return mgr, co


def test_save_wav_defaults_to_float32_subtype(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.wav")
    assert sf.info(str(target)).subtype == "FLOAT"


def test_save_flac_defaults_to_pcm_24(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.flac", fmt="FLAC")
    assert sf.info(str(target)).subtype == "PCM_24"


def test_save_flac_coerces_float_to_pcm_24(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.flac", fmt="FLAC", subtype="FLOAT")
    assert sf.info(str(target)).subtype == "PCM_24"


def test_save_explicit_pcm_16(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.wav", subtype="PCM_16")
    assert sf.info(str(target)).subtype == "PCM_16"


def test_save_rejects_unknown_subtype(tmp_path):
    mgr, co = _mgr_with_checkout()
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "clip.wav", subtype="PCM_32_BANANA")


def test_save_mark_saved_false_leaves_state(tmp_path):
    mgr, co = _mgr_with_checkout()
    mgr.save(co.id, tmp_path / "clip.wav", mark_saved=False)
    assert mgr.get(co.id).state == "pending"


def test_mark_saved_sets_state():
    mgr, co = _mgr_with_checkout()
    mgr.mark_saved(co.id)
    assert mgr.get(co.id).state == "saved"


# ─────────────────────────────────────────────────────────────────────────
# Save — native WAV encoder routing
# ─────────────────────────────────────────────────────────────────────────


def test_wav_save_uses_native_encoder_when_available(tmp_path, monkeypatch):
    """WAV saves must route through the Zig encoder when the native
    library is built (this machine) instead of always calling
    soundfile.write -- FLAC always keeps its existing soundfile path
    (native.py has no FLAC encoder)."""
    from flashback_sampler.core import native

    if native.load() is None:
        pytest.skip("flashback_core library not built")

    calls = []
    real = native.wav_write
    monkeypatch.setattr(
        native, "wav_write",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )

    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.wav")

    assert calls, "WAV save did not route through the native encoder"
    # The file soundfile reads back must still match the checkout's audio
    # -- routing to a different encoder must not change the bytes a
    # consumer (Ableton, this repo's own tests) reads back.
    data, sr = sf.read(str(target), dtype="float32", always_2d=True)
    assert sr == co.sample_rate
    assert np.allclose(data, co.trimmed_audio(), atol=1e-7)


def test_flac_save_does_not_use_native_encoder(tmp_path, monkeypatch):
    """FLAC has no native encoder (native.SUBTYPE_INTS covers WAV subtypes
    only) -- FLAC saves must keep going through soundfile regardless of
    whether the native library is present."""
    from flashback_sampler.core import native

    calls = []
    real = native.wav_write
    monkeypatch.setattr(
        native, "wav_write",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )

    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.flac", fmt="FLAC")

    assert not calls, "FLAC save must never call the native WAV encoder"
    assert target.exists()


def test_mark_saved_unknown_id_raises():
    mgr, _ = _mgr_with_checkout()
    with pytest.raises(KeyError):
        mgr.mark_saved("nope")
