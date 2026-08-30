"""NativeScrubPlayer over a FAKE ctypes library. No DLL, no device: every
fb_playback_* symbol is a Python stub that records calls and serves a
scripted state. The fill logic lives in Zig (core/src/Playback.zig) and
is tested there."""
import ctypes as C

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.scrub_player import NativeScrubPlayer


class _FakePlaybackLib:
    def __init__(self):
        self.calls = []
        self.state = (0, 0, 0, 0, 0)  # running, playing, cursor, clip_frames, mix_rate
        self.bind_status = 0
        self.play_status = 0
        self.err = b""
        self.bound = None  # (frames ndarray copy, n_frames, rate, channels)

    def __getattr__(self, name):
        def _fn(*a):
            self.calls.append((name, a))
            if name == "fb_playback_create":
                return 0xF00D
            if name == "fb_playback_bind":
                _h, ptr, n, rate, ch = a
                assert _h is not None, "fb_playback_bind called with a closed/None handle"
                arr = np.ctypeslib.as_array(ptr, shape=(n * ch,)).copy() if n else np.zeros(0, np.float32)
                self.bound = (arr, n, rate, ch)
                if self.bind_status == 0:
                    self.state = self.state[:3] + (n, self.state[4])
                return self.bind_status
            if name == "fb_playback_play":
                if self.play_status == 0:
                    self.state = (1, 1) + self.state[2:]  # a real play() spawns and sets playing
                return self.play_status
            if name == "fb_playback_state":
                st = a[1]._obj if hasattr(a[1], "_obj") else a[1]
                st.running, st.playing, st.cursor, st.clip_frames, st.mix_rate = self.state
            if name == "fb_playback_last_error":
                return self.err
            return None
        return _fn


@pytest.fixture
def lib(monkeypatch):
    fake = _FakePlaybackLib()
    monkeypatch.setattr(native, "_lib", fake)
    monkeypatch.setattr(native, "_lib_tried", True)
    return fake


def _calls(lib, name):
    return [a for n, a in lib.calls if n == name]


def test_create_is_lazy_and_first_bind_passes_rate_channels_and_device(lib):
    p = NativeScrubPlayer(44_100, 1, device="{hp}")
    assert not _calls(lib, "fb_playback_create")
    p.bind(np.zeros(4, dtype=np.float32), 44_100)
    assert _calls(lib, "fb_playback_create") == [(b"{hp}", 44_100, 1)]
    p.bind(np.zeros(4, dtype=np.float32), 44_100)
    assert len(_calls(lib, "fb_playback_create")) == 1  # created once, reused


def test_construct_without_library_is_silent_and_the_first_native_call_raises(monkeypatch):
    """Zig-less workstations stay green (tests/conftest.py): AppState
    builds a player at startup, so construction must not touch the
    native library. Only a call that needs the handle may raise."""
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", True)
    p = NativeScrubPlayer()
    with pytest.raises(RuntimeError):
        p.bind(np.zeros(4, dtype=np.float32), 48_000)
    with pytest.raises(RuntimeError):
        p.play()


def test_bind_passes_frames_rate_channels_and_updates_attributes(lib):
    p = NativeScrubPlayer(48_000, 2)
    audio = np.arange(6, dtype=np.float32).reshape(3, 2)
    p.bind(audio, 96_000)
    arr, n, rate, ch = lib.bound
    assert (n, rate, ch) == (3, 96_000, 2)
    np.testing.assert_array_equal(arr, audio.ravel())
    assert p.sample_rate == 96_000 and p.channels == 2
    assert p.source_length_samples == 3


def test_bind_reshapes_mono_1d_to_one_channel(lib):
    p = NativeScrubPlayer(48_000, 2)
    p.bind(np.zeros(4, dtype=np.float32), 48_000)
    assert lib.bound[1:] == (4, 48_000, 1)
    assert p.channels == 1


