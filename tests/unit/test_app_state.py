"""
Unit tests for AppState — the headless object graph root.

These tests do not import PySide6; AppState is a plain Python container
that owns the buffer, checkout manager, and scrub player.
"""

from __future__ import annotations

import numpy as np
import pytest

from flashback_sampler.app.state import AppState
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.native import NativeAudioCircularBuffer
from flashback_sampler.core.scrub_player import NativeScrubPlayer


def test_core_package_exports_nothing_from_a_python_buffer():
    import flashback_sampler.core as core
    assert not hasattr(core, "AudioCircularBuffer")


def test_appstate_wires_core_objects_with_matching_sample_rate_and_channels():
    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=2)
    assert isinstance(st.buffer, NativeAudioCircularBuffer)
    assert isinstance(st.checkout_manager, CheckoutManager)
    assert isinstance(st.scrub_player, NativeScrubPlayer)
    assert st.sample_rate == 16_000
    assert st.channels == 2
    assert st.buffer.sample_rate == 16_000
    assert st.buffer.channels == 2
    assert st.scrub_player.sample_rate == 16_000
    assert st.scrub_player.channels == 2


def test_appstate_default_buffer_seconds_matches_5min_launch_default():
    from flashback_sampler.app.state import DEFAULT_BUFFER_SECONDS

    assert DEFAULT_BUFFER_SECONDS == 5 * 60
    st = AppState(sample_rate=1000, channels=1)  # buffer_seconds omitted
    assert st.buffer.duration == 300.0


# ─────────────────────────────────────────────────────────────────────────
# Multi-slot AppState (M10.4)
# ─────────────────────────────────────────────────────────────────────────


def test_appstate_starts_with_exactly_one_slot():
    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=1)
    assert len(st.slots) == 1
    assert st.active_slot_index == 0
    assert st.active_slot is st.slots[0]


def test_initial_slot_uses_constructor_args():
    st = AppState(buffer_seconds=7.5, sample_rate=22_050, channels=1)
    slot = st.active_slot
    assert slot.sample_rate == 22_050
    assert slot.channels == 1
    assert slot.buffer_seconds == 7.5
    assert slot.quality_preset == "CUSTOM"
    assert slot.name == "Main"


def test_backward_compat_properties_delegate_to_active_slot():
    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=1)
    assert st.buffer is st.active_slot.buffer
    assert st.checkout_manager is st.active_slot.checkout_manager
    assert st.sample_rate == st.active_slot.sample_rate
    assert st.channels == st.active_slot.channels


def test_add_slot_appends_and_does_not_change_active():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=1)
    new_slot = st.add_slot(preset_by_name("SCRATCH"), name="Discord")
    assert len(st.slots) == 2
    assert st.slots[1] is new_slot
    assert new_slot.name == "Discord"
    # Active slot unchanged
    assert st.active_slot_index == 0


def test_set_active_slot_index_rotates_backward_compat_properties():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=1)
    slot_b = st.add_slot(preset_by_name("SCRATCH"), name="Discord")

    # Before switch: properties point at slot 0 (rate 16_000)
    assert st.sample_rate == 16_000
    assert st.buffer is st.slots[0].buffer

    st.set_active_slot_index(1)
    assert st.active_slot is slot_b
    assert st.sample_rate == 16_000  # SCRATCH is also 16k
    # But the underlying buffer is a different instance now
    assert st.buffer is slot_b.buffer
    assert st.checkout_manager is slot_b.checkout_manager


def test_set_active_slot_out_of_range_raises():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    with pytest.raises(IndexError):
        st.set_active_slot_index(5)
    with pytest.raises(IndexError):
        st.set_active_slot_index(-1)


def test_remove_slot_adjusts_active_index():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))
    st.add_slot(preset_by_name("CHAT"))
    assert len(st.slots) == 3

    st.set_active_slot_index(2)
    st.remove_slot(0)  # removing a slot before the active index
    assert len(st.slots) == 2
    assert st.active_slot_index == 1  # shifted down


def test_remove_last_slot_raises():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    with pytest.raises(RuntimeError, match="cannot remove the last slot"):
        st.remove_slot(0)


def test_remove_slot_stops_capture():
    from flashback_sampler.core.quality_presets import preset_by_name
    from tests.fixtures.fake_capture import FakeCaptureSourceNoThread

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    new_slot = st.add_slot(preset_by_name("SCRATCH"))
    src = FakeCaptureSourceNoThread(
        new_slot.buffer,
        sample_rate=new_slot.sample_rate,
        channels=new_slot.channels,
    )
    new_slot.bind_capture(src)
    new_slot.start_capture()
    assert new_slot.is_capturing() is True

    st.remove_slot(1)
    assert src.is_running() is False
    assert len(st.slots) == 1


