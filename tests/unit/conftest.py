"""Every AppState in the unit suite gets its own scratch dir under
tmp_path, a fixed checkout cache budget, and a fixed export bit
depth and drag handle budget — never the developer's real
user-cache dir or real config.json. state.py (once h9 lands) reads
these prefs through the module attribute (`app_config.load_scratch_dir()`
/ `app_config.load_checkout_cache_mb()`), which is what makes this
monkeypatch take.

test_config.py is excluded (see R-h7a): it exercises the REAL
`load_scratch_dir` / `load_checkout_cache_mb` against an explicit tmp
config path, and patching them suite-wide would make those roundtrip
tests tautological (they'd assert the fixture's stub, not the function
under test).
"""
from __future__ import annotations

import pytest

_UNPATCHED_MODULES = {"test_config", "tests.unit.test_config"}


@pytest.fixture(autouse=True)
def _isolated_scratch_dir(tmp_path, monkeypatch, request):
    if request.module.__name__ in _UNPATCHED_MODULES:
        yield
        return

    from flashback_sampler.app import config

    monkeypatch.setattr(config, "load_scratch_dir", lambda path=None: tmp_path / "scratch")
    # R-h7b: pin the eviction budget so no unit test's timing depends on
    # whatever checkout_cache_mb happens to be in the developer's real
    # config.json.
    monkeypatch.setattr(config, "load_checkout_cache_mb", lambda path=None: config.DEFAULT_CHECKOUT_CACHE_MB)
    # Same reason, one import hop further out: TurntableWindow does
    # `from ...config import load_export_bit_depth, load_drag_handle_mb`,
    # so the names it calls live on the window module, not on config.
    # Unpinned, every drag test's export span would be computed from
    # whatever bit depth and handle budget the developer last saved.
    from flashback_sampler.app import turntable_window
    monkeypatch.setattr(turntable_window, "load_export_bit_depth", lambda path=None: "FLOAT")
    monkeypatch.setattr(turntable_window, "load_drag_handle_mb", lambda path=None: config.DEFAULT_DRAG_HANDLE_MB)
    yield
