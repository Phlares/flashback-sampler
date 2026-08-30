"""NativeCaptureSource + native.list_devices over a FAKE ctypes library.
No hardware, no DLL: every fb_* symbol is a Python stub recording calls."""
import ctypes as C

import pytest

from flashback_sampler.core import native
from flashback_sampler.core.native_capture import NativeCaptureSource, NativeMixedSource


class _FakeLib:
    """Records calls; behaves like the real exports."""

    def __init__(self):
        self.calls = []
        self.started = False
        self.stats = (0, 0, 0, 48_000)  # running, frames, xruns, mix_rate
        self.err = b""
        self.devices = []
        self.ring_status = 0
        self.mixer_create_fails = False

    def __getattr__(self, name):  # argtypes/restype assignment is a no-op
        def _fn(*a):
            self.calls.append((name, a))
            if name == "fb_capture_create":
                return 0xC0FFEE
            if name == "fb_mixer_create":
                return 0 if self.mixer_create_fails else 0xA1CE
            if name in ("fb_capture_start", "fb_mixer_start"):
                self.started = True
                self.stats = (1,) + self.stats[1:]
                return 0
            if name in ("fb_capture_stop", "fb_mixer_stop"):
                self.started = False
                self.stats = (0,) + self.stats[1:]
            if name in ("fb_capture_stats", "fb_mixer_stats"):
                st = a[1]._obj if hasattr(a[1], "_obj") else a[1]
                st.running, st.frames_written, st.xruns, st.mix_rate = self.stats
            if name in ("fb_capture_last_error", "fb_mixer_last_error"):
                return self.err
            if name == "fb_devices_list":
                arr, mx = a
                n = min(len(self.devices), mx)
                for i, d in enumerate(self.devices[:n]):
                    arr[i].kind, arr[i].is_default, arr[i].mix_rate, arr[i].mix_channels = d[:4]
                    arr[i].id, arr[i].name = d[4].encode(), d[5].encode()
                return n
            if name == "fb_ring_create":
                a[3]._obj.value = self.ring_status  # byref(status) -> the c_int
                return 0 if self.ring_status else 0xA11
            return None
        return _fn


class _FakeBuffer:
    _h = 0xB0B
    channels = 2
    sample_rate = 48_000


@pytest.fixture
def lib(monkeypatch):
    fake = _FakeLib()
    monkeypatch.setattr(native, "_lib", fake)
    monkeypatch.setattr(native, "_lib_tried", True)
    return fake


def test_conforms_to_capture_source(lib):
    from flashback_sampler.core.capture_source import CaptureSource
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    assert isinstance(src, CaptureSource)
    assert src.sample_rate == 48_000 and src.channels == 2


def test_rejects_non_native_buffer(lib):
    with pytest.raises(TypeError):
        NativeCaptureSource(object(), kind="loopback")


def test_rejects_unknown_kind(lib):
    with pytest.raises(ValueError):
        NativeCaptureSource(_FakeBuffer(), kind="telepathy")


def test_create_passes_spec_fields(lib):
    NativeCaptureSource(_FakeBuffer(), kind="process", device_id="{dev}", pid=77, sample_rate=44_100, channels=1)
    name, args = next(c for c in lib.calls if c[0] == "fb_capture_create")
    spec = args[1]._obj if hasattr(args[1], "_obj") else args[1]
    assert (spec.kind, spec.pid, spec.rate, spec.channels, spec.device_id) == (2, 77, 44_100, 1, b"{dev}")


def test_start_stop_round_trip_and_running(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="input", device_id="x")
    assert not src.is_running()
    src.start()
    assert src.is_running()
    src.start()  # idempotent — no second fb_capture_start
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_start") == 1
    src.stop()
    src.stop()
    assert not src.is_running()
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_stop") == 1


