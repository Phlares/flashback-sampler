"""Every AppState in the unit suite gets its own scratch dir under
tmp_path, and a fixed checkout cache budget — never the developer's
real user-cache dir or real config.json. state.py (once h9 lands) reads
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
    yield