def test_remove_slot_closes_buffer():
    from flashback_sampler.core.quality_presets import preset_by_name

    class _RecordingBuffer:
        def __init__(self):
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))
    survivor_buffer = _RecordingBuffer()
    removed_buffer = _RecordingBuffer()
    st.slots[0].buffer = survivor_buffer
    st.slots[1].buffer = removed_buffer

    st.remove_slot(1)

    assert removed_buffer.close_calls == 1
    assert survivor_buffer.close_calls == 0


def test_remove_slot_stop_precedes_close():
    from flashback_sampler.core.quality_presets import preset_by_name

    call_order: list[str] = []

    class _RecordingSource:
        def stop(self) -> None:
            call_order.append("stop")

        def is_running(self) -> bool:
            return False

        def xrun_count(self) -> int:
            return 0

        def last_error(self):
            return None

    class _RecordingBuffer:
        def close(self) -> None:
            call_order.append("close")

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    new_slot = st.add_slot(preset_by_name("SCRATCH"))
    new_slot.bind_capture(_RecordingSource())
    new_slot.buffer = _RecordingBuffer()

    st.remove_slot(1)

    assert call_order == ["stop", "close"]


def test_rebuild_buffer_scoped_to_active_slot():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))  # slot 1
    assert len(st.slots) == 2

    # Active is still slot 0
    orig_slot1_buf = st.slots[1].buffer
    st.rebuild_buffer(3.0)
    assert st.slots[0].buffer.duration == 3.0
    # Slot 1 untouched
    assert st.slots[1].buffer is orig_slot1_buf


def test_apply_checkout_caps_scoped_to_active_slot():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))

    st.apply_checkout_caps(max_active=3)
    # Active slot (0) updated
    assert st.slots[0].checkout_manager._max_active == 3
    # Slot 1 left at the default 16
    assert st.slots[1].checkout_manager._max_active == 16


def test_total_project_ram_bytes_counts_every_slot():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=5.0, sample_rate=48_000, channels=2)
    # Main slot ~1.92 MB (5s * 48k * 2 * 4). capacity_bytes, not
    # .buffer.nbytes -- the latter is the raw storage array, which is
    # larger than the readable window by a guard band (see native.py's
    # capacity_bytes docstring).
    main_bytes = st.slots[0].buffer.capacity_bytes
    assert main_bytes == 5 * 48_000 * 2 * 4

    st.add_slot(preset_by_name("SCRATCH"))  # +~11 MB
    total = st.total_project_ram_bytes()
    assert total > main_bytes
    # SCRATCH = 16k mono 180s = 16000 * 180 * 4 = 11_520_000
    expected = main_bytes + 11_520_000
    assert total == expected


def test_total_project_ram_includes_checkouts():
    # R-h9a: a 0 MB budget (the unit default) makes resident_bytes racy --
    # the writer can evict the instant the write lands. A budget big
    # enough to hold the whole clip makes the number deterministic.
    st = AppState(buffer_seconds=2.0, sample_rate=1000, channels=1, checkout_cache_mb=1.0)
    buf_bytes = st.slots[0].buffer.capacity_bytes
    assert st.total_project_ram_bytes() == buf_bytes
    # Write some audio and create a checkout
    st.slots[0].buffer.write(
        np.zeros((1000, 1), dtype=np.float32)  # 1 s
    )
    co = st.slots[0].checkout_manager.create(duration_s=0.5)
    # The checkout's RAM copy lives in the scratch cache, counted once per process.
    assert st.total_project_ram_bytes() == buf_bytes + st.scratch.resident_bytes
    assert st.scratch.resident_bytes == 500 * 4


def test_add_slot_rejects_when_over_the_max_footprint():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=5.0, sample_rate=48_000, channels=2)
    # A footprint that only fits the initial slot
    current = st.total_project_ram_mb()
    st.set_max_footprint_mb(current + 1)  # tiny headroom

    with pytest.raises(RuntimeError, match="Max footprint") as info:
        st.add_slot(preset_by_name("FULL"))
    # The message carries both numbers the user needs to act.
    assert f"{current + 1:.0f} MB" in str(info.value)


