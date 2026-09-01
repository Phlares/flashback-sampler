"""Unit tests for the drag-out export renderer (pure core, no Qt)."""

from __future__ import annotations

import struct
from datetime import datetime
from typing import get_args

import pytest

from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from flashback_sampler.core.checkout import CheckoutManager, CheckoutSubtype
from flashback_sampler.core import drag_export
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


def test_bytes_per_sample_covers_every_export_subtype():
    """render_slice_drag indexes BYTES_PER_SAMPLE with whatever bit_depth
    it is handed, so a subtype the manager accepts but this map lacks is
    a KeyError at drag time."""
    assert set(BYTES_PER_SAMPLE) == set(get_args(CheckoutSubtype))


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


def test_render_slice_drag_discards_the_minted_slice_when_the_export_fails(scratch, tmp_path, monkeypatch):
    """A failing export (full disk, an unwritable pool dir) must not
    strand the slice: the caller never gets a DragRender, so it has no id
    to discard, and adoption would resurrect the orphan at next launch."""
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 200, 300)

    def boom(*a, **kw):
        raise RuntimeError("could not export checkout; the disk is full")

    monkeypatch.setattr(mgr, "export_range", boom)
    with pytest.raises(RuntimeError, match="the disk is full"):
        render_slice_drag(mgr, co.id, tmp_path, "x", now=WHEN)
    assert [c.id for c in mgr.list()] == [co.id]  # the slice is gone
    assert mgr.file_refcount(co.path) == 1  # its refcount on the parent file too
    assert co.path.exists()  # and the parent's own file survived the cleanup


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


def _alc_clip(path):
    import gzip, xml.etree.ElementTree as ET
    with gzip.open(path, "rb") as fh:
        return next(ET.fromstring(fh.read()).iter("AudioClip"))


def test_render_slice_drag_writes_an_alc_sidecar_at_the_slice(scratch, tmp_path):
    """The WAV carries slice + handles; the sidecar says which seconds of
    it are the slice, so Live opens there with the handles recoverable."""
    mgr, co = _mgr_with_checkout(scratch, tmp_path)  # 1000 Hz mono
    mgr.set_trim(co.id, 200, 300)
    handles = 100 * 4 / 2**20  # 50 extra frames each side
    r = render_slice_drag(mgr, co.id, tmp_path, "Deck A", handle_mb=handles, alc=True, now=WHEN)
    assert r.sidecar == r.path.with_suffix(".alc") and r.sidecar.exists()
    clip = _alc_clip(r.sidecar)
    # span 150..350, slice 200..300 -> 0.05 s .. 0.15 s into the export
    assert clip.find("Loop/LoopStart").get("Value") == "0.05"
    assert clip.find("Loop/LoopEnd").get("Value") == "0.15"
    assert clip.find("Loop/HiddenLoopEnd").get("Value") == "0.2"  # 200 exported frames
    assert clip.find("SampleRef/FileRef/Path").get("Value") == r.path.resolve().as_posix()
    assert clip.find("SampleRef/DefaultDuration").get("Value") == "200"


def test_render_slice_drag_writes_no_sidecar_unless_asked(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 200, 300)
    r = render_slice_drag(mgr, co.id, tmp_path, "x", now=WHEN)
    assert r.sidecar is None and list(tmp_path.glob("*.alc")) == []


def test_render_root_drag_writes_a_sidecar_only_when_it_has_markers(scratch, tmp_path):
    """A full-clip drag has no bounds to write, so it gets no sidecar
    even with the preference on."""
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 100, 300)
    full = render_root_drag(mgr, co.id, tmp_path / "full", "x", alc=True, now=WHEN)
    assert full.sidecar is None and list((tmp_path / "full").glob("*.alc")) == []

    marked = render_root_drag(
        mgr, co.id, tmp_path / "marked", "x", markers_at_trim=True, alc=True, now=WHEN
    )
    assert marked.sidecar == marked.path.with_suffix(".alc")
    clip = _alc_clip(marked.sidecar)
    assert clip.find("Loop/LoopStart").get("Value") == "0.1"
    assert clip.find("Loop/LoopEnd").get("Value") == "0.3"


def test_render_slice_drag_cleans_up_the_wav_and_the_slice_when_the_sidecar_fails(
    scratch, tmp_path, monkeypatch
):
    """A sidecar that will not write is a failed render: the caller is
    told, so the pool must not keep the WAV and the mint must not
    survive to be adopted at the next launch."""
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 200, 300)

    def boom(*a, **kw):
        raise OSError("could not write the sidecar; the disk is full")

    monkeypatch.setattr(drag_export, "write_alc", boom)
    with pytest.raises(OSError, match="the disk is full"):
        render_slice_drag(mgr, co.id, tmp_path, "x", alc=True, now=WHEN)
    assert list(tmp_path.glob("*.wav")) == [] and list(tmp_path.glob("*.alc")) == []
    assert [c.id for c in mgr.list()] == [co.id]
    assert co.path.exists()


def test_render_root_drag_unlinks_the_wav_when_the_export_fails(scratch, tmp_path, monkeypatch):
    """A reported failure must leave nothing in the pool: a part-written
    WAV is a file the caller was never told about and cannot delete."""
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    pool = tmp_path / "pool"

    def boom(checkout_id, target, *a, **kw):
        target.write_bytes(b"RIFF part")
        raise RuntimeError("could not export checkout; the disk is full")

    monkeypatch.setattr(mgr, "export_range", boom)
    with pytest.raises(RuntimeError, match="the disk is full"):
        render_root_drag(mgr, co.id, pool, "x", now=WHEN)
    assert list(pool.glob("*")) == []


def test_export_touches_no_sidecar_path_when_the_pref_is_off(scratch, tmp_path, monkeypatch):
    """With alc off there is no sidecar to write or clean up, so a
    failing export must not go near a .alc path at all."""
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    pool = tmp_path / "pool"
    pool.mkdir()
    # A pre-existing .alc that a blanket cleanup would delete.
    bystander = pool / "x_20260715-130509_0.5s.alc"
    bystander.write_bytes(b"someone else's clip")

    def boom(*a, **kw):
        raise RuntimeError("could not export checkout; the disk is full")

    monkeypatch.setattr(mgr, "export_range", boom)
    with pytest.raises(RuntimeError, match="the disk is full"):
        render_root_drag(mgr, co.id, pool, "x", now=WHEN)
    assert bystander.exists()
