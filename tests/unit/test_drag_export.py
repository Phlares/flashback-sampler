"""Unit tests for the drag-out export renderer (pure core, no Qt)."""

from __future__ import annotations

import struct
from datetime import datetime

import pytest

from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.drag_export import (
    BYTES_PER_SAMPLE, DragRender, drag_filename, export_span, render_root_drag,
    render_slice_drag, resolve_collision, sanitize_source_name,
)
from tests.fixtures.sine_source import ramp_block
from tests.fixtures.wavread import read_wav

WHEN = datetime(2026, 7, 15, 13, 5, 9)


@pytest.fixture
def scratch():
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    yield s
    s.close()


def _mgr_with_checkout(scratch, tmp_path):
    buf = NativeAudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))
    mgr = CheckoutManager(buffer=buf, scratch=scratch, scratch_dir=tmp_path / "scratch")
    co = mgr.create(duration_s=0.5)  # 500 samples = 0.5 s
    return mgr, co


def test_sanitize_source_name():
    assert sanitize_source_name("Speakers (Realtek)") == "speakers_realtek"
    assert sanitize_source_name("") == "source"
    assert sanitize_source_name("___") == "source"


def test_drag_filename_format():
    assert (
        drag_filename("My Deck", WHEN, 3.52)
        == "my_deck_20260715-130509_3.5s.wav"
    )


def test_resolve_collision_appends_suffix(tmp_path):
    target = tmp_path / "clip.wav"
    assert resolve_collision(target) == target
    target.write_bytes(b"")
    assert resolve_collision(target) == tmp_path / "clip_2.wav"
    (tmp_path / "clip_2.wav").write_bytes(b"")
    assert resolve_collision(target) == tmp_path / "clip_3.wav"


@pytest.mark.parametrize("parent,s,e,handle_mb,expect", [
    (1000, 400, 500, 0.0, (400, 500)),          # budget 0 = slice only
    (1000, 400, 500, 1e9, (0, 1000)),           # budget ∞ = whole parent
    (1000, 400, 500, 300 * 4 / 2**20, (250, 650)),   # 300 extra mono float frames: 150 each side
    (1000, 50, 150, 300 * 4 / 2**20, (0, 300)),      # clamped at the start; the unused half is not moved
    (1000, 900, 950, 300 * 4 / 2**20, (750, 1000)),  # clamped at the end
    (1000, 400, 500, 50 * 4 / 2**20, (375, 525)),    # a budget smaller than the slice still adds handles; the slice is whole
    (1000, 0, 1000, 10 * 4 / 2**20, (0, 1000)),      # a slice that IS the parent is never truncated
])
def test_export_span(parent, s, e, handle_mb, expect):
    assert export_span(parent, s, e, 1, 4, handle_mb) == expect


def test_render_root_drag_exports_the_whole_clip_with_markers_at_the_trim(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 100, 300)
    r = render_root_drag(mgr, co.id, tmp_path / "pool", "Deck A", markers_at_trim=True, now=WHEN)
    assert r == DragRender(tmp_path / "pool" / "deck_a_20260715-130509_0.5s.wav", co.id, False)
    audio, info = read_wav(r.path)
    assert info.frames == 500 and info.subtype == "FLOAT"
    raw = r.path.read_bytes()
    assert b"cue " in raw and b"smpl" in raw
    assert mgr.get(co.id).state == "pending"  # the caller commits on drop


def test_render_root_drag_without_markers_has_no_cue(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    r = render_root_drag(mgr, co.id, tmp_path, "x", bit_depth="PCM_24", now=WHEN)
    assert b"cue " not in r.path.read_bytes()
    assert read_wav(r.path)[1].subtype == "PCM_24"


def test_render_slice_drag_mints_a_saved_slice_and_exports_the_span(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 200, 300)
    handles = 100 * 4 / 2**20  # 100 extra mono float frames: 50 each side
    r = render_slice_drag(mgr, co.id, tmp_path, "Deck A", handle_mb=handles, now=WHEN)
    assert r.minted and r.checkout_id != co.id
    s = mgr.get(r.checkout_id)
    assert (s.parent_id, s.start_frame, s.n_frames, s.state) == (co.id, 200, 100, "saved")
    assert r.path.name == "deck_a_20260715-130509_0.1s.wav"  # named for the slice, not the span
    audio, info = read_wav(r.path)
    assert info.frames == 200 and audio[0, 0] == pytest.approx(1150.0)  # span 150..350 = slice 200..300 + 50 each side
    raw = r.path.read_bytes()
    assert b"cue " in raw
    # The only test that pins the ctypes markers path with a real value:
    # the first cue point's dwSampleOffset, rebased by lo (200 - 150).
    assert struct.unpack_from("<I", raw, raw.index(b"cue ") + 8 + 4 + 20)[0] == 50


def test_render_slice_drag_on_an_untrimmed_clip_falls_back_to_the_root(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    r = render_slice_drag(mgr, co.id, tmp_path, "x", now=WHEN)
    assert r == DragRender(r.path, co.id, False)
    assert read_wav(r.path)[1].frames == 500


def test_render_creates_pool_dir(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    pool = tmp_path / "nested" / "exports"
    r = render_root_drag(mgr, co.id, pool, "x", now=WHEN)
    assert r.path.parent == pool and r.path.exists()