def test_add_slot_succeeds_within_the_max_footprint():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_max_footprint_mb(4096.0)
    slot = st.add_slot(preset_by_name("CHAT"))
    assert slot is not None
    assert len(st.slots) == 2


def test_max_footprint_zero_means_uncapped(monkeypatch):
    from flashback_sampler.core.quality_presets import preset_by_name
    import flashback_sampler.app.state as state_mod

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_max_footprint_mb(0)
    assert st.max_footprint_mb == 0.0
    # Plenty of free memory reported: only the cap could refuse, and there is none.
    monkeypatch.setattr(state_mod.native, "mem_info", lambda: (1 << 40, 1 << 40))
    assert st.add_slot(preset_by_name("FULL")) is not None


def test_max_footprint_negative_floors_to_uncapped():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_max_footprint_mb(-10)
    assert st.max_footprint_mb == 0.0


def test_add_slot_rejects_a_ring_larger_than_free_physical_memory(monkeypatch):
    from flashback_sampler.core.quality_presets import preset_by_name
    import flashback_sampler.app.state as state_mod

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_max_footprint_mb(0)  # uncapped: only the free-memory clause can refuse
    monkeypatch.setattr(state_mod.native, "mem_info", lambda: (1 << 40, 1024 * 1024))  # 1 MB free
    with pytest.raises(RuntimeError, match="free") as info:
        st.add_slot(preset_by_name("FULL"))
    assert "1 MB" in str(info.value)


def test_free_memory_clause_is_skipped_when_the_platform_cannot_say(monkeypatch):
    from flashback_sampler.core.quality_presets import preset_by_name
    import flashback_sampler.app.state as state_mod

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_max_footprint_mb(0)
    monkeypatch.setattr(state_mod.native, "mem_info", lambda: (0, 0))
    assert st.add_slot(preset_by_name("CHAT")) is not None


def test_default_max_footprint_is_a_quarter_of_physical_ram(monkeypatch, tmp_path):
    import flashback_sampler.app.state as state_mod
    import flashback_sampler.app.config as cfg

    monkeypatch.setattr(state_mod.native, "mem_info", lambda: (64 * 1024 ** 3, 32 * 1024 ** 3))
    monkeypatch.setattr(cfg, "load_max_footprint_mb", lambda *a, **k: k["default"])  # unset pref
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert st.max_footprint_mb == 16384.0
    st.shutdown()


def test_effective_capture_spec_prefers_slot_override():
    from flashback_sampler.app.audio_devices import CaptureDevice
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    slot_a = st.slots[0]
    slot_b = st.add_slot(preset_by_name("SCRATCH"))

    global_dev = CaptureDevice(kind="loopback", name="GlobalSpeaker", id="gs")
    override_dev = CaptureDevice(kind="input", name="MicA", id="7")
    st.capture_spec = global_dev
    slot_b.capture_spec = override_dev

    # slot_a inherits from the global
    assert st.effective_capture_spec_for_slot(slot_a) is global_dev
    # slot_b uses its own override
    assert st.effective_capture_spec_for_slot(slot_b) is override_dev


def test_effective_capture_spec_falls_through_when_slot_has_none():
    from flashback_sampler.app.audio_devices import CaptureDevice

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    slot = st.slots[0]
    assert slot.capture_spec is None
    global_dev = CaptureDevice(kind="loopback", name="X", id="x")
    st.capture_spec = global_dev
    assert st.effective_capture_spec_for_slot(slot) is global_dev


def test_add_slot_carries_capture_spec_override():
    from flashback_sampler.app.audio_devices import CaptureDevice
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    dev = CaptureDevice(kind="input", name="MicB", id="9")
    slot = st.add_slot(preset_by_name("SCRATCH"), capture_spec=dev)
    assert slot.capture_spec is dev


def test_build_capture_for_slot_uses_override_path():
    """
    Verify the delegation without touching real hardware: swap out
    build_capture_source with a stub that records which spec it saw.
    """
    from flashback_sampler.app.audio_devices import CaptureDevice
    from flashback_sampler.core.quality_presets import preset_by_name

    import flashback_sampler.app.state as state_mod
    captured_device = {}
    def fake_build(device, buffer, sample_rate, channels):
        captured_device["d"] = device
        return object()
    real = state_mod.build_capture_source
    state_mod.build_capture_source = fake_build
    try:
        st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
        global_dev = CaptureDevice(kind="loopback", name="G", id="g")
        override_dev = CaptureDevice(kind="input", name="O", id="11")
        st.capture_spec = global_dev
        slot = st.add_slot(preset_by_name("SCRATCH"), capture_spec=override_dev)
        st.build_capture_for_slot(slot)
        assert captured_device["d"] is override_dev

        # Remove override and check the global takes over
        slot.capture_spec = None
        st.build_capture_for_slot(slot)
        assert captured_device["d"] is global_dev
    finally:
        state_mod.build_capture_source = real


