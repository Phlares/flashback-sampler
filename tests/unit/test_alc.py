"""Unit tests for the Ableton Live Clip (.alc) sidecar writer."""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from flashback_sampler.core import alc

# The spike capture is a working note, not a shipped file (`.superpowers/`
# is gitignored), so the byte-identity round trip runs only where it is.
CAPTURE = (
    Path(__file__).resolve().parents[2]
    / ".superpowers" / "sdd" / "2026-08-30-checkout-persistence" / "spike-capture.xml"
)

# What Live wrote for the capture: a 10-15 s clip of a 30 s, 48 kHz,
# 1 440 000-frame, 8 640 230-byte spike.wav in the User Library.
CAPTURED = dict(
    wav_path=Path("D:/Audio/User Library/Samples/Imported/spike.wav"),
    relative_path="Samples/Imported/spike.wav",
    start_s=10.0,
    end_s=15.0,
    frames=1440000,
    rate=48000,
    size=8640230,
)


# A clip whose every value differs from the capture's, so a substitution
# that quietly does nothing shows up as a line that did not change.
SUBSTITUTED = dict(
    wav_path=Path("/pool/take.wav"),
    relative_path="take.wav",
    start_s=3.25,
    end_s=7.5,
    frames=96000,
    rate=24000,
    size=4321,
)

# The elements render_alc_xml is allowed to touch, in document order.
# `Path` and `RelativePath` are the template's two placeholders.
SUBSTITUTED_ELEMENTS = [
    "CurrentEnd", "LoopStart", "LoopEnd", "HiddenLoopEnd", "Name",
    "RelativePath", "Path", "OriginalFileSize", "DefaultDuration",
    "DefaultSampleRate", "WarpMarker", "WarpMarker",
]


def _element_name(line: str) -> str:
    return line.strip().split()[0].lstrip("<")


def _clip(target: Path) -> ET.Element:
    with gzip.open(target, "rb") as fh:
        root = ET.fromstring(fh.read())
    return next(root.iter("AudioClip"))


def _value(clip: ET.Element, path: str) -> str:
    return clip.find(path).get("Value")


@pytest.mark.skipif(not CAPTURE.exists(), reason="spike capture is a local working note")
def test_the_captured_values_round_trip_to_the_capture_byte_for_byte():
    """The template is the capture with two placeholders; substituting
    Live's own values back must reproduce Live's file exactly. Anything
    else means the substitution rewrote bytes Live cares about."""
    assert alc.render_alc_xml(**CAPTURED).encode("utf-8") == CAPTURE.read_bytes()


def test_write_alc_gzips_xml_that_reads_back_as_the_substituted_values(tmp_path):
    wav = tmp_path / "pool" / "deck_a_20260715-130509_2.0s.wav"
    wav.parent.mkdir()
    wav.write_bytes(b"x" * 4321)
    out = alc.write_alc(tmp_path / "deck.alc", wav, 1.5, 3.5, frames=96000, rate=24000)

    assert out == tmp_path / "deck.alc"
    clip = _clip(out)
    assert _value(clip, "Loop/LoopStart") == "1.5"
    assert _value(clip, "Loop/LoopEnd") == "3.5"
    assert _value(clip, "Loop/HiddenLoopStart") == "0"
    assert _value(clip, "Loop/HiddenLoopEnd") == "4"          # 96000 / 24000 s
    assert _value(clip, "CurrentStart") == "0"
    assert _value(clip, "CurrentEnd") == "4.266666666666667"  # 2 s at 128 BPM
    assert _value(clip, "Name") == "deck_a_20260715-130509_2.0s"
    assert _value(clip, "SampleRef/FileRef/Path") == wav.resolve().as_posix()
    assert _value(clip, "SampleRef/FileRef/RelativePath") == wav.name
    assert _value(clip, "SampleRef/FileRef/OriginalFileSize") == "4321"
    assert _value(clip, "SampleRef/DefaultDuration") == "96000"
    assert _value(clip, "SampleRef/DefaultSampleRate") == "24000"
    markers = clip.findall("WarpMarkers/WarpMarker")
    assert [m.get("SecTime") for m in markers] == ["1.5", "1.5146484375"]
    assert [m.get("BeatTime") for m in markers] == ["0", "0.03125"]


def test_write_alc_writes_a_forward_slash_path_even_on_windows(tmp_path):
    wav = tmp_path / "nested" / "pool" / "slice.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"")
    out = alc.write_alc(tmp_path / "slice.alc", wav, 0.0, 1.0, frames=48000, rate=48000)
    path_value = _value(_clip(out), "SampleRef/FileRef/Path")
    assert "\\" not in path_value
    assert path_value.endswith("/nested/pool/slice.wav")


