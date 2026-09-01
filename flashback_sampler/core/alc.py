"""
Write an Ableton Live Clip (`.alc`) pointing at an exported WAV.

A `.alc` is a gzipped Live-set XML holding one `AudioClip`. Dropping one
on a Live track opens the sample at the clip's bounds with the rest of
the file still reachable by dragging the clip edge — which is exactly
the drag-out contract: the WAV carries the slice plus handle audio, the
sidecar says where the slice is.

The file is `alc_template.xml`, captured from Live 12.3.6 on 2026-09-01
(a 10-15 s clip of a 30 s, 48 kHz spike.wav) with two placeholders:
`SAMPLE_PATH` and `SAMPLE_NAME`. Substitution is TEXTUAL, one element
value at a time — Live's own bytes round-trip untouched, which no XML
re-serialisation can promise. A substitution that matches no element (or
more than one) raises: shipping a clip that still points at the spike
sample would look like success.

The clip is unwarped (`IsWarped Value="false"`), so its bounds are
SECONDS into the sample, not beats or frames.

Substituted elements, all under
`LiveSet/Tracks/AudioTrack/DeviceChain/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/AudioClip`:

| Element                             | Unit    | Value                          |
|-------------------------------------|---------|--------------------------------|
| `Loop/LoopStart`                    | seconds | slice start in the WAV         |
| `Loop/LoopEnd`                      | seconds | slice end in the WAV           |
| `Loop/HiddenLoopEnd`                | seconds | whole WAV length (`frames/rate`) |
| `CurrentEnd`                        | beats   | slice length at `TEMPLATE_TEMPO` |
| `Name`                              | text    | the WAV's stem (clip title)    |
| `SampleRef/FileRef/Path`            | text    | absolute WAV path, forward slashes |
| `SampleRef/FileRef/RelativePath`    | text    | the WAV's file name            |
| `SampleRef/FileRef/OriginalFileSize`| bytes   | the WAV's size on disk         |
| `SampleRef/DefaultDuration`         | frames  | the WAV's frame count          |
| `SampleRef/DefaultSampleRate`       | Hz      | the WAV's sample rate          |
| `WarpMarkers/WarpMarker[0]@SecTime` | seconds | slice start                    |
| `WarpMarkers/WarpMarker[1]@SecTime` | seconds | slice start + 1/32 beat        |

Left as captured on purpose: `Loop/HiddenLoopStart` and `CurrentStart`
are 0 for every clip; `OriginalCrc` and `LastModDate` are Live's own
bookkeeping (the spike dropped a clip whose CRC did not describe the
file and Live opened it anyway); `ScrollerTimePreserver` is view state;
everything outside the `AudioClip` is session state.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from xml.sax.saxutils import escape

# The template's `Tempo/Manual`. Live needs the clip length in beats, so
# the seconds the caller gives are converted at this tempo; changing the
# template means changing this number with it.
TEMPLATE_TEMPO = 128.0

# The second warp marker sits one 1/32 beat past the first (`BeatTime
# 0.03125`), which is what the capture holds; both markers move with the
# clip start so the unwarped sample plays at pitch.
_SECOND_MARKER_BEATS = 0.03125

_TEMPLATE = Path(__file__).with_name("alc_template.xml")


def template_text() -> str:
    """The captured Live XML, placeholders and all. Read as bytes so the
    file's own line endings survive on every platform."""
    return _TEMPLATE.read_bytes().decode("utf-8")


def _attr(text: str) -> str:
    """XML-escape a value for an attribute. `saxutils.escape` leaves the
    double quote alone, and every substituted text lands inside
    `Value="..."` -- the pool folder is user-chosen and a quote is a
    legal file name character on POSIX."""
    return escape(str(text), {'"': "&quot;"})


def _num(value: float) -> str:
    """Live writes whole numbers without a decimal point; `repr` matches
    the precision the capture shows for the rest."""
    f = float(value)
    return str(int(f)) if f.is_integer() else repr(f)


def _replace_once(text: str, old: str, new: str) -> str:
    n = text.count(old)
    if n != 1:
        raise ValueError(f"alc template: expected 1 occurrence of {old!r}, found {n}")
    return text.replace(old, new, 1)


def render_alc_xml(
    *,
    wav_path: Path | str,
    relative_path: str,
    start_s: float,
    end_s: float,
    frames: int,
    rate: int,
    size: int,
) -> str:
    """The template with this clip's values in it. `start_s`/`end_s` are
    seconds into the WAV at `wav_path`; `frames`/`rate`/`size` describe
    the whole WAV."""
    wav_path = Path(wav_path)
    beat_s = 60.0 / TEMPLATE_TEMPO
    subs = (
        ('<CurrentEnd Value="10.666666666666666" />',
         f'<CurrentEnd Value="{_num((end_s - start_s) / beat_s)}" />'),
        ('<LoopStart Value="10" />', f'<LoopStart Value="{_num(start_s)}" />'),
        ('<LoopEnd Value="15" />', f'<LoopEnd Value="{_num(end_s)}" />'),
        ('<HiddenLoopEnd Value="30" />', f'<HiddenLoopEnd Value="{_num(frames / rate)}" />'),
        ('<Name Value="spike" />', f'<Name Value="{_attr(wav_path.stem)}" />'),
        ('<RelativePath Value="SAMPLE_NAME" />',
         f'<RelativePath Value="{_attr(relative_path)}" />'),
        ('<Path Value="SAMPLE_PATH" />', f'<Path Value="{_attr(wav_path.as_posix())}" />'),
        ('<OriginalFileSize Value="8640230" />', f'<OriginalFileSize Value="{int(size)}" />'),
        ('<DefaultDuration Value="1440000" />', f'<DefaultDuration Value="{int(frames)}" />'),
        ('<DefaultSampleRate Value="48000" />', f'<DefaultSampleRate Value="{int(rate)}" />'),
        ('<WarpMarker Id="87" SecTime="10" BeatTime="0" />',
         f'<WarpMarker Id="87" SecTime="{_num(start_s)}" BeatTime="0" />'),
        ('<WarpMarker Id="88" SecTime="10.0146484375" BeatTime="0.03125" />',
         f'<WarpMarker Id="88" SecTime="{_num(start_s + beat_s * _SECOND_MARKER_BEATS)}"'
         f' BeatTime="{_num(_SECOND_MARKER_BEATS)}" />'),
    )
    text = template_text()
    for old, new in subs:
        text = _replace_once(text, old, new)
    return text


def write_alc(
    target: Path | str,
    wav_path: Path | str,
    slice_start_s: float,
    slice_end_s: float,
    *,
    frames: int,
    rate: int,
) -> Path:
    """Write the gzipped `.alc` at `target` for the slice
    `[slice_start_s, slice_end_s]` of the WAV at `wav_path`, which holds
    `frames` frames at `rate` Hz. Returns `target`."""
    target, wav_path = Path(target), Path(wav_path)
    xml = render_alc_xml(
        wav_path=wav_path.resolve(),
        relative_path=wav_path.name,
        start_s=float(slice_start_s),
        end_s=float(slice_end_s),
        frames=int(frames),
        rate=int(rate),
        size=wav_path.stat().st_size,
    )
    with gzip.open(target, "wb") as fh:
        fh.write(xml.encode("utf-8"))
    return target
