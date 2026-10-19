# Zero Suppressions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all 56 inline suppressions and add guards that stop new or dead ones.

**Architecture:** Fix each site in code, grouped by mechanism. One `best_effort()` helper replaces every swallowed teardown error. Guards land last, once the tree is clean.

**Tech Stack:** Python 3.10+, PySide6, pytest, ruff 0.17.1 (via prek), pyright (hash-pinned).

**Spec:** Phlares/flashback-sampler#106. Every site and its fix is listed there.

## Global Constraints

- `typing.cast` and new `Any` annotations do not count as fixes.
- Write the failing test first for each behaviour change.
- Baseline: 637 passed (`pytest tests/unit -m "not audio_hw and not perf"`).
- Done: `git grep -nE "type: ignore|pyright: ignore|noqa|pragma: no cover|fmt: (off|skip)" -- '*.py'` prints nothing.
- Do not add the shared workflows suppression hook (wave 2).
- Commands run from the worktree with `.venv/Scripts/python`, env `QT_QPA_PLATFORM=offscreen PYTHONUTF8=1 FLASHBACK_REQUIRE_NATIVE=1`.

## Review Focus

- A teardown call raising `KeyboardInterrupt`: `best_effort` must not swallow it (catches `Exception` only). Test in Task 2.
- `stop_rolling` with one slot whose `stop()` raises: the count excludes it, the other slots still stop. Test in Task 2.
- `shutdown` with every step raising: every later step still runs. Test in Task 2.
- A test that patches `QMessageBox.warning` itself: it must restore the session stub, not the real dialog. Covered by `MonkeyPatch` undo order (Task 3).
- `ctypes.wintypes` import on Linux, Python 3.10 and 3.12. Checked in Task 6 with Docker.

---

### Task 1: Delete the dead noqa and coverage pragmas on tests

**Files:**
- Modify: `flashback_sampler/app/widgets/{duration_preset,selectable_waveform,tactile_button,waveform_view}.py`, `flashback_sampler/input/sources/global_hotkey.py:192`, `flashback_sampler/app/process_picker_dialog.py:128`
- Modify: `tests/unit/test_buffer.py:307`, `tests/unit/test_no_sans_serif.py:58`

No behaviour change, no new test. Ruff selects `E, F, W, I, B, UP`, so `N802`/`D401` never fire; coverage measures only `flashback_sampler`.

- [ ] Delete the 15 `# noqa: N802|D401` comments and the 2 test `# pragma: no cover` comments.
- [ ] Run `prek run --all-files` and the suite. Expected: clean, 637 passed.
- [ ] Commit `chore: delete dead noqa and test coverage pragmas`.

### Task 2: `best_effort` for teardown, and the CaptureSource `last_error()`

**Files:**
- Create: `flashback_sampler/core/best_effort.py`, `tests/unit/test_best_effort.py`
- Modify: `flashback_sampler/app/state.py` (9 teardown sites + `:416`), `flashback_sampler/core/capture_slot.py:162,175,186-195`
- Modify: `tests/fixtures/fake_capture.py`, `tests/unit/test_app_state.py`, `tests/unit/test_capture_slot.py`

**Interfaces:**
- Produces: `best_effort(fn: Callable[[], object]) -> bool` — calls `fn()`; on `Exception` logs it with traceback through `logging.getLogger("flashback_sampler.core.best_effort")` at ERROR and returns False; returns True when `fn` returned. `BaseException` subclasses that are not `Exception` propagate.
- Produces: `RaisingCaptureSource(fail: Iterable[str])` in `tests/fixtures/fake_capture.py` — satisfies `CaptureSource`; each method named in `fail` raises `RuntimeError(name)`; `is_running()` returns True from construction, so every stop path is taken.

- [ ] **Step 1: Failing tests for `best_effort`** in `test_best_effort.py`:
  - `test_returns_true_and_runs_the_call`: calls recorded, result True.
  - `test_logs_and_returns_false_when_the_call_raises` (caplog): result False; one ERROR record; `record.exc_info[1]` is the raised error.
  - `test_does_not_swallow_keyboard_interrupt`: `pytest.raises(KeyboardInterrupt)`.
