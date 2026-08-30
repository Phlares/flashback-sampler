"""Hardware playback tests: the real default render endpoint. Run by hand:
    pytest tests/hw -m audio_hw -s
You should hear a 1 s 440 Hz tone twice (48 kHz clip, then 96 kHz clip)."""
import time

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.scrub_player import NativeScrubPlayer

pytestmark = pytest.mark.audio_hw


@pytest.fixture(scope="module")
def lib():
    if native.load() is None:
        pytest.skip("flashback_core not built")
    return native.load()


def _tone(rate: int, seconds: float = 1.0, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    mono = (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    return np.stack([mono, mono], axis=1)


def test_list_devices_has_a_default_render(lib):
    devs = native.list_devices()
    assert any(d["kind"] == "render" and d["is_default"] for d in devs), devs


@pytest.mark.parametrize("rate", [48_000, 96_000])
def test_tone_plays_cursor_advances_and_playing_drops(lib, rate):
    """96 kHz is the AUTOCONVERTPCM measurement the spec asks for: the
    stream opens at the clip's rate and the engine resamples."""
    p = NativeScrubPlayer(rate, 2)
    clip = _tone(rate)
    p.bind(clip, rate)
    p.play()
    time.sleep(0.4)
    mid = p.cursor_samples
    playing_mid = p.is_playing
    err = p.last_error()
    time.sleep(1.0)
    end = p.cursor_samples
    playing_end = p.is_playing
    mix_rate = p._state().mix_rate
    p.close()
    assert err is None, err
    assert 0 < mid < len(clip), mid
    assert playing_mid
    assert end == len(clip), end
    assert not playing_end
    print(f"{rate} Hz: mid={mid} end={end} bind_rate={rate} mix_rate={mix_rate}")
