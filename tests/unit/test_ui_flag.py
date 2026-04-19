"""Tests for --ui turntable entry point flag."""
from __future__ import annotations

import pytest

from flashback_sampler.app.main import _parse_args


def test_default_ui_is_turntable():
    args = _parse_args([])
    assert args.ui == "turntable"


def test_classic_ui_flag():
    args = _parse_args(["--ui", "classic"])
    assert args.ui == "classic"


def test_invalid_ui_rejected():
    with pytest.raises(SystemExit):
        _parse_args(["--ui", "bogus"])
