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
    WAV is written as PCM_16 by default (soundfile's default for WAV), so
    the audio must be in [-1, 1]. We use a normalized sine wave so we can
    assert approximate equality across the format's quantization.
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
    # PCM_16 quantization error is ~1/32768 ≈ 3e-5. 1e-3 is very generous.
    assert np.allclose(data, co.audio, atol=1e-3)
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