def test_shutdown_stops_all_slots():
    from flashback_sampler.core.quality_presets import preset_by_name
    from tests.fixtures.fake_capture import FakeCaptureSourceNoThread

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))
    for slot in st.slots:
        src = FakeCaptureSourceNoThread(
            slot.buffer, sample_rate=slot.sample_rate, channels=slot.channels
        )
        slot.bind_capture(src)
        slot.start_capture()

    st.shutdown()
    for slot in st.slots:
        assert slot.is_capturing() is False


def test_appstate_is_not_capturing_initially():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    assert st.is_capturing() is False
    assert st.capture is None


def test_shutdown_is_idempotent_without_capture_or_stream():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.shutdown()  # must not raise


def test_checkout_from_live_buffer_then_bind_to_scrub_player(monkeypatch):
    """
    End-to-end headless: push audio into the buffer, create a checkout,
    bind it to the scrub player, and verify the callback plays it back.
    This is the core P1 path exercised without any Qt or audio hardware.
    """
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    # Write a ramp so we can assert exact sample positions
    ramp = np.arange(500, dtype=np.float32).reshape(-1, 1) / 500.0  # [0, 1)
    st.buffer.write(ramp)
    co = st.checkout_manager.create(duration_s=0.5)
    assert co.n_frames == 500

    seen = {}
    # The player's handle is lazy, so a fake library serves the whole
    # native side: no DLL, no device, and no real handle to free.
    from flashback_sampler.core import native

    fake_lib = type("L", (), {
        "fb_playback_create": staticmethod(lambda d, r, c: 0xF00D),
        "fb_playback_bind_checkout": staticmethod(lambda h, s, co, start, n: seen.update(start=start, n=n) or 0),
        "fb_playback_play": staticmethod(lambda h: seen.update(played=True) or 0),
        "fb_playback_destroy": staticmethod(lambda h: None),
    })()
    monkeypatch.setattr(native, "load", lambda: fake_lib)
    st.scrub_player.bind_checkout(st.scratch, co.handle, 0, co.n_frames, co.sample_rate, co.channels)
    st.scrub_player.play()
    assert seen == dict(start=0, n=500, played=True)


# ─────────────────────────────────────────────────────────────────────────
# CLI argument parsing
# ─────────────────────────────────────────────────────────────────────────


def test_cli_defaults():
    from flashback_sampler.app.main import _parse_args

    args = _parse_args([])
    assert args.buffer_minutes == 5.0
    assert args.sample_rate == 48_000
    assert args.channels == 2


def test_cli_custom_buffer_for_rollover_testing():
    from flashback_sampler.app.main import _parse_args

    args = _parse_args(["--buffer-minutes", "0.5"])
    assert args.buffer_minutes == 0.5
    # AppState constructor accepts the computed seconds
    st = AppState(
        buffer_seconds=args.buffer_minutes * 60,
        sample_rate=args.sample_rate,
        channels=args.channels,
    )
    assert st.buffer.duration == 30.0


def test_cli_mono_override():
    from flashback_sampler.app.main import _parse_args

    args = _parse_args(["--channels", "1", "--sample-rate", "16000"])
    assert args.channels == 1
    assert args.sample_rate == 16_000


def test_rebuild_buffer_closes_the_old_buffer():
    """rebuild_buffer discards the active slot's ring buffer and builds a
    fresh NativeAudioCircularBuffer -- the OLD buffer's close() must be
    called so its Zig-owned handle is released deterministically instead
    of relying only on eventual GC/__del__."""
    from flashback_sampler.app.state import AppState
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    try:
        old_buf = st.active_slot.buffer
        calls = []
        old_buf.close = lambda: calls.append(True)
        st.rebuild_buffer(2.0)
        assert calls, "rebuild_buffer did not close the old buffer"
        assert st.active_slot.buffer is not old_buf
    finally:
        st.shutdown()


