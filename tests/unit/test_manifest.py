from __future__ import annotations

import json

import numpy as np

from flashback_sampler.core.manifest import (
    Manifest, bins_from_json, bins_to_json, manifest_path, read_manifest,
    resolve_audio, scan, write_manifest,
)


def _m(**kw) -> Manifest:
    base = dict(id="abc123", slot="Main", rate=48_000, channels=2, abs_start=10, abs_end=110,
                created_at=1.0, parent=None, start_frame=0, n_frames=100, trim_in=0, trim_out=0,
                state="pending", partial=False, bins=None)
    base.update(kw)
    return Manifest(**base)


def test_write_then_read_round_trips_including_bins(tmp_path):
    bins = {"540": np.arange(540 * 2 * 2, dtype=np.float32).reshape(540, 2, 2) / 7.0}
    m = _m(bins=bins_to_json(bins))
    p = write_manifest(tmp_path, m)
    assert p == manifest_path(tmp_path, "abc123") == tmp_path / "abc123.json"
    back = read_manifest(p)
    assert back is not None
    assert back.id == "abc123" and back.n_frames == 100 and back.parent is None
    got = bins_from_json(back.bins, channels=2)
    np.testing.assert_array_equal(got["540"], bins["540"])


def test_write_is_atomic_and_leaves_no_tmp(tmp_path):
    write_manifest(tmp_path, _m())
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads((tmp_path / "abc123.json").read_text())["id"] == "abc123"


def test_read_corrupt_or_wrong_shape_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert read_manifest(p) is None
    p.write_text(json.dumps({"id": "x"}))  # missing fields
    assert read_manifest(p) is None
    p.write_text(json.dumps([1, 2]))
    assert read_manifest(p) is None


def test_scan_orders_roots_before_slices_and_by_created_at(tmp_path):
    write_manifest(tmp_path, _m(id="s1", parent="r2", created_at=0.5))
    write_manifest(tmp_path, _m(id="r2", created_at=2.0))
    write_manifest(tmp_path, _m(id="r1", created_at=1.0))
    (tmp_path / "junk.json").write_text("nope")
    (tmp_path / "other.txt").write_text("x")
    assert [m.id for m in scan(tmp_path)] == ["r1", "r2", "s1"]


def test_scan_on_a_missing_dir_is_empty(tmp_path):
    assert scan(tmp_path / "absent") == []


def test_resolve_audio_prefers_wav_then_adopts_part(tmp_path):
    m = _m(id="r1")
    assert resolve_audio(tmp_path, m) is None
    (tmp_path / "r1.wav.part").write_bytes(b"RIFF")
    got = resolve_audio(tmp_path, m)
    assert got == (tmp_path / "r1.wav", True)
    assert (tmp_path / "r1.wav").exists() and not (tmp_path / "r1.wav.part").exists()
    assert resolve_audio(tmp_path, m) == (tmp_path / "r1.wav", False)


def test_resolve_audio_keeps_wav_when_both_exist(tmp_path):
    m = _m(id="r1")
    (tmp_path / "r1.wav").write_bytes(b"RIFF")
    (tmp_path / "r1.wav.part").write_bytes(b"RIFF")
    assert resolve_audio(tmp_path, m) == (tmp_path / "r1.wav", False)
    assert (tmp_path / "r1.wav.part").exists()  # left for the user; never deleted here


def test_bins_from_json_skips_a_bin_whose_flat_length_does_not_match_its_key():
    # "540" claims 540 * 2 * channels floats; give it too few.
    got = bins_from_json({"540": [1.0, 2.0, 3.0]}, channels=2)
    assert got == {}
