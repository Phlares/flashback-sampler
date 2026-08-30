"""
Unit tests for AppState — the headless object graph root.

These tests do not import PySide6; AppState is a plain Python container
that owns the buffer, checkout manager, and scrub player.
"""

from __future__ import annotations

import numpy as np
import pytest

from flashback_sampler.app.state import AppState
from flashback_sampler.core.buffer import RingDerivedOps
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.scrub_player import ScrubPlayer


def test_appstate_wires_core_objects_with_matching_sample_rate_and_channels():
    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=2)
    # RingDerivedOps, not AudioCircularBuffer specifically -- st.buffer is
    # constructed via make_ring_buffer, which returns whichever ring
    # implementation (Python or native) the machine has available.
    assert isinstance(st.buffer, RingDerivedOps)
    assert isinstance(st.checkout_manager, CheckoutManager)
    assert isinstance(st.scrub_player, ScrubPlayer)
    assert st.sample_rate == 16_000
    assert st.channels == 2
    assert st.buffer.sample_rate == 16_000
    assert st.buffer.channels == 2
    assert st.scrub_player.sample_rate == 16_000
    assert st.scrub_player.channels == 2


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

    st.apply_checkout_caps(max_active=3, max_ram_mb=64)
    # Active slot (0) updated
    assert st.slots[0].checkout_manager._max_active == 3
    # Slot 1 left at the default 16
    assert st.slots[1].checkout_manager._max_active == 16


def test_total_project_ram_bytes_counts_every_slot():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=5.0, sample_rate=48_000, channels=2)
    # Main slot ~1.92 MB (5s * 48k * 2 * 4). capacity_bytes, not
    # .buffer.nbytes -- the latter is the raw storage array, which on
    # NativeAudioCircularBuffer is larger than the readable window by a
    # guard band (see buffer.py's RingDerivedOps.capacity_bytes docstring).
    main_bytes = st.slots[0].buffer.capacity_bytes
    assert main_bytes == 5 * 48_000 * 2 * 4

    st.add_slot(preset_by_name("SCRATCH"))  # +~11 MB
    total = st.total_project_ram_bytes()
    assert total > main_bytes
    # SCRATCH = 16k mono 180s = 16000 * 180 * 4 = 11_520_000
    expected = main_bytes + 11_520_000
    assert total == expected


def test_total_project_ram_includes_checkouts():
    st = AppState(buffer_seconds=2.0, sample_rate=1000, channels=1)
    buf_bytes = st.slots[0].buffer.capacity_bytes
    assert st.total_project_ram_bytes() == buf_bytes
    # Write some audio and create a checkout
    st.slots[0].buffer.write(
        np.zeros((1000, 1), dtype=np.float32)  # 1 s
    )
    co = st.slots[0].checkout_manager.create(duration_s=0.5)
    # Total now includes the checkout's ndarray
    assert st.total_project_ram_bytes() == buf_bytes + co.ram_bytes


def test_add_slot_rejects_when_over_budget():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=5.0, sample_rate=48_000, channels=2)
    # Set a tight budget that only fits the initial slot
    current = st.total_project_ram_mb()
    st.set_project_ram_budget_mb(current + 1)  # tiny headroom

    with pytest.raises(RuntimeError, match="Project RAM budget exceeded"):
        st.add_slot(preset_by_name("FULL"))


def test_add_slot_succeeds_within_budget():
    from flashback_sampler.core.quality_presets import preset_by_name

    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_project_ram_budget_mb(4096.0)
    slot = st.add_slot(preset_by_name("CHAT"))
    assert slot is not None
    assert len(st.slots) == 2


def test_set_project_ram_budget_enforces_minimum():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.set_project_ram_budget_mb(10)  # below 64 min
    assert st.project_ram_budget_mb == 64.0


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


def test_checkout_from_live_buffer_then_bind_to_scrub_player():
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
    assert co.audio.shape == (500, 1)

    st.scrub_player.bind(co.audio)
    st.scrub_player.play()
    out = np.zeros((100, 1), dtype=np.float32)
    st.scrub_player._audio_callback(out, 100, None, None)
    assert np.allclose(out[:, 0], ramp[:100, 0])


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
    fresh one via make_ring_buffer -- the OLD buffer's close() must be
    called so a NativeAudioCircularBuffer's Zig-owned handle is released
    deterministically instead of relying only on eventual GC/__del__."""
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