def test_rebuild_buffer_preserves_record_gain():
    """Changing buffer duration must not silently reset a source's gain/mute."""
    from flashback_sampler.app.state import AppState
    s = AppState(buffer_seconds=2.0, sample_rate=1000, channels=1)
    try:
        s.active_slot.buffer.gain_db = -6.0
        prev = s.active_slot.buffer.gain
        s.rebuild_buffer(1.0)
        assert abs(s.active_slot.buffer.gain - prev) < 1e-9
    finally:
        s.shutdown()


def test_build_capture_for_slot_routes_two_specs_to_the_mixer():
    """Two or more capture_specs go to build_mixed_capture_source with the
    devices themselves — no factories, no staging buffers in Python."""
    from flashback_sampler.app.audio_devices import CaptureDevice
    from flashback_sampler.core.quality_presets import preset_by_name

    import flashback_sampler.app.state as state_mod
    seen = {}

    def fake_mixed(devices, buffer, sample_rate, channels):
        seen.update(devices=list(devices), buffer=buffer, sample_rate=sample_rate, channels=channels)
        return object()

    def fake_single(device, buffer, sample_rate, channels):
        raise AssertionError("single-source builder must not run for two specs")

    real_mixed, real_single = state_mod.build_mixed_capture_source, state_mod.build_capture_source
    state_mod.build_mixed_capture_source, state_mod.build_capture_source = fake_mixed, fake_single
    try:
        st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
        slot = st.add_slot(preset_by_name("SCRATCH"))
        d1 = CaptureDevice(kind="loopback", name="A", id="a")
        d2 = CaptureDevice(kind="input", name="B", id="b")
        slot.capture_specs = [d1, d2]
        st.build_capture_for_slot(slot)
        assert seen["devices"] == [d1, d2]
        assert seen["buffer"] is slot.buffer
        assert (seen["sample_rate"], seen["channels"]) == (slot.sample_rate, slot.channels)
    finally:
        state_mod.build_mixed_capture_source, state_mod.build_capture_source = real_mixed, real_single


# ─────────────────────────────────────────────────────────────────────────
# Scratch ownership, adoption at launch, RAM accounting over handles (h9)
# ─────────────────────────────────────────────────────────────────────────


def _written(st, co, timeout=5.0):
    import time
    t0 = time.monotonic()
    mgr = st.slots[0].checkout_manager
    while time.monotonic() - t0 < timeout:
        if mgr.write_state(co.id) == "written":
            return
        time.sleep(0.005)
    raise AssertionError("never written")


def test_state_uses_the_configured_scratch_dir_and_starts_the_writer(tmp_path):
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path / "s")
    assert st.scratch_dir == tmp_path / "s" and st.scratch_dir.is_dir()
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.1)
    _written(st, co)
    assert (tmp_path / "s" / f"{co.id}.wav").exists()
    assert st.scratch_dir_error is None
    st.shutdown()


def test_uncreatable_scratch_dir_falls_back_to_default_and_continues(tmp_path, monkeypatch):
    """F1: an uncreatable configured scratch_dir (bad drive, permission
    denied, stale removable-media path, ...) must not brick launch --
    AppState falls back to config.default_scratch_dir() and records what
    happened instead of raising out of __init__."""
    import flashback_sampler.app.state as state_mod
    from pathlib import Path as _Path

    bad_dir = tmp_path / "unwritable"
    fallback_dir = tmp_path / "fallback"
    monkeypatch.setattr(state_mod.app_config, "default_scratch_dir", lambda: fallback_dir)

    real_mkdir = _Path.mkdir

    def raising_mkdir(self, *a, **k):
        if self == bad_dir:
            raise OSError("permission denied (simulated)")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(_Path, "mkdir", raising_mkdir)

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=bad_dir)
    assert st.scratch_dir == fallback_dir and fallback_dir.is_dir()
    assert st.scratch_dir_error is not None and str(bad_dir) in st.scratch_dir_error
    # The fallback is fully usable -- a checkout can still be created and written.
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.1)
    _written(st, co)
    assert (fallback_dir / f"{co.id}.wav").exists()
    st.shutdown()


def test_adoption_restores_checkouts_into_a_matching_slot(tmp_path):
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.arange(1000, dtype=np.float32).reshape(-1, 1))
    co = st.checkout_manager.create(duration_s=0.5)
    st.checkout_manager.set_trim(co.id, 10, 20)
    st.checkout_manager.mark_saved(co.id)
    _written(st, co)
    st.shutdown()  # files stay

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    cos = st2.slots[0].checkout_manager.list()
    assert [c.id for c in cos] == [co.id]
    back = cos[0]
    assert (back.n_frames, back.trim_in_samples, back.trim_out_samples, back.state) == (500, 10, 20, "saved")
    assert back.bins["540"].shape == (540, 2, 1)
    assert len(st2.slots) == 1
    st2.shutdown()


