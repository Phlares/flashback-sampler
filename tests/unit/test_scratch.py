"""NativeScratch + checkout handles over the real library: create from a
ring, background write, adoption, slice, export, peak bins, pin/evict."""
from __future__ import annotations

import time
from unittest import mock

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from tests.fixtures.wavread import read_wav


def _wait_state(scratch, h, want: str, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if native.WRITE_STATES[scratch.checkout_info(h).write_state] == want:
            return
        time.sleep(0.005)
    raise AssertionError(f"checkout never reached {want}")


@pytest.fixture
def ring():
    buf = NativeAudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=2)
    audio = np.zeros((1500, 2), dtype=np.float32)
    audio[:, 0] = np.arange(1500, dtype=np.float32)
    audio[:, 1] = -audio[:, 0]
    buf.write(audio)
    yield buf
    buf.close()


@pytest.fixture
def scratch():
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    yield s
    s.close()


def test_create_writes_the_span_to_disk_and_reports_written(ring, scratch, tmp_path):
    p = tmp_path / "a.wav"
    h = scratch.checkout_create(ring, 100, 110, p)
    info = scratch.checkout_info(h)
    assert (info.rate, info.channels, info.n_frames, info.start_frame) == (1000, 2, 10, 0)
    assert info.resident_bytes == 10 * 2 * 4
    _wait_state(scratch, h, "written")
    assert p.exists() and not p.with_suffix(".wav.part").exists()
    audio, wi = read_wav(p)
    assert wi.frames == 10
    assert audio[0, 0] == 100.0 and audio[9, 1] == -109.0
    scratch.checkout_destroy(h)


def test_create_rejects_a_lapped_or_inverted_span(ring, scratch, tmp_path):
    with pytest.raises(RuntimeError, match="out_of_range|overwritten"):
        scratch.checkout_create(ring, 1400, 1600, tmp_path / "x.wav")
    with pytest.raises(ValueError):
        scratch.checkout_create(ring, 10, 10, tmp_path / "y.wav")


def test_budget_zero_evicts_after_write_and_peak_bins_still_work(ring, scratch, tmp_path):
    scratch.set_budget(0)
    h = scratch.checkout_create(ring, 0, 1000, tmp_path / "b.wav")
    before = scratch.checkout_peak_bins(h, 10)
    _wait_state(scratch, h, "written")
    scratch.checkout_pin(h, False)  # any touch trims to budget
    assert scratch.checkout_info(h).resident_bytes == 0
    after = scratch.checkout_peak_bins(h, 10)  # streamed from the file
    np.testing.assert_array_equal(before, after)
    assert after.shape == (10, 2, 2)
    assert after[0, 0, 0] == 0.0 and after[0, 1, 0] == 99.0
    scratch.checkout_destroy(h)


def test_pin_preloads_and_keeps_resident(ring, scratch, tmp_path):
    scratch.set_budget(0)
    h = scratch.checkout_create(ring, 0, 100, tmp_path / "c.wav")
    _wait_state(scratch, h, "written")
    scratch.checkout_pin(h, False)
    assert scratch.checkout_info(h).resident_bytes == 0
    scratch.checkout_pin(h, True)
    t0 = time.monotonic()
    while scratch.checkout_info(h).resident_bytes == 0 and time.monotonic() - t0 < 5:
        time.sleep(0.005)
    assert scratch.checkout_info(h).resident_bytes == 100 * 2 * 4
    assert scratch.resident_bytes == 800
    scratch.checkout_destroy(h)
    assert scratch.resident_bytes == 0


def test_open_adopts_a_file_and_slice_references_it(ring, scratch, tmp_path):
    p = tmp_path / "d.wav"
    h = scratch.checkout_create(ring, 200, 300, p)
    _wait_state(scratch, h, "written")
    scratch.checkout_destroy(h)
    a = scratch.checkout_open(p, 0, 100)
    assert native.WRITE_STATES[scratch.checkout_info(a).write_state] == "adopted"
    s = scratch.checkout_slice(a, 10, 20)
    si = scratch.checkout_info(s)
    assert (si.start_frame, si.n_frames) == (10, 20)
    bins = scratch.checkout_peak_bins(s, 2)
    assert bins[0, 0, 0] == 210.0  # frames 10..20 of the file = ring 210..220
    with pytest.raises(ValueError):
        scratch.checkout_slice(a, 95, 10)
    scratch.checkout_destroy(s)
    scratch.checkout_destroy(a)


