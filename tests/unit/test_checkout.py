"""Checkout workflow over Zig handles: create = copy the span out of
the ring into Zig + queue the scratch write; Python holds ids, states,
trims, manifests and per-file refcounts. Audio is asserted by reading
the scratch file back through the Zig reader."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.checkout import Checkout, CheckoutManager
from flashback_sampler.core.manifest import manifest_path, read_manifest
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from tests.fixtures.sine_source import ramp_block
from tests.fixtures.wavread import read_wav


@pytest.fixture
def scratch():
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    yield s
    s.close()


def _mgr(scratch, tmp_path, seconds=2.0, rate=1000, channels=1, frames=1500, **kw):
    buf = NativeAudioCircularBuffer(duration_seconds=seconds, sample_rate=rate, channels=channels)
    if frames:
        buf.write(ramp_block(0, frames, channels=channels))
    return CheckoutManager(buffer=buf, scratch=scratch, scratch_dir=tmp_path, slot_name="Main", **kw)


def _wait_written(mgr, co, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if mgr.write_state(co.id) == "written":
            return
        time.sleep(0.005)
    raise AssertionError("scratch write never landed")


def _audio(mgr, co) -> np.ndarray:
    _wait_written(mgr, co)
    return native.wav_read(co.path, co.start_frame, co.n_frames)


def test_create_checkout_snapshots_latest_n_seconds(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)  # last 500 samples
    assert isinstance(co, Checkout)
    assert (co.n_frames, co.start_frame, co.channels, co.sample_rate) == (500, 0, 1, 1000)
    assert co.state == "pending"
    assert (co.abs_sample_start, co.abs_sample_end) == (1000, 1500)
    audio = _audio(mgr, co)
    assert audio.shape == (500, 1)
    assert audio[0, 0] == pytest.approx(1000.0) and audio[-1, 0] == pytest.approx(1499.0)
    assert co.path == tmp_path / f"{co.id}.wav"


def test_create_writes_a_manifest_with_bins(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    m = read_manifest(manifest_path(tmp_path, co.id))
    assert m is not None and m.slot == "Main" and m.n_frames == 500 and m.parent is None
    assert set(m.bins) == {"540", "360"}
    assert co.bins["540"].shape == (540, 2, 1) and co.bins["360"].shape == (360, 2, 1)
    # 360 bins over 500 frames: binEdge truncation gives bin 0 exactly
    # one frame (frame 0 only, abs sample 1000) -- not two.
    assert co.bins["360"][0, 1, 0] == pytest.approx(1000.0)  # max of the first bin (frame 1000 only)


def test_checkout_id_is_unique(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=500)
    assert mgr.create(duration_s=0.2).id != mgr.create(duration_s=0.2).id


def test_list_returns_all_active_checkouts(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=800)
    a = mgr.create(duration_s=0.2)
    b = mgr.create(duration_s=0.3)
    assert [c.id for c in mgr.list()] == [a.id, b.id]


def test_checkout_anchor_offset_pulls_earlier_range(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5, anchor_offset_s=0.5)  # ends 500 samples ago: 500..999
    assert (co.abs_sample_start, co.abs_sample_end) == (500, 1000)
    audio = _audio(mgr, co)
    assert audio[0, 0] == pytest.approx(500.0) and audio[-1, 0] == pytest.approx(999.0)


def test_checkout_anchor_offset_clamped_when_past_buffered(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=2.0, frames=800)  # only 0.8 s buffered
    co = mgr.create(duration_s=0.5, anchor_offset_s=5.0)
    # offset clamps to buffered - 1 sample; the span is whatever remains before it
    assert co.abs_sample_end == 1 and co.n_frames == 1


def test_checkout_duration_clamped_to_available(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=300)
    co = mgr.create(duration_s=5.0)
    assert co.n_frames == 300 and co.abs_sample_start == 0


def test_checkout_duration_clamps_to_oldest_on_a_lapped_ring(scratch, tmp_path):
    # R-h8e: the plain duration-clamped test above has oldest == 0, so it
    # cannot pin the `max(oldest, ...)` clamp in create(). This ring has
    # capacity 1000 frames but 1500 written -- only samples 500..1500 are
    # still live -- so a 5 s request must clamp to the oldest live sample,
    # not run off the front of the ring. This also wraps the ring
    # (1500 written into 1000 frames of capacity) -- R-h8j: the
    # `_audio()` check below is what pins a correct wrapped-physical-read
    # (dropping test_create_from_abs_range_succeeds_within_live_ring in
    # the h8 rewrite lost that pin; this restores it).
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=1500)
    co = mgr.create(duration_s=5.0)
    assert co.n_frames == 1000 and co.abs_sample_start == 500 and co.abs_sample_end == 1500
    audio = _audio(mgr, co)
    assert audio[0, 0] == pytest.approx(500.0) and audio[-1, 0] == pytest.approx(1499.0)


def test_create_has_no_anchor_parameter():
    """#75: `anchor` raised NotImplementedError for every value but
    "latest" and no caller passed anything else. A second anchor comes
    back with its implementation, not as a parameter that refuses."""
    import inspect
    assert "anchor" not in inspect.signature(CheckoutManager.create).parameters


def test_checkout_anchor_offset_rejects_negative(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    with pytest.raises(ValueError):
        mgr.create(duration_s=0.1, anchor_offset_s=-1.0)
    with pytest.raises(ValueError):
        mgr.create(duration_s=0.0)


def test_create_from_abs_range_pulls_exact_samples(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create_from_abs_range(200, 260)
    audio = _audio(mgr, co)
    assert audio.shape == (60, 1) and audio[0, 0] == pytest.approx(200.0)


def test_create_from_abs_range_rejects_inverted_past_head_and_overwritten(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=1500)  # capacity 1000; 500..1500 live
    with pytest.raises(ValueError):
        mgr.create_from_abs_range(10, 10)
    with pytest.raises(RuntimeError, match="past current head"):
        mgr.create_from_abs_range(1400, 1600)
    with pytest.raises(RuntimeError, match="overwritten"):
        mgr.create_from_abs_range(100, 200)


def test_max_active_cap_refuses_new_checkouts(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, max_active_checkouts=2)
    mgr.create(duration_s=0.1)
    mgr.create(duration_s=0.1)
    with pytest.raises(RuntimeError, match="Maximum active checkouts"):
        mgr.create(duration_s=0.1)


@pytest.mark.timeout(20)
@pytest.mark.perf
def test_checkout_create_does_not_stall_writer(scratch, tmp_path):
    """R-h8h: restores the pre-h8 shape (48 kHz-paced writer, per-write
    timing, `@pytest.mark.perf`) that a prior rewrite dropped in favor of
    a busy-loop + a `written[0] > 4096 * 5` assertion any spinning thread
    satisfies (~54% flaky on this box for reasons unrelated to
    checkout.py). `perf` keeps this out of the default filtered run --
    it costs ~5.5 s and asserts a real timing bound."""
    buf = NativeAudioCircularBuffer(duration_seconds=30.0, sample_rate=48_000, channels=2)
    mgr = CheckoutManager(buffer=buf, scratch=scratch, scratch_dir=tmp_path, slot_name="Main", max_active_checkouts=1024)
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
    # Prime the ring with real-time audio -- need ~5 s of buffer to take 3 s checkouts
    time.sleep(5.0)

    checkouts = []
    for _ in range(15):
        co = mgr.create(duration_s=3.0)
        assert co.n_frames > 0, (
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


def test_save_as_wav_writes_correct_samples(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    out = mgr.save(co.id, tmp_path / "out" / "clip.wav")
    audio, info = read_wav(out)
    assert info.frames == 500 and info.subtype == "FLOAT"
    assert audio[0, 0] == pytest.approx(1000.0)
    assert mgr.get(co.id).state == "saved"


def test_save_trimmed_uses_the_trim_and_updates_the_manifest(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    mgr.set_trim(co.id, 100, 300)
    assert co.trim_range() == (100, 200) and co.has_trim()
    out = mgr.save(co.id, tmp_path / "t.wav", trimmed=True, subtype="PCM_16", mark_saved=False)
    audio, info = read_wav(out)
    assert info.frames == 200 and info.subtype == "PCM_16"
    assert mgr.get(co.id).state == "pending"
    m = read_manifest(manifest_path(tmp_path, co.id))
    assert (m.trim_in, m.trim_out) == (100, 300)
    full = mgr.save(co.id, tmp_path / "f.wav", trimmed=False)
    assert read_wav(full)[1].frames == 500


def test_save_validation(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.1)
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "x.flac", fmt="FLAC")
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "x.wav", subtype="PCM_8")
    with pytest.raises(KeyError):
        mgr.save("nope", tmp_path / "x.wav")
    with pytest.raises(KeyError):
        mgr.mark_saved("nope")
    with pytest.raises(KeyError):
        mgr.discard("nope")


def test_mark_saved_sets_state(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.1)
    mgr.mark_saved(co.id)
    assert mgr.get(co.id).state == "saved"
    m = read_manifest(manifest_path(tmp_path, co.id))
    assert m.state == "saved"


def test_discard_removes_manifest_and_wav(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.1)
    _wait_written(mgr, co)
    mgr.discard(co.id)
    assert mgr.list() == []
    assert not manifest_path(tmp_path, co.id).exists() and not co.path.exists()


def test_discard_cleans_up_manifest_and_wav(scratch, tmp_path):
    # R-h8i: renamed from ..._before_the_write_lands_still_cleans_up --
    # write_state reads "written" immediately after create() returns on
    # this box (measured), so this never actually exercises a discard
    # racing an in-flight write. That race is an untested coverage gap,
    # not something this test proves; see the h8 fix report.
    mgr = _mgr(scratch, tmp_path, seconds=2.0, rate=48_000, channels=2, frames=96_000)
    co = mgr.create(duration_s=2.0)  # 768 KB
    mgr.discard(co.id)
    assert not co.path.exists() and not (tmp_path / f"{co.id}.wav.part").exists()


def test_flushing_buffer_does_not_invalidate_existing_checkouts(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    mgr._buffer.flush()  # noqa: SLF001
    audio = _audio(mgr, co)
    assert audio[0, 0] == pytest.approx(1000.0)


def test_pin_and_peak_bins_and_resident_bytes(scratch, tmp_path):
    scratch.set_budget(0)
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    _wait_written(mgr, co)
    mgr.pin(None)  # any touch trims to budget 0
    assert mgr.resident_bytes(co.id) == 0
    bins = mgr.peak_bins(co.id, 10)  # streamed from the file
    assert bins.shape == (10, 2, 1)
    np.testing.assert_array_equal(bins, native.wav_peak_bins(co.path, 0, 500, 10))
    mgr.pin(co.id)
    t0 = time.monotonic()
    while mgr.resident_bytes(co.id) == 0 and time.monotonic() - t0 < 5:
        time.sleep(0.005)
    assert mgr.resident_bytes(co.id) == 500 * 4


def test_adopt_root_and_slice_share_one_file_with_a_refcount(scratch, tmp_path):
    from flashback_sampler.core.manifest import Manifest, write_manifest
    mgr = _mgr(scratch, tmp_path)
    root = mgr.create(duration_s=0.5)
    _wait_written(mgr, root)
    m_root = read_manifest(manifest_path(tmp_path, root.id))
    mgr.close()  # handles gone, files stay
    mgr2 = _mgr(scratch, tmp_path, frames=0)
    a = mgr2.adopt_root(m_root, root.path, partial=False)
    assert a.id == root.id and a.n_frames == 500 and mgr2.write_state(a.id) == "adopted"
    assert a.bins["540"].shape == (540, 2, 1)  # from the manifest, no audio read
    m_slice = Manifest(id="slice1", slot="Main", rate=1000, channels=1, abs_start=1100, abs_end=1200,
                       created_at=2.0, parent=root.id, start_frame=100, n_frames=100, trim_in=0, trim_out=0,
                       state="saved", partial=False, bins=None)
    write_manifest(tmp_path, m_slice)
    s = mgr2.adopt_slice(m_slice, a)
    assert s.path == a.path and s.start_frame == 100 and s.parent_id == root.id
    assert s.bins["360"].shape == (360, 2, 1)  # computed once, from the file
    assert mgr2.file_refcount(a.path) == 2
    mgr2.discard(a.id)
    assert a.path.exists() and mgr2.file_refcount(a.path) == 1
    mgr2.discard(s.id)
    assert not a.path.exists() and mgr2.file_refcount(a.path) == 0


def test_adopt_root_partial_clamps_to_the_file(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    root = mgr.create(duration_s=0.5)
    _wait_written(mgr, root)
    m = read_manifest(manifest_path(tmp_path, root.id))
    mgr.close()
    # chop the file to 200 frames
    data = root.path.read_bytes()
    root.path.write_bytes(data[:44 + 200 * 4])
    mgr2 = _mgr(scratch, tmp_path, frames=0)
    a = mgr2.adopt_root(m, root.path, partial=True)
    assert a.n_frames == 200 and a.partial is True
    assert read_manifest(manifest_path(tmp_path, a.id)).partial is True


def _boom(*_a, **_k):
    raise FileNotFoundError("scratch dir gone")


def test_engine_errors_surface_as_one_runtimeerror_everywhere(scratch, tmp_path, monkeypatch):
    """R-h8k: every writer-reaching public method converts the engine's
    (RuntimeError, OSError) into a single RuntimeError, regardless of
    which underlying call fails -- create_from_abs_range, export_range
    (save's path), adopt_root, adopt_slice."""
    from flashback_sampler.core.manifest import Manifest, write_manifest

    mgr = _mgr(scratch, tmp_path)
    with monkeypatch.context() as m:
        m.setattr(scratch, "checkout_create", _boom)
        with pytest.raises(RuntimeError):
            mgr.create_from_abs_range(200, 260)

    co = mgr.create(duration_s=0.1)
    with monkeypatch.context() as m:
        m.setattr(scratch, "checkout_export", _boom)
        with pytest.raises(RuntimeError):
            mgr.save(co.id, tmp_path / "x.wav")

    root = mgr.create(duration_s=0.5)
    _wait_written(mgr, root)
    m_root = read_manifest(manifest_path(tmp_path, root.id))
    mgr.close()
    mgr2 = _mgr(scratch, tmp_path, frames=0)
    with monkeypatch.context() as m:
        m.setattr(scratch, "checkout_open", _boom)
        with pytest.raises(RuntimeError):
            mgr2.adopt_root(m_root, root.path, partial=False)

    a = mgr2.adopt_root(m_root, root.path, partial=False)
    m_slice = Manifest(id="slice1", slot="Main", rate=1000, channels=1, abs_start=1100, abs_end=1200,
                       created_at=2.0, parent=a.id, start_frame=100, n_frames=100, trim_in=0, trim_out=0,
                       state="saved", partial=False, bins=None)
    write_manifest(tmp_path, m_slice)
    with monkeypatch.context() as m:
        m.setattr(scratch, "checkout_slice", _boom)
        with pytest.raises(RuntimeError):
            mgr2.adopt_slice(m_slice, a)


def test_create_from_abs_range_cleans_up_on_manifest_failure(scratch, tmp_path, monkeypatch):
    """R-h8m: a failure AFTER checkout_create (bins or the manifest
    write) must not leak the handle or leave a manifest-less orphan
    .wav -- adoption only scans *.json, so an orphan file would never
    be found or cleaned up. Uses a large (768 KB) span so the scratch
    write is still in flight when the manifest write fails; without
    `checkout_destroy` in the cleanup path the job keeps running
    unowned and eventually writes the orphan to disk anyway, so a
    single immediate glob (nothing written *yet*) would pass by
    accident -- poll instead."""
    mgr = _mgr(scratch, tmp_path, seconds=2.0, rate=48_000, channels=2, frames=96_000)
    monkeypatch.setattr(CheckoutManager, "_write_manifest", _boom)
    with pytest.raises(RuntimeError):
        mgr.create(duration_s=2.0)
    assert mgr.list() == []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        if list(tmp_path.glob("*.wav")) or list(tmp_path.glob("*.wav.part")):
            break
        time.sleep(0.02)
    assert list(tmp_path.glob("*.wav")) == [] and list(tmp_path.glob("*.wav.part")) == []


def test_cleanup_after_create_failure_does_not_mask_the_original_error(scratch, tmp_path, monkeypatch):
    """h8 round 2: if checkout_destroy ITSELF raises while cleaning up
    after a create failure, that must not replace the original error --
    the caller still needs to know the manifest write (or whatever
    actually failed) is what went wrong, not that cleanup also failed."""
    mgr = _mgr(scratch, tmp_path)
    monkeypatch.setattr(CheckoutManager, "_write_manifest", _boom)
    monkeypatch.setattr(scratch, "checkout_destroy", _boom)
    with pytest.raises(RuntimeError, match="could not create checkout"):
        mgr.create(duration_s=0.1)


def test_slice_references_the_parent_file_and_is_saved(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    parent = mgr.create(duration_s=0.5)
    s = mgr.slice(parent.id, 100, 200)
    assert (s.parent_id, s.path, s.start_frame, s.n_frames, s.state) == (parent.id, parent.path, 100, 200, "saved")
    assert s.bins["360"].shape == (360, 2, 1)
    # Bin 0 is empty (360 bins over 200 frames) and stays zero by the
    # peaks.zig rule, so bin 1 is the first populated one. Bin 1 pins the
    # slice's OFFSET (the ramp starts at 1000, so bins over the whole
    # parent file would read ~1001 here); bin 359 pins its LENGTH.
    assert s.bins["360"][1, 0, 0] == pytest.approx(1100.0)  # slice frame 0 = parent frame 100
    assert s.bins["360"][359, 1, 0] == pytest.approx(1299.0)  # slice frame 199 = parent frame 299
    assert mgr.file_refcount(parent.path) == 2
    m = read_manifest(manifest_path(tmp_path, s.id))
    assert m.parent == parent.id and m.start_frame == 100
    mgr.discard(parent.id)
    assert parent.path.exists()
    audio = native.wav_read(s.path, s.start_frame, s.n_frames)
    assert audio[0, 0] == pytest.approx(1100.0)
    mgr.discard(s.id)
    assert not parent.path.exists()


def test_slice_rejects_a_span_past_the_parent_and_a_failed_parent(scratch, tmp_path, monkeypatch):
    mgr = _mgr(scratch, tmp_path)
    parent = mgr.create(duration_s=0.5)
    # `match` is load-bearing: the ENGINE also rejects an out-of-range
    # span (as a bare ValueError "fb_checkout_slice: invalid_arg"), so an
    # unmatched pytest.raises(ValueError) passes with Python's own guard
    # deleted and pins nothing.
    with pytest.raises(ValueError, match="outside the parent"):
        mgr.slice(parent.id, 450, 100)
    monkeypatch.setattr(mgr, "write_state", lambda cid: "failed")
    with pytest.raises(RuntimeError, match="scratch write failed"):
        mgr.slice(parent.id, 0, 10)


def test_slice_rejects_a_parent_discarded_while_it_waited_for_the_write(scratch, tmp_path, monkeypatch):
    """R-h8l: the P13 wait holds no lock, so a concurrent discard can
    land under it. slice must re-look the parent up rather than reuse the
    handle it fetched before the wait -- that one is freed."""
    mgr = _mgr(scratch, tmp_path)
    parent = mgr.create(duration_s=0.5)
    real_write_state = mgr.write_state

    def discard_under_the_wait(cid):
        ws = real_write_state(cid)
        if ws in ("written", "adopted") and cid in mgr._checkouts:
            mgr.discard(cid)
        return ws

    monkeypatch.setattr(mgr, "write_state", discard_under_the_wait)
    with pytest.raises(KeyError):
        mgr.slice(parent.id, 0, 10)