def test_adoption_makes_an_unarmed_slot_for_a_foreign_rate(tmp_path):
    st = AppState(buffer_seconds=1.0, sample_rate=2000, channels=2, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((2000, 2), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.2)
    _written(st, co)
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert len(st2.slots) == 2
    adopted = st2.slots[1]
    assert (adopted.sample_rate, adopted.channels, adopted.armed) == (2000, 2, False)
    assert adopted.name == "Main"  # the manifest's slot name
    assert [c.id for c in adopted.checkout_manager.list()] == [co.id]
    st2.shutdown()


def test_adoption_does_not_match_a_slot_on_rate_alone(tmp_path):
    """R-h9c: same rate as the Main slot, DIFFERENT channels. Pins the
    channels half of `s.sample_rate == m.rate and s.channels == m.channels`
    -- a rate-only match would wrongly fold this into slot 0."""
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=2, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((1000, 2), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.2)
    _written(st, co)
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert len(st2.slots) == 2
    adopted = st2.slots[1]
    assert (adopted.sample_rate, adopted.channels, adopted.armed) == (1000, 2, False)
    assert [c.id for c in adopted.checkout_manager.list()] == [co.id]
    st2.shutdown()


def test_adoption_does_not_match_a_slot_on_channels_alone(tmp_path):
    """R-h9c: same channels as the Main slot, DIFFERENT rate. Pins the
    rate half of the same compound condition -- a channels-only match
    would wrongly fold this into slot 0."""
    st = AppState(buffer_seconds=1.0, sample_rate=2000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((2000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.2)
    _written(st, co)
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert len(st2.slots) == 2
    adopted = st2.slots[1]
    assert (adopted.sample_rate, adopted.channels, adopted.armed) == (2000, 1, False)
    assert [c.id for c in adopted.checkout_manager.list()] == [co.id]
    st2.shutdown()


def test_adoption_survives_add_slot_refusing_a_foreign_rate(tmp_path, monkeypatch):
    """R-h9b: add_slot raises RuntimeError on a project-RAM refusal.
    adopt_scratch must swallow that and skip the manifest -- a launch
    must never raise because of what's sitting on disk."""
    st = AppState(buffer_seconds=1.0, sample_rate=2000, channels=2, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((2000, 2), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.2)
    _written(st, co)
    st.shutdown()

    import flashback_sampler.app.state as state_mod

    def raising_add_slot(self, *a, **k):
        raise RuntimeError("Max footprint exceeded")

    monkeypatch.setattr(state_mod.AppState, "add_slot", raising_add_slot)
    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert len(st2.slots) == 1  # no slot could be created for the foreign rate
    assert st2.slots[0].checkout_manager.list() == []  # the manifest was skipped, not adopted
    st2.shutdown()


def test_adoption_survives_a_range_corrupt_manifest_and_still_adopts_others(tmp_path):
    """Review round 1, item 1: a parseable manifest with rate=0 reaches
    add_slot -> NativeAudioCircularBuffer -> ValueError, which escapes a
    narrow `except RuntimeError`. No on-disk artefact may abort a launch:
    adopt_scratch must skip it (leaving its files in place) and keep
    adopting everything else."""
    from flashback_sampler.core.manifest import Manifest, write_manifest
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.5)
    _written(st, co)
    st.shutdown()

    (tmp_path / "badrate.wav").write_bytes(b"\x00" * 64)  # just needs to exist
    write_manifest(tmp_path, Manifest(id="badrate", slot="Ghost", rate=0, channels=1, abs_start=0, abs_end=1,
                                      created_at=5.0, parent=None, file="badrate", start_frame=0, n_frames=1, trim_in=0, trim_out=0,
                                      state="pending", partial=False, bins=None))

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)  # must not raise
    ids = [c.id for c in st2.slots[0].checkout_manager.list()]
    assert ids == [co.id]  # the real checkout still adopted
    assert len(st2.slots) == 1  # no slot was left half-built for the bad manifest
    assert (tmp_path / "badrate.json").exists() and (tmp_path / "badrate.wav").exists()  # left in place
    st2.shutdown()


def test_adoption_takes_a_part_file_as_partial_and_skips_junk(tmp_path):
    from flashback_sampler.core.manifest import Manifest, write_manifest
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.5)
    _written(st, co)
    st.shutdown()
    p = tmp_path / f"{co.id}.wav"
    data = p.read_bytes()
    (tmp_path / f"{co.id}.wav.part").write_bytes(data[:44 + 100 * 4])
    p.unlink()
    # a manifest with no audio at all, and a corrupt one
    write_manifest(tmp_path, Manifest(id="ghost", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=0.0, parent=None, file="ghost", start_frame=0, n_frames=1, trim_in=0, trim_out=0,
                                      state="pending", partial=False, bins=None))
    (tmp_path / "bad.json").write_text("{")
    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    cos = st2.slots[0].checkout_manager.list()
    assert [c.id for c in cos] == [co.id]
    assert cos[0].partial is True and cos[0].n_frames == 100
    assert (tmp_path / "ghost.json").exists() and (tmp_path / "bad.json").exists()  # left in place
    st2.shutdown()


def test_adoption_of_a_slice_needs_its_parent(tmp_path):
    from flashback_sampler.core.manifest import Manifest, write_manifest
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.5)
    _written(st, co)
    st.shutdown()
    write_manifest(tmp_path, Manifest(id="sl", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=9.0, parent=co.id, file=co.id, start_frame=100, n_frames=50, trim_in=0, trim_out=0,
                                      state="saved", partial=False, bins=None))
    write_manifest(tmp_path, Manifest(id="orphan", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=9.5, parent="missing", file="missing", start_frame=0, n_frames=5, trim_in=0, trim_out=0,
                                      state="saved", partial=False, bins=None))
    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    ids = [c.id for c in st2.slots[0].checkout_manager.list()]
    assert ids == [co.id, "sl"]
    sl = st2.slots[0].checkout_manager.get("sl")
    assert sl.parent_id == co.id and sl.start_frame == 100 and sl.path == st2.slots[0].checkout_manager.get(co.id).path
    st2.shutdown()