- [ ] **Step 2: Failing tests for the call sites**, each with `RaisingCaptureSource` or a monkeypatched raising method:
  - `CaptureSlot.bind_capture` with an old source whose `stop` raises: new source is bound, one ERROR record.
  - `CaptureSlot.stop_capture` with `stop` raising: no raise, one ERROR record.
  - `CaptureSlot.last_error` with `last_error` raising: `pytest.raises(RuntimeError)` (the Protocol declares it; no `getattr`, no `try`).
  - `AppState.remove_slot` with `stop` and `buffer.close` raising: slot removed, checkouts discarded, two ERROR records.
  - `AppState.start_rolling` with `build_capture_for_slot` returning a source whose `start` raises: returns `(0, err)` where `err` is that exception; `rolling` is True.
  - `AppState.stop_rolling` with two capturing slots, one raising on `stop`: returns 1; `rolling` False.
  - `AppState.rebuild_buffer` with `stop` and old `buffer.close` raising: new buffer of the new duration is in place.
  - `AppState.shutdown` with `stop`, `checkout_manager.close`, `scratch.close`, and `scrub_player.close` all raising: each is called once (record order), no raise.
- [ ] **Step 3: Run them.** Expected: `best_effort` tests fail on import; logging assertions fail (no records today); `last_error` test fails (swallowed).
- [ ] **Step 4: Implement** `best_effort`; replace every `try: x() except Exception: pass  # pragma: no cover` with `best_effort(x)`. `stop_rolling` counts `if best_effort(slot.stop_capture)`. Drop the pragma at `state.py:416` and keep its `except`. `CaptureSlot.last_error` returns `self.capture_source.last_error()`.
- [ ] **Step 5: Run the suite.** Expected: 637 + new, all pass.
- [ ] **Step 6: Mutation check** on `best_effort.py`: (a) re-raise in the `except`; (b) delete the log call. Each must turn tests red. Revert.
- [ ] **Step 7: Commit** `fix: log swallowed teardown errors through best_effort`.

### Task 3: Session fixture for the Qt dialog stubs

**Files:**
- Modify: `conftest.py`
- Create: `tests/unit/test_dialog_stubs.py`

- [ ] **Step 1: Failing test** `test_message_box_helpers_return_without_a_dialog`: `QMessageBox.warning(None, "t", "m") is QMessageBox.StandardButton.Ok`, `question(...) is StandardButton.Yes`. Today `warning` returns `QMessageBox.Ok` — run it to see whether it passes; if it already passes, it is a guard, not a red step (the refactor keeps behaviour).
- [ ] **Step 2: Replace** the import-time block with one `@pytest.fixture(scope="session", autouse=True) def _no_modal_dialogs()` that opens `pytest.MonkeyPatch.context()` and `mp.setattr(...)` each of the 11 stubs, then yields. Use `QMessageBox.StandardButton.Ok/Yes`. Drop the `exec_` patch and the `except ImportError`. Keep the comment on why the stubs are process-wide.
- [ ] **Step 3: Run the suite** with `timeout = 30`. Expected: all pass, no hang.
- [ ] **Step 4: Commit** `test: patch Qt dialogs in a session fixture`.

### Task 4: Remove the `fmt` parameter

**Files:**
- Modify: `flashback_sampler/core/checkout.py` (`CheckoutFormat`, `_VALID_FORMATS`, `save`), `flashback_sampler/app/turntable_window.py:1371`
- Test: `tests/unit/test_checkout.py`

- [ ] **Step 1: Failing test** `test_save_has_no_format_parameter`: `"fmt" not in inspect.signature(CheckoutManager.save).parameters`.
- [ ] **Step 2:** In `test_save_validation`, drop the FLAC line; replace the `subtype="PCM_8"` save with `mgr.export_range(co.id, tmp_path / "x.wav", 0, 10, subtype="PCM_8")` under `pytest.raises(ValueError)`.
- [ ] **Step 3: Implement.** Delete the parameter, `CheckoutFormat`, `_VALID_FORMATS`, and the check. Drop `fmt="WAV"` at the caller.
- [ ] **Step 4: Run** the suite and pyright. Expected: pass.
- [ ] **Step 5: Commit** `refactor: drop the always-WAV fmt parameter from save`.

### Task 5: Public accessors and test-input rewrites

**Files:**
- Modify: `flashback_sampler/core/checkout.py`, `flashback_sampler/app/state.py:523,553`, `flashback_sampler/core/manifest.py:149`
- Modify: `tests/unit/test_checkout.py`, `tests/unit/test_turntable_window.py:1264,1267`, `tests/unit/test_manifest.py:191`, `tests/unit/input/test_events.py:24`, `tests/unit/test_audio_devices.py:38,44`