def test_stats_and_last_error_surface(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    lib.stats = (0, 12_345, 3, 44_100)
    assert src.frames_written() == 12_345
    assert src.xrun_count() == 3
    assert src.mix_rate() == 44_100
    assert src.last_error() is None
    lib.err = b"open failed: DeviceNotFound"
    assert src.last_error() == "open failed: DeviceNotFound"


def test_close_destroys_once(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    src.close()
    src.close()
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_destroy") == 1


def test_list_devices_maps_kinds_and_strings(lib):
    lib.devices = [(0, 1, 48_000, 2, "{id-a}", "Speakers"), (1, 0, 44_100, 1, "{id-b}", "Mic")]
    got = native.list_devices()
    assert got == [
        {"kind": "loopback", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{id-a}", "name": "Speakers"},
        {"kind": "input", "is_default": False, "mix_rate": 44_100, "mix_channels": 1, "id": "{id-b}", "name": "Mic"},
    ]


def test_list_devices_maps_render_kind(lib):
    lib.devices = [(3, 1, 48_000, 2, "{spk}", "Speakers")]
    got = native.list_devices()
    assert got == [{"kind": "render", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{spk}", "name": "Speakers"}]


def test_queries_are_inert_after_close(lib):
    """A closed handle is NULL on the Zig side; fb_capture_* exports take a
    non-optional *Capture, so passing NULL through is undefined behavior
    (an access violation), not a Python exception. Every query must go
    inert instead of reaching the lib once closed."""
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    src.close()
    n_calls_at_close = len(lib.calls)

    assert src.is_running() is False
    assert src.xrun_count() == 0
    assert src.frames_written() == 0
    assert src.mix_rate() == 0
    assert src.last_error() is None
    src.stop()  # no-op, must not touch the lib

    assert len(lib.calls) == n_calls_at_close, "a query reached the lib after close()"


def test_start_after_close_raises(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    src.close()
    with pytest.raises(RuntimeError):
        src.start()


def test_start_close_stop_never_calls_fb_capture_stop_with_null_handle(lib):
    """close() must reset _started too -- otherwise a stale _started=True
    survives close() and a later stop() reaches fb_capture_stop with the
    now-NULL handle."""
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    src.start()
    src.close()
    src.stop()
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_stop") == 0


def test_ring_create_out_of_memory_raises_memory_error_with_the_byte_count(lib):
    lib.ring_status = native._OUT_OF_MEMORY
    with pytest.raises(MemoryError) as e:
        native.NativeAudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=2)
    assert "16,000 bytes" in str(e.value)  # 2 s * 1000 Hz * 2 ch * 4 B


def test_ring_create_invalid_arg_raises_value_error(lib):
    lib.ring_status = native._INVALID_ARG
    with pytest.raises(ValueError):
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=3)


def test_ring_create_passes_a_status_out_param(lib):
    lib.ring_status = native._OUT_OF_MEMORY
    with pytest.raises(MemoryError):
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    name, args = next(c for c in lib.calls if c[0] == "fb_ring_create")
    assert len(args) == 4 and hasattr(args[3], "_obj")


def test_mixed_conforms_to_capture_source(lib):
    from flashback_sampler.core.capture_source import CaptureSource
    src = NativeMixedSource(_FakeBuffer(), specs=[{"kind": "loopback"}, {"kind": "input", "device_id": "{mic}"}])
    assert isinstance(src, CaptureSource)
    assert src.sample_rate == 48_000 and src.channels == 2


def test_mixed_rejects_non_native_buffer(lib):
    with pytest.raises(TypeError):
        NativeMixedSource(object(), specs=[{"kind": "loopback"}])


def test_mixed_rejects_unknown_kind(lib):
    with pytest.raises(ValueError):
        NativeMixedSource(_FakeBuffer(), specs=[{"kind": "telepathy"}])


def test_mixed_create_passes_every_spec(lib):
    NativeMixedSource(
        _FakeBuffer(),
        specs=[{"kind": "loopback", "device_id": "{spk}"}, {"kind": "process", "pid": 77}],
        sample_rate=44_100, channels=1,
    )
    name, args = next(c for c in lib.calls if c[0] == "fb_mixer_create")
    ring, arr, n = args
    assert ring == _FakeBuffer._h and n == 2
    assert (arr[0].kind, arr[0].pid, arr[0].rate, arr[0].channels, arr[0].device_id) == (0, 0, 44_100, 1, b"{spk}")
    assert (arr[1].kind, arr[1].pid, arr[1].rate, arr[1].channels, arr[1].device_id) == (2, 77, 44_100, 1, b"")


def test_mixed_create_failure_raises(lib):
    lib.mixer_create_fails = True
    with pytest.raises(RuntimeError):
        NativeMixedSource(_FakeBuffer(), specs=[{"kind": "loopback"}])


def test_mixed_start_stop_stats_and_close_are_inert_after_close(lib):
    src = NativeMixedSource(_FakeBuffer(), specs=[{"kind": "loopback"}, {"kind": "input"}])
    assert not src.is_running()
    src.start(); src.start()
    assert src.is_running()
    assert sum(1 for c in lib.calls if c[0] == "fb_mixer_start") == 1
    lib.stats = (1, 999, 4, 44_100)
    assert src.frames_written() == 999 and src.xrun_count() == 4 and src.mix_rate() == 44_100
    lib.err = b"source start failed: AlreadyRunning"
    assert src.last_error() == "source start failed: AlreadyRunning"
    src.stop(); src.stop()
    assert sum(1 for c in lib.calls if c[0] == "fb_mixer_stop") == 1
    src.close(); src.close()
    assert sum(1 for c in lib.calls if c[0] == "fb_mixer_destroy") == 1
    # Inert after close: no ABI call reaches a NULL handle.
    before = len(lib.calls)
    assert src.is_running() is False and src.xrun_count() == 0 and src.last_error() is None
    src.stop()
    with pytest.raises(RuntimeError):
        src.start()
    assert len(lib.calls) == before