def _stamp_created_at(scratch_dir, checkout_id, when):
    """Pin a manifest's creation stamp so `scan` adopts in a known order.
    The Windows `time.time()` tick is ~15.6 ms, so two manifests written
    in one tick would otherwise sort by id."""
    import json
    p = scratch_dir / f"{checkout_id}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["created_at"] = when
    p.write_text(json.dumps(d), encoding="utf-8")


def test_adoption_keeps_a_slice_whose_parent_is_gone(tmp_path):
    """C1: a trimmed drag mints a slice, then the parent is discarded --
    by the user, or by the window's count-cap eviction. The slice is the
    only reference keeping the parent's WAV alive, so adoption must take
    it: as a ROOT over that file at the slice's own span. Skipping it
    leaks both the WAV and the slice's own manifest forever."""
    from flashback_sampler.core import native
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.arange(1000, dtype=np.float32).reshape(-1, 1))
    mgr = st.checkout_manager
    co = mgr.create(duration_s=0.5)
    _written(st, co)
    sl = mgr.slice(co.id, 100, 50)
    parent_wav = co.path
    mgr.discard(co.id)  # the slice's refcount keeps the file alive
    assert parent_wav.exists()
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    mgr2 = st2.slots[0].checkout_manager
    assert [c.id for c in mgr2.list()] == [sl.id]
    back = mgr2.get(sl.id)
    assert (back.path, back.start_frame, back.n_frames, back.state) == (parent_wav, 100, 50, "saved")
    assert back.parent_id == co.id  # recorded, so the next launch takes this same path
    # Through the handle: the adopted checkout must read the slice's
    # samples, not the file's first 50 frames.
    got = native.wav_read(mgr2.export_range(back.id, tmp_path / "out" / "orphan.wav", 0, 50), 0, 50)
    assert np.array_equal(got, native.wav_read(parent_wav, 100, 50))
    mgr2.discard(back.id)  # the last reference to the file
    assert not parent_wav.exists() and not (tmp_path / f"{sl.id}.json").exists()
    st2.shutdown()