**Interfaces:**
- Produces on `CheckoutManager`: `set_max_active(n: int) -> None`; `rebind_buffer(buffer: NativeAudioCircularBuffer) -> None` (under `self._lock`); `pinned_id -> str | None` (read-only property).
- Produces in `test_checkout.py`: `_ring(seconds=2.0, rate=1000, channels=1, frames=1500) -> NativeAudioCircularBuffer`; `_mgr(..., buf=None)` uses `_ring` when `buf` is None.

- [ ] **Step 1: Failing tests** in `test_checkout.py`:
  - `test_set_max_active_caps_creates`: `set_max_active(1)`; second `create` raises `RuntimeError` matching `"Maximum active checkouts reached (1)"`.
  - `test_rebind_buffer_cuts_new_checkouts_from_the_new_ring`: new ring filled with a different ramp offset; the next checkout's audio starts with that value.
  - `test_pinned_id_reports_the_pin`: None, then `pin(a.id)` → `a.id`, then `pin(None)` → None.
- [ ] **Step 2: Run.** Expected: AttributeError.
- [ ] **Step 3: Implement** the three members. `state.py` calls `mgr.set_max_active(...)` and `slot.checkout_manager.rebind_buffer(new_buf)`.
- [ ] **Step 4: Rewrite tests:** `test_turntable_window` reads `mgr.pinned_id`; `test_flushing_buffer_...` builds `buf = _ring()` and passes `buf=buf`, then `buf.flush()`.
- [ ] **Step 5:** `bins_from_json(d: object, channels: int)`; drop the ignore at `test_manifest.py:191`.
- [ ] **Step 6:** Frozen tests: for every field in `dataclasses.fields(obj)`, `setattr(obj, f.name, ...)` inside `pytest.raises(FrozenInstanceError)`. The field name comes from a variable, so ruff `B010` does not fire, and pyright still enforces frozen fields on direct assignment.
- [ ] **Step 7: Run** suite, pyright, prek. Expected: pass, 0 errors.
- [ ] **Step 8: Commit** `refactor: public CheckoutManager accessors; tests use public inputs`.

### Task 6: Hard-dependency imports, config, guards, follow-ups

**Files:**
- Modify: `flashback_sampler/app/theme.py:52-59`, `flashback_sampler/input/sources/global_hotkey.py:24-27,111,124,198`
- Modify: `pyproject.toml`, `.github/workflows/test.yml:1-9`
- Create: `tests/fixtures/alc.py`; Modify: `tests/unit/test_alc.py:71`, `tests/unit/test_drag_export.py:227`

**Interfaces:**
- Produces: `audio_clip(path: Path) -> ET.Element` in `tests/fixtures/alc.py` — gunzip, parse, first `AudioClip`. Replaces `test_alc._clip` and `test_drag_export._alc_clip`.

- [ ] **Step 1:** `theme.py`: plain `from PySide6.QtGui import QFontDatabase`, no `try`. `global_hotkey.py`: `from ctypes import wintypes`; drop the `None` fallback, both asserts, and the `_wintypes is None` check.
- [ ] **Step 2: Confirm on Linux:** `docker run --rm python:3.10-slim python -c "from ctypes import wintypes; print(wintypes.HWND)"`, same for 3.12. Expected: prints the type.
- [ ] **Step 3:** `pyproject.toml`: drop `"pragma: no cover"` from `exclude_also`; `"\\.\\.\\."` → `"^\\s*\\.\\.\\.$"`; delete `ignore::DeprecationWarning`, run the suite, and read the warnings summary. If a third-party module emits one, add `ignore::DeprecationWarning:<module>` for that module only. A warning from `flashback_sampler` gets a code fix instead.
- [ ] **Step 4: Guards:** ruff `select` adds `RUF100`, `PGH004`; `[tool.pyright]` adds `enableTypeIgnoreComments = false`, `reportUnnecessaryTypeIgnoreComment = "error"`.
  The cap of 0 has no file in this repo yet; the shared hook (wave 2) carries it. The PR body states the cap.
- [ ] **Step 5: Guard check:** add a scratch `x = 1  # noqa: E501` → `ruff check` reports RUF100; add `y: int = "a"  # type: ignore` → pyright reports the error. Revert.
- [ ] **Step 6: alc helper** to `tests/fixtures/alc.py`; both test files import it.
- [ ] **Step 7: test.yml header:** name the four required checks: lint, pytest, typecheck, zig on `main` and `dev`.
- [ ] **Step 8: Verify** the success-criteria grep prints nothing; suite, pyright, prek all clean.
- [ ] **Step 9: Commit** `ci: guard against new and dead suppressions`.