def test_write_alc_escapes_xml_specials_in_the_path_and_the_name(tmp_path):
    """The export pool is a user-chosen folder; `&` in it must not break
    the XML Live parses."""
    wav = tmp_path / "a & b" / "x&y.wav"
    wav.parent.mkdir()
    wav.write_bytes(b"")
    out = alc.write_alc(tmp_path / "amp.alc", wav, 0.0, 1.0, frames=48000, rate=48000)
    clip = _clip(out)
    assert _value(clip, "Name") == "x&y"
    assert _value(clip, "SampleRef/FileRef/Path").endswith("/a & b/x&y.wav")


def test_render_alc_xml_escapes_a_quote_in_the_path(tmp_path):
    """A double quote would close the attribute Live reads the path from.
    Legal in a POSIX file name, so escape it rather than trust the OS."""
    xml = alc.render_alc_xml(
        wav_path=Path('/pool/say "hi".wav'), relative_path='say "hi".wav',
        start_s=0.0, end_s=1.0, frames=48000, rate=48000, size=44,
    )
    import xml.etree.ElementTree as _ET
    clip = next(_ET.fromstring(xml).iter("AudioClip"))
    assert clip.find("SampleRef/FileRef/Path").get("Value") == '/pool/say "hi".wav'
    assert clip.find("Name").get("Value") == 'say "hi"'


def test_write_alc_output_is_gzipped_xml(tmp_path):
    wav = tmp_path / "s.wav"
    wav.write_bytes(b"")
    out = alc.write_alc(tmp_path / "s.alc", wav, 0.0, 1.0, frames=48000, rate=48000)
    assert gzip.decompress(out.read_bytes()).startswith(b"<?xml")


def test_render_alc_xml_rejects_a_template_that_lost_an_element(monkeypatch):
    """A silent no-op substitution would ship a clip pointing at the
    spike sample, so a missing element is an error, not a pass-through."""
    broken = alc.template_text().replace('<LoopEnd Value="15" />', "")
    monkeypatch.setattr(alc, "template_text", lambda: broken)
    with pytest.raises(ValueError, match="LoopEnd"):
        alc.render_alc_xml(**CAPTURED)


def test_rendering_changes_exactly_the_listed_elements_and_nothing_else():
    """The byte-identity property, pinned against the COMMITTED template
    alone so it runs everywhere: every other line of Live's file must
    survive a render untouched."""
    before = alc.template_text().splitlines()
    after = alc.render_alc_xml(**SUBSTITUTED).splitlines()
    assert len(after) == len(before)
    changed = [(i, a) for i, (a, b) in enumerate(zip(after, before)) if a != b]
    assert [_element_name(line) for _, line in changed] == SUBSTITUTED_ELEMENTS


def test_rendering_leaves_the_session_level_loop_start_alone():
    """`LiveSet/Transport/LoopStart` shares its name with the clip's
    `Loop/LoopStart`. A substitution matched by element name instead of
    by the captured line would move the set's loop brace."""
    root = ET.fromstring(alc.render_alc_xml(**SUBSTITUTED))
    assert root.find("LiveSet/Transport/LoopStart").get("Value") == "8"


def test_numbers_are_plain_decimals_never_exponents():
    """A one-frame offset at 48 kHz is 2.08e-05, and `repr` would write
    it in exponent notation -- which the capture never contains and Live
    has never been shown to parse."""
    xml = alc.render_alc_xml(
        wav_path=Path("/pool/take.wav"), relative_path="take.wav",
        start_s=1 / 48000, end_s=2 / 48000, frames=48000, rate=48000, size=44,
    )
    clip = next(ET.fromstring(xml).iter("AudioClip"))
    start = clip.find("Loop/LoopStart").get("Value")
    assert "e" not in start.lower() and start.startswith("0.0000208")
    assert "e" not in clip.find("CurrentEnd").get("Value").lower()
    marker = clip.findall("WarpMarkers/WarpMarker")[0].get("SecTime")
    assert "e" not in marker.lower()


def test_write_alc_removes_a_half_written_file(tmp_path, monkeypatch):
    """The writer owns its own output: a gzip write that dies partway
    must not leave a truncated .alc for the drag to offer."""
    wav = tmp_path / "s.wav"
    wav.write_bytes(b"")
    target = tmp_path / "s.alc"

    def fake_open(path, mode):
        Path(path).write_bytes(b"half a clip")
        raise OSError("could not write; the disk is full")

    monkeypatch.setattr(alc.gzip, "open", fake_open)
    with pytest.raises(OSError, match="the disk is full"):
        alc.write_alc(target, wav, 0.0, 1.0, frames=48000, rate=48000)
    assert not target.exists()