def test_adoption_keeps_a_nested_slice_whose_intermediate_parent_is_gone(tmp_path):
    """#72: root -> s1 -> s2, discard s1, relaunch. s2's manifest names
    s1 as its parent, but the audio lives in the root's file. Adoption
    must open s2 over `<root>.wav` at its absolute offset; skipping it
    strands the clip and pins the root's WAV for nothing."""
    from flashback_sampler.core import native
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.arange(1000, dtype=np.float32).reshape(-1, 1))
    mgr = st.checkout_manager
    root = mgr.create(duration_s=0.5)
    _written(st, root)
    s1 = mgr.slice(root.id, 100, 300)   # file frames 100..400
    s2 = mgr.slice(s1.id, 250, 50)      # file frames 350..400
    before = native.wav_read(mgr.export_range(s2.id, tmp_path / "out" / "before.wav", 0, 50), 0, 50)
    mgr.discard(s1.id)
    _stamp_created_at(tmp_path, s2.id, 20.0)
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    mgr2 = st2.slots[0].checkout_manager
    assert sorted(c.id for c in mgr2.list()) == sorted([root.id, s2.id])
    back = mgr2.get(s2.id)
    assert (back.path, back.start_frame, back.n_frames, back.state) == (root.path, 350, 50, "saved")
    after = native.wav_read(mgr2.export_range(s2.id, tmp_path / "out" / "after.wav", 0, 50), 0, 50)
    assert np.array_equal(after, before)
    # The root's file is shared by both: the refcount says so, and the
    # last discard removes it.
    assert mgr2.file_refcount(root.path) == 2
    mgr2.discard(root.id)
    assert root.path.exists()
    mgr2.discard(s2.id)
    assert not root.path.exists()
    st2.shutdown()


def test_adoption_of_a_slice_of_a_slice_reads_the_same_audio(tmp_path):
    """C2: a manifest's `start_frame` is ABSOLUTE into the file, but the
    Zig slice call takes a PARENT-RELATIVE start and adds the parent's
    own start itself. Re-adopting a nested slice with the absolute value
    reads at `parent.start + start` -- the wrong audio, or (here) past
    the parent's end, where Zig's range guard drops the slice."""
    from flashback_sampler.core import native
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.arange(1000, dtype=np.float32).reshape(-1, 1))
    mgr = st.checkout_manager
    co = mgr.create(duration_s=0.5)
    _written(st, co)
    s1 = mgr.slice(co.id, 100, 300)   # file frames 100..400
    s2 = mgr.slice(s1.id, 250, 50)    # file frames 350..400
    assert s2.start_frame == 350      # absolute into the file
    before = native.wav_read(mgr.export_range(s2.id, tmp_path / "out" / "before.wav", 0, 50), 0, 50)
    _stamp_created_at(tmp_path, s1.id, 10.0)
    _stamp_created_at(tmp_path, s2.id, 20.0)
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    mgr2 = st2.slots[0].checkout_manager
    assert sorted(c.id for c in mgr2.list()) == sorted([co.id, s1.id, s2.id])
    back = mgr2.get(s2.id)
    assert back.start_frame == 350
    after = native.wav_read(mgr2.export_range(s2.id, tmp_path / "out" / "after.wav", 0, 50), 0, 50)
    assert np.array_equal(after, before)
    st2.shutdown()


def test_adoption_skips_a_slice_that_starts_before_its_parent(tmp_path):
    """The absolute -> parent-relative conversion can only go negative on
    a manifest that disagrees with its parent. Skip it like any other
    corrupt manifest, and keep adopting everything else."""
    from flashback_sampler.core.manifest import Manifest, write_manifest
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.arange(1000, dtype=np.float32).reshape(-1, 1))
    mgr = st.checkout_manager
    co = mgr.create(duration_s=0.5)
    _written(st, co)
    s1 = mgr.slice(co.id, 100, 300)
    _stamp_created_at(tmp_path, s1.id, 10.0)
    write_manifest(tmp_path, Manifest(id="before", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=20.0, parent=s1.id, file=co.id, start_frame=50, n_frames=10, trim_in=0,
                                      trim_out=0, state="saved", partial=False, bins=None))
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert sorted(c.id for c in st2.slots[0].checkout_manager.list()) == sorted([co.id, s1.id])
    assert (tmp_path / "before.json").exists()  # left in place
    st2.shutdown()


def test_remove_slot_discards_its_checkouts_and_files(tmp_path):
    from flashback_sampler.core.quality_presets import preset_by_name
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    slot = st.add_slot(preset_by_name("SCRATCH"))
    slot.buffer.write(np.zeros((16_000, 1), dtype=np.float32))
    co = slot.checkout_manager.create(duration_s=0.1)
    st.remove_slot(1)
    assert not co.path.exists() and not (tmp_path / f"{co.id}.json").exists()
    st.shutdown()
