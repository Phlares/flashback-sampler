from __future__ import annotations

import logging

import pytest

from flashback_sampler.core.best_effort import best_effort


def test_returns_true_and_runs_the_call():
    calls = []
    assert best_effort(lambda: calls.append(1)) is True
    assert calls == [1]


def test_logs_and_returns_false_when_the_call_raises(caplog):
    err = RuntimeError("boom")

    def fail():
        raise err

    with caplog.at_level(logging.ERROR):
        assert best_effort(fail) is False
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[1] is err


def test_does_not_swallow_keyboard_interrupt():
    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        best_effort(interrupt)
