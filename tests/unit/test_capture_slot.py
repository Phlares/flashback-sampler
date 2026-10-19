"""
CaptureSlot dataclass tests. Focuses on the factory, the capture
source lifecycle, and the ram / buffered_seconds queries.
"""

from __future__ import annotations

import logging

import pytest

from flashback_sampler.core.capture_slot import CaptureSlot
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.native import NativeAudioCircularBuffer
from flashback_sampler.core.quality_presets import (
    PRESETS,
    default_preset,
    preset_by_name,
)
from tests.fixtures.fake_capture import (
    FakeCaptureSourceNoThread,
    RaisingCaptureSource,
)


@pytest.fixture
def scratch():
    from flashback_sampler.core.native import NativeScratch

    s = NativeScratch(budget_bytes=1 << 30)
    yield s
    s.close()


def _slot(scratch, tmp_path, preset, name=""):
    return CaptureSlot.from_quality_preset(
        preset, name=name, scratch=scratch, scratch_dir=tmp_path
    )


def test_from_quality_preset_builds_buffer_and_manager(scratch, tmp_path):
    preset = preset_by_name("SCRATCH")
    assert preset is not None
    slot = _slot(scratch, tmp_path, preset, name="Scratch pad")

    assert slot.name == "Scratch pad"
    assert slot.sample_rate == 16_000
    assert slot.channels == 1
    assert slot.buffer_seconds == 180.0
    assert slot.quality_preset == "SCRATCH"
    assert isinstance(slot.buffer, NativeAudioCircularBuffer)
    assert slot.buffer.duration == 180.0
    assert slot.buffer.sample_rate == 16_000
    assert slot.buffer.channels == 1
    assert isinstance(slot.checkout_manager, CheckoutManager)
    assert slot.capture_source is None


def test_from_quality_preset_default_name_uses_preset_name(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    assert slot.name == "MUSIC"


def test_slot_ids_are_unique(scratch, tmp_path):
    preset = default_preset()
    a = _slot(scratch, tmp_path, preset)
    b = _slot(scratch, tmp_path, preset)
    assert a.id != b.id


def test_slot_id_is_short_hex(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    assert isinstance(slot.id, str)
    assert len(slot.id) == 12
    int(slot.id, 16)  # must parse as hex


def test_initial_state_not_capturing_no_xruns(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    assert slot.is_capturing() is False
    assert slot.xrun_count() == 0


def test_start_capture_without_binding_raises(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    with pytest.raises(RuntimeError, match="no capture source"):
        slot.start_capture()


def test_bind_capture_and_start_stop(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    source = FakeCaptureSourceNoThread(
        slot.buffer, sample_rate=slot.sample_rate, channels=slot.channels
    )
    slot.bind_capture(source)
    assert slot.capture_source is source

    slot.start_capture()
    assert slot.is_capturing() is True

    slot.stop_capture()
    assert slot.is_capturing() is False


def test_bind_capture_replaces_and_stops_previous(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    a = FakeCaptureSourceNoThread(
        slot.buffer, sample_rate=slot.sample_rate, channels=slot.channels
    )
    b = FakeCaptureSourceNoThread(
        slot.buffer, sample_rate=slot.sample_rate, channels=slot.channels
    )
    slot.bind_capture(a)
    a.start()
    slot.bind_capture(b)  # should stop `a`
    assert a.is_running() is False
    assert slot.capture_source is b


def test_ram_matches_preset(scratch, tmp_path):
    preset = preset_by_name("CHAT")
    assert preset is not None
    slot = _slot(scratch, tmp_path, preset)
    assert slot.ram_bytes() == preset.ram_bytes()
    assert slot.ram_mb() == preset.ram_mb()


def test_buffered_seconds_delegates_to_buffer(scratch, tmp_path):
    import numpy as np

    slot = _slot(scratch, tmp_path, preset_by_name("SCRATCH"))
    assert slot.buffered_seconds() == 0.0
    # Write 1 second of silence (16 kHz mono)
    block = np.zeros((16_000, 1), dtype=np.float32)
    slot.buffer.write(block)
    assert slot.buffered_seconds() == pytest.approx(1.0)


def test_xrun_count_follows_bound_source(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    source = FakeCaptureSourceNoThread(
        slot.buffer, sample_rate=slot.sample_rate, channels=slot.channels
    )
    slot.bind_capture(source)
    assert slot.xrun_count() == 0
    source.bump_xrun()
    source.bump_xrun()
    source.bump_xrun()
    assert slot.xrun_count() == 3


def test_all_presets_produce_valid_slots(scratch, tmp_path):
    """Every preset in the table must build a working slot."""
    for preset in PRESETS:
        slot = _slot(scratch, tmp_path, preset)
        assert slot.buffer.duration == preset.buffer_seconds
        assert slot.buffer.sample_rate == preset.sample_rate
        assert slot.buffer.channels == preset.channels


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_bind_capture_logs_a_failing_stop_and_binds_the_new_source(
    scratch, tmp_path, caplog
):
    slot = _slot(scratch, tmp_path, default_preset())
    slot.bind_capture(RaisingCaptureSource(fail=("stop",)))
    new = FakeCaptureSourceNoThread(slot.buffer)
    slot.bind_capture(new)
    assert slot.capture_source is new
    assert len(_errors(caplog)) == 1


def test_stop_capture_raises_what_stop_raises(scratch, tmp_path):
    """Callers decide: the UI warns, teardown logs and moves on."""
    slot = _slot(scratch, tmp_path, default_preset())
    slot.bind_capture(RaisingCaptureSource(fail=("stop",)))
    with pytest.raises(RuntimeError, match="stop"):
        slot.stop_capture()


def test_last_error_propagates_what_the_source_raises(scratch, tmp_path):
    slot = _slot(scratch, tmp_path, default_preset())
    slot.bind_capture(RaisingCaptureSource(fail=("last_error",)))
    with pytest.raises(RuntimeError, match="last_error"):
        slot.last_error()
