"""Pure-core per-source health evaluation (no Qt, no hardware)."""

from __future__ import annotations

from flashback_sampler.core.source_status import (
    Severity,
    SourceSnapshot,
    evaluate,
    worst,
)


def snap(**kw):
    base = dict(capturing=True, peak=0.2, silent_seconds=0.0, buffer_fill=0.0,
                xrun_rate=0.0, error=None)
    base.update(kw)
    return SourceSnapshot(**base)


def test_error_takes_precedence_over_everything():
    s = evaluate(snap(error="device invalidated", peak=0.5))
    assert s.severity is Severity.ERROR
    assert s.code == "error"


def test_not_capturing_is_idle_ok():
    s = evaluate(snap(capturing=False))
    assert s.severity is Severity.OK
    assert s.code == "idle"


def test_clipping_is_warning():
    s = evaluate(snap(peak=0.999))
    assert s.severity is Severity.WARN
    assert s.code == "clip"


def test_clipping_outranks_buffer_and_silence():
    s = evaluate(snap(peak=1.0, buffer_fill=0.99))
    assert s.code == "clip"


def test_buffer_near_full_is_warning():
    s = evaluate(snap(peak=0.2, buffer_fill=0.99))
    assert s.severity is Severity.WARN
    assert s.code == "buffer_high"


def test_silent_beyond_grace_is_warning():
    s = evaluate(snap(peak=0.0, silent_seconds=6.0))
    assert s.severity is Severity.WARN
    assert s.code == "silent"


def test_silent_within_grace_stays_ok():
    # below the silence floor but not yet long enough → no alarm
    s = evaluate(snap(peak=0.0, silent_seconds=1.0))
    assert s.severity is Severity.OK
    assert s.code == "ok"


def test_low_level_is_info():
    # ~ -44 dBFS: audible but quiet
    s = evaluate(snap(peak=0.006, silent_seconds=0.0))
    assert s.severity is Severity.INFO
    assert s.code == "low"


def test_healthy_signal_is_ok():
    s = evaluate(snap(peak=0.2))
    assert s.severity is Severity.OK
    assert s.code == "ok"


def test_worst_returns_highest_severity():
    a = evaluate(snap(peak=0.2))                 # OK
    b = evaluate(snap(peak=0.006))               # INFO
    c = evaluate(snap(peak=0.0, silent_seconds=9))  # WARN
    assert worst([a, b, c]).severity is Severity.WARN
    assert worst([a, b]).severity is Severity.INFO


def test_worst_of_empty_is_ok():
    assert worst([]).severity is Severity.OK