def test_bind_status_invalid_arg_raises_value_error(lib):
    lib.bind_status = native._INVALID_ARG
    with pytest.raises(ValueError):
        NativeScrubPlayer().bind(np.zeros((2, 3), dtype=np.float32), 48_000)


def test_bind_status_out_of_memory_raises_memory_error(lib):
    lib.bind_status = native._OUT_OF_MEMORY
    with pytest.raises(MemoryError):
        NativeScrubPlayer().bind(np.zeros((2, 2), dtype=np.float32), 48_000)


def test_play_pause_forward_and_play_failure_raises(lib):
    p = NativeScrubPlayer()
    p.play()
    p.pause()
    assert [n for n, _ in lib.calls[-2:]] == ["fb_playback_play", "fb_playback_pause"]
    lib.play_status = native._IO_ERROR
    lib.err = b"open failed: DeviceNotFound"
    with pytest.raises(RuntimeError, match="DeviceNotFound"):
        p.play()


def test_stop_is_pause_then_seek_zero(lib):
    p = NativeScrubPlayer()
    p.play()  # materialize the handle; pause/seek are inert without one
    p.stop()
    assert [(n, a[1:]) for n, a in lib.calls[-2:]] == [("fb_playback_pause", ()), ("fb_playback_seek", (0,))]


def test_seek_samples_clamps_negative_to_zero_and_passes_through(lib):
    p = NativeScrubPlayer()
    p.play()  # materialize the handle
    p.seek_samples(-5)
    p.seek_samples(123)
    assert [a[1] for a in _calls(lib, "fb_playback_seek")] == [0, 123]


def test_seek_seconds_uses_the_bound_rate(lib):
    p = NativeScrubPlayer(48_000, 2)
    p.bind(np.zeros((10, 2), dtype=np.float32), 1_000)
    p.seek(0.25)
    assert _calls(lib, "fb_playback_seek")[-1][1] == 250


def test_state_properties_read_native_state(lib):
    p = NativeScrubPlayer(48_000, 2)
    p.bind(np.zeros((500, 2), dtype=np.float32), 1_000)
    lib.state = (1, 1, 250, 500, 48_000)
    assert p.is_playing is True
    assert p.cursor_samples == 250
    assert p.cursor_seconds == 0.25
    assert p.source_length_samples == 500
    lib.state = (1, 0, 500, 500, 48_000)
    assert p.is_playing is False


def test_set_device_before_the_handle_reaches_create_and_after_it_forwards(lib):
    p = NativeScrubPlayer()
    p.set_device("{spk}")  # AppState does this at startup, before any play
    assert p.device == "{spk}"
    assert not _calls(lib, "fb_playback_set_device")
    p.play()
    assert _calls(lib, "fb_playback_create") == [(b"{spk}", 48_000, 2)]
    p.set_device("{hp}")
    assert _calls(lib, "fb_playback_set_device") == [(0xF00D, b"{hp}")]


def test_last_error_none_when_empty(lib):
    p = NativeScrubPlayer()
    p.play()  # materialize the handle
    assert p.last_error() is None
    lib.err = b"stream failed: ActivationFailed"
    assert p.last_error() == "stream failed: ActivationFailed"


def test_close_destroys_once_and_is_inert_after(lib):
    p = NativeScrubPlayer()
    p.play()  # materialize the handle
    p.close()
    p.close()
    assert len(_calls(lib, "fb_playback_destroy")) == 1
    p.pause()  # inert, no call
    assert not _calls(lib, "fb_playback_pause")
    assert p.is_playing is False and p.cursor_samples == 0


def test_bind_after_close_is_inert_and_never_recreates_the_handle(lib):
    p = NativeScrubPlayer()
    p.play()  # materialize the handle
    p.close()
    p.bind(np.zeros((4, 1), dtype=np.float32), 48_000)  # neither crashes nor reaches the fake
    assert not _calls(lib, "fb_playback_bind")
    # Lazy creation must not resurrect a closed player.
    assert len(_calls(lib, "fb_playback_create")) == 1
