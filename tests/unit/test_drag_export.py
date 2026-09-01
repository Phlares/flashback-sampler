"""Unit tests for the drag-out export renderer (pure core, no Qt)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.drag_export import (
    drag_filename,
    render_drag_file,
    resolve_collision,
    sanitize_source_name,
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


def test_render_drag_file_writes_wav_without_marking_saved(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    path = render_drag_file(mgr, co.id, tmp_path, "Deck A", now=WHEN)
    assert path == tmp_path / "deck_a_20260715-130509_0.5s.wav"
    _, info = read_wav(path)
    assert info.subtype == "FLOAT"
    assert info.frames == 500
    assert mgr.get(co.id).state == "pending"


def test_render_drag_file_respects_trim_and_bit_depth(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 100, 300)
    path = render_drag_file(
        mgr, co.id, tmp_path, "Deck A", bit_depth="PCM_24", now=WHEN
    )
    _, info = read_wav(path)
    assert info.frames == 200
    assert info.subtype == "PCM_24"
    assert "_0.2s" in path.name


def test_render_drag_file_creates_pool_dir(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    pool = tmp_path / "nested" / "exports"
    path = render_drag_file(mgr, co.id, pool, "x", now=WHEN)
    assert path.parent == pool and path.exists()