def test_open_clamps_to_the_file_and_rejects_a_start_past_it(ring, scratch, tmp_path):
    p = tmp_path / "e.wav"
    h = scratch.checkout_create(ring, 0, 50, p)
    _wait_state(scratch, h, "written")
    scratch.checkout_destroy(h)
    a = scratch.checkout_open(p, 40, 1000)  # asks for more than the file holds
    assert scratch.checkout_info(a).n_frames == 10
    scratch.checkout_destroy(a)
    with pytest.raises(ValueError):
        scratch.checkout_open(p, 50, 1)
    with pytest.raises(FileNotFoundError):
        scratch.checkout_open(tmp_path / "nope.wav", 0, 1)


def test_export_from_file_agrees_across_two_calls(ring, scratch, tmp_path):
    """R-h6d: the RAM-vs-file race is not reproducible deterministically
    from Python (the write landing is a real race with this test's own
    timing) — the RAM branch is pinned in Zig instead, in
    core/src/abi.zig's "fb_checkout_export: the RAM branch (pre-write)
    and the file branch (post-write, evicted) agree byte-for-byte" test,
    using the write_fn parking seam. This test instead pins file-branch
    determinism: two exports of the same evicted (written, non-resident)
    checkout must produce identical bytes."""
    p = tmp_path / "f.wav"
    h = scratch.checkout_create(ring, 0, 300, p)
    _wait_state(scratch, h, "written")
    scratch.set_budget(0)
    scratch.checkout_pin(h, False)
    assert scratch.checkout_info(h).resident_bytes == 0  # both exports below hit the file branch
    out_a = tmp_path / "a.wav"
    out_b = tmp_path / "b.wav"
    scratch.checkout_export(h, out_a, 100, 50, "PCM_16")
    scratch.checkout_export(h, out_b, 100, 50, "PCM_16")
    a, ai = read_wav(out_a)
    b, bi = read_wav(out_b)
    assert ai.subtype == bi.subtype == "PCM_16" and ai.frames == bi.frames == 50
    np.testing.assert_array_equal(a, b)
    scratch.checkout_destroy(h)


def test_bind_checkout_plays_the_range(ring, scratch, tmp_path):
    # The real library binds; a fake player would not exercise Zig. Use
    # the real player only for bind (no device open happens at bind).
    from flashback_sampler.core.scrub_player import NativeScrubPlayer
    h = scratch.checkout_create(ring, 0, 100, tmp_path / "g.wav")
    player = NativeScrubPlayer(1000, 2)
    try:
        player.bind_checkout(scratch, h, 10, 20, 1000, 2)
        assert player.source_length_samples == 20
    finally:
        player.close()
        scratch.checkout_destroy(h)


def test_checkout_peak_bins_passes_the_real_out_len(ring, scratch, tmp_path):
    """R-h6a: the Python side must pass the ACTUAL FbPeakBin count of the
    (n_bins, channels, 2) buffer it builds — not a formula (n_bins *
    channels) re-derived in parallel, which is exactly the anti-pattern
    R-h6a exists to catch: the two can silently drift apart. Pin against
    an independently-built buffer of the documented output shape, not
    against the implementation's own internal arithmetic."""
    h = scratch.checkout_create(ring, 0, 100, tmp_path / "h.wav")
    channels = int(scratch.checkout_info(h).channels)
    n_bins = 5
    out = np.zeros((n_bins, channels, 2), dtype=np.float32)  # the shape checkout_peak_bins documents building
    lib = scratch._lib
    orig = lib.fb_checkout_peak_bins
    calls = []

    def spy(*a):
        calls.append(a)
        return orig(*a)

    with mock.patch.object(lib, "fb_checkout_peak_bins", side_effect=spy):
        scratch.checkout_peak_bins(h, n_bins)
    assert len(calls) == 1
    assert calls[0][-1] == out.size // 2  # the buffer's own FbPeakBin count
    scratch.checkout_destroy(h)
