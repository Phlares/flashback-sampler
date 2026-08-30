"""TurntableWindow — dual-turntable layout, the application's main window."""
from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from flashback_sampler.app.audio_devices import (
    CaptureDevice,
    apply_rate_probe,
    list_capture_devices,
)
from flashback_sampler.app.time_format import format_time_signed_cs
from flashback_sampler.app.process_picker_dialog import ProcessPickerDialog
from flashback_sampler.app.config import (
    config_dir,
    load_export_bit_depth,
    load_export_pool_dir,
    load_global_hotkeys_enabled,
    load_show_notifications,
    save_export_bit_depth,
    save_export_pool_dir,
    save_global_hotkeys_enabled,
    save_show_notifications,
)
from flashback_sampler.app.drag_out import perform_file_drag
from flashback_sampler.app.preferences_dialog import PreferencesDialog
from flashback_sampler.app.state import AppState
from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.widgets.center_bridge import CenterBridge
from flashback_sampler.app.widgets.duration_preset import DEFAULT_PRESETS
from flashback_sampler.app.widgets.nav_bar import NavBar
from flashback_sampler.app.widgets.tactile_button import TactileButton
from flashback_sampler.app.widgets.turntable_widget import TurntableWidget
from flashback_sampler.app.widgets.waveform_panel import WaveformPanel
from flashback_sampler.core.drag_export import (
    render_drag_file,
    sanitize_source_name,
)
from flashback_sampler.core.source_status import (
    SILENCE_DBFS,
    Severity,
    SourceSnapshot,
    SourceStatus,
    evaluate,
    worst,
)
from flashback_sampler.input.core import Action, BindingTable, invoke, register
from flashback_sampler.input.sources.global_hotkey import (
    GlobalHotkeySource,
    build_global_bindings,
)
from flashback_sampler.input.sources.qt_keyboard import KeyboardSource
from flashback_sampler.input.ui.settings_dialog import KeybindingsDialog
from flashback_sampler.platform.capabilities import (
    global_hotkeys_supported,
    tray_supported,
)
from flashback_sampler.platform.tray import SystemTray

# Linear magnitude of the silence floor, for the per-source silent-duration tally.
_SILENCE_MAG = 10.0 ** (SILENCE_DBFS / 20.0)

SELECTION_COLOR_BUFFER = "#FFD900"   # yellow
SELECTION_COLOR_CLIP = "#FF9500"     # orange


def _peak_bins_from_audio(audio: np.ndarray, n_bins: int) -> np.ndarray:
    """Same shape as AudioCircularBuffer.get_peak_bins but operating on
    a static (N, channels) array — used to render a checkout's fixed audio."""
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    n = int(audio.shape[0])
    channels = int(audio.shape[1])
    out = np.zeros((n_bins, 2, channels), dtype=np.float32)
    if n == 0:
        return out
    edges = np.linspace(0, n, n_bins + 1, dtype=np.int64)
    for i in range(n_bins):
        a, b = int(edges[i]), int(edges[i + 1])
        if b <= a:
            if i > 0:
                out[i] = out[i - 1]
            continue
        chunk = audio[a:b]
        out[i, 0] = chunk.min(axis=0)
        out[i, 1] = chunk.max(axis=0)
    return out


class TurntableWindow(QMainWindow):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("Flashback — Turntable UI")
        self.setMinimumSize(960, 700)
        self.resize(1120, 800)

        # Keep keybindings next to the app's config.json (same dir as
        # AppState's device/buffer settings) rather than the binding
        # engine's standalone default, so all per-user state lives in one
        # place. input/core stays app-agnostic — the app injects the path.
        self._binding_table = BindingTable(storage_path=config_dir() / "bindings.json")
        self._keyboard_source = KeyboardSource(self._binding_table, self)

        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background-color: {EREBUS['chassis']};")

        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 0)
        root.setSpacing(4)

        # ── Row 1: Turntables + Center Bridge ────────────────────────
        turntable_row = QHBoxLayout()
        turntable_row.setSpacing(0)

        self.buffer_turntable = TurntableWidget(side="buffer")
        self.buffer_turntable.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        turntable_row.addWidget(self.buffer_turntable, stretch=1)

        self.center_bridge = CenterBridge()
        turntable_row.addWidget(self.center_bridge)

        self.clip_turntable = TurntableWidget(side="clip")
        self.clip_turntable.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        turntable_row.addWidget(self.clip_turntable, stretch=1)

        root.addLayout(turntable_row, stretch=6)

        # ── Row 2: Waveform Panels + OUT Button ──────────────────────
        waveform_row = QHBoxLayout()
        waveform_row.setSpacing(4)

        self.buffer_panel = WaveformPanel(side="buffer")
        waveform_row.addWidget(self.buffer_panel, stretch=1)

        self.out_btn = TactileButton("OUT →", variant="primary")
        self.out_btn.setFixedWidth(56)
        self.out_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        # Align OUT→ vertically with the container, not the full panel height.
        # Top spacing ≈ header row height (8pt label + panel top margin + spacing).
        out_col = QVBoxLayout()
        out_col.setContentsMargins(0, 0, 0, 0)
        out_col.setSpacing(0)
        out_col.addSpacing(20)
        out_col.addWidget(self.out_btn, stretch=1)
        waveform_row.addLayout(out_col)

        self.clip_panel = WaveformPanel(side="clip")
        waveform_row.addWidget(self.clip_panel, stretch=1)

        root.addLayout(waveform_row, stretch=2)

        # ── Row 3: Controls ──────────────────────────────────────────
        controls_row = QHBoxLayout()
        controls_row.setSpacing(4)

        # Left column: buffer controls packed tight, then stretch to push right
        buffer_col = QHBoxLayout()
        buffer_col.setSpacing(4)
        self.buffer_controls: list[TactileButton] = []
        for label in ["FLUSH", "−", "+", "◀", "▶", "FREEZE"]:
            btn = TactileButton(label, variant="secondary")
            btn.setMinimumWidth(40); btn.setMinimumHeight(36)
            if label == "FREEZE":
                btn.setCheckable(True)
            self.buffer_controls.append(btn)
            buffer_col.addWidget(btn)
        buffer_col.addStretch()
        controls_row.addLayout(buffer_col, stretch=1)

        # Center column: LOOP, exact column width, no flanking stretches in THIS layout
        self.loop_btn = TactileButton("LOOP", variant="primary")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setFixedWidth(56)
        self.loop_btn.setMinimumHeight(36)
        controls_row.addWidget(self.loop_btn)

        # Right column: stretch first, then clip controls
        clip_col = QHBoxLayout()
        clip_col.setSpacing(4)
        clip_col.addStretch()
        self.clip_controls: list[TactileButton] = []
        for label in ["PLAY", "−", "+", "◀", "▶", "SAVE"]:
            btn = TactileButton(label, variant="secondary")
            btn.setMinimumWidth(40); btn.setMinimumHeight(36)
            self.clip_controls.append(btn)
            clip_col.addWidget(btn)
        controls_row.addLayout(clip_col, stretch=1)

        root.addLayout(controls_row, stretch=1)

        # ── Row 4: Nav Bar ───────────────────────────────────────────
        self.nav_bar = NavBar()
        root.addWidget(self.nav_bar)

        # Buffer turntable has one ring per slot (at least 1). Clip
        # turntable rings correspond to checkouts and are set in
        # _refresh_clip_side() below.
        n = max(len(state.slots), 1)
        self.buffer_turntable.set_track_count(n)

        # Selection state for drift-with-audio.
        # abs_samples: (start, end) in total_written-space. Stays fixed; we
        # compute current fractions each tick based on the buffer's current
        # write position so the selection keeps pointing at the same audio
        # content as the buffer advances.
        # mode: "default" (anchored to "now" — re-computed every tick from
        # slot.duration_preset_idx/anchor_offset_s) or "user" (fixed abs
        # samples captured when the user drags).
        self._buffer_sel_abs: tuple[int, int] | None = None
        self._buffer_sel_mode: str = "default"  # "default" | "user"
        # Clip selection is stored as FRACTIONS of the currently displayed
        # clip's audio — the clip is an immutable snapshot, so this never
        # drifts with the live buffer's advancing write head. Keyed by
        # checkout id so switching clips preserves each one's trim.
        self._clip_trim_fracs: dict[str, tuple[float, float]] = {}
        # Waveform bins per checkout id ("ring_amp" 540-bin radial amp,
        # "panel_bins" 360-bin min/max). Checkout audio is immutable, so
        # these are computed once — without the cache every refresh
        # recomputes every banked clip and refresh cost grows with each
        # drag (measured live: 0.17s -> 1.7s over 8 drags). Pruned in
        # _refresh_clip_side when checkouts disappear.
        self._clip_bins_cache: dict[str, dict[str, np.ndarray]] = {}

        # Tracks whether the scrub player was rolling on the previous
        # tick — used to auto-restart on loop when playback drains.
        self._was_playing_last_tick: bool = False
        # Whether the user has asked for playback (True) or stopped
        # explicitly (False). LOOP only auto-restarts while this is
        # True, so pressing STOP while LOOP is on actually stops.
        self._intending_playback: bool = False
        # Last native last_error() string shown to the user as a
        # playback-failed dialog. The native player opens its device
        # lazily on the Zig render thread, so an open failure surfaces
        # later — as last_error() plus playing flipping back to 0 — not
        # as an exception at the play() call. This remembers what was
        # already shown so the ~100ms tick doesn't re-pop the dialog.
        self._last_playback_error_shown: str | None = None

        # FREEZE state: when True, the buffer panel's waveform + time
        # labels + timeline are held at the snapshot taken when freeze
        # was engaged; the ring keeps polling live audio, and selection
        # fractions are computed against the frozen total/buffered_s so
        # the user can drag on a static waveform while capture rolls on.
        self._buffer_frozen: bool = False
        self._buffer_frozen_total: int = 0
        self._buffer_frozen_buffered_s: float = 0.0

        # Guard so the "muxing combines inputs" warning only fires the
        # first time the user asks to mux within this session.
        self._mux_warning_shown: bool = False

        # Explicit start/stop primitives drive the center buttons and the tray
        # menu; they're not directly rebindable (bindable=False) — the user-facing
        # record key is the single toggle below, so one key works the same
        # focused or minimized.
        register(Action(id="transport.start_recording", name="Start Recording",
                        category="Transport",
                        callable=self._on_start_clicked,
                        repeat_policy="ignore_repeat", bindable=False))
        register(Action(id="transport.stop_recording", name="Stop Recording",
                        category="Transport",
                        callable=self._on_stop_clicked,
                        repeat_policy="ignore_repeat", bindable=False))
        register(Action(id="transport.toggle_recording", name="Toggle Recording",
                        category="Transport",
                        callable=self._on_toggle_recording,
                        default_binding="Ctrl+Alt+R", is_global=True,
                        repeat_policy="ignore_repeat"))
        register(Action(id="transport.play_clip", name="Play Clip",
                        category="Transport",
                        callable=self._on_play_clip_clicked,
                        default_binding="Space",
                        repeat_policy="ignore_repeat"))

        self._wire_selection_sync()
        self._wire_controls()

        # Live audio polling @ ~30 Hz
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(33)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._refresh_source_names()
        self._update_buffer_duration_label()
        # Paint the initial default selection immediately rather than waiting
        # for the first tick (33ms).
        self._update_selection_display()
        # Paint the initial (empty) clip side so its labels/rings are consistent.
        self._refresh_clip_side()

        # ── Menu bar ─────────────────────────────────────────────────
        settings_menu = self.menuBar().addMenu("Settings")
        prefs_act = settings_menu.addAction("Preferences…")
        prefs_act.triggered.connect(self._open_preferences_dialog)
        keybindings_act = settings_menu.addAction("Keybindings…")
        keybindings_act.triggered.connect(self._open_keybindings_dialog)

        self._binding_table.load()
        # Migrate retired action ids: the explicit start/stop record actions are
        # no longer directly bindable, so fold any saved override onto the single
        # toggle (a user's old in-focus record key keeps working, now as a toggle
        # and visible in the dialog). Persist so the file stops carrying dead ids.
        if self._binding_table.remap_actions({
            "transport.start_recording": "transport.toggle_recording",
            "transport.stop_recording": "transport.toggle_recording",
        }):
            self._binding_table.save()

        # Drag-out export prefs — pool dir + bit depth used when rendering
        # a clip for an OS drag (see _render_for_drag).
        self._export_pool_dir: Path = load_export_pool_dir()
        self._export_bit_depth: str = load_export_bit_depth()

        # Global hotkeys (fire while minimized) — opt-in, Windows-only for now.
        self._global_hotkeys_enabled = load_global_hotkeys_enabled()
        self._global_hotkeys: GlobalHotkeySource | None = None
        self._apply_global_hotkeys(self._global_hotkeys_enabled)

        # Lazy-create status bar for surfacing non-modal messages.
        self.statusBar().showMessage("Ready", 0)

        # ── System tray ──────────────────────────────────────────────
        # Gated on availability (off under headless/offscreen Qt). When a
        # tray exists, closing the window hides to tray and keeps capturing;
        # the app only really exits via the tray's Quit.
        self._quitting = False
        self._close_to_tray = True
        self._bg_notice_shown = False
        self._show_notifications = load_show_notifications()
        # Per-source defensive-heal status, polled once a second.
        self._worst_sev = Severity.OK
        self._silent_secs: dict[str, float] = {}
        self._prev_xrun: dict[str, int] = {}
        self._prev_source_sev: dict[str, Severity] = {}
        self._last_poll_t: float | None = None
        self._tray: SystemTray | None = None
        if tray_supported():
            self._tray = SystemTray(
                is_recording=self._any_recording,
                source_count=self._recording_source_count,
                on_open=self._restore_window,
                on_quit=self._request_quit,
                on_settings=self._open_preferences_dialog,
                on_toggle_notifications=self._set_notifications_enabled,
                memory_bytes=self._state.total_project_ram_bytes,
                worst_severity=lambda: self._worst_sev,
                show_toasts=self._show_notifications,
                parent=self,
            )
            self._tray.show()
            # Keep capture alive when the last window is hidden/closed.
            QApplication.instance().setQuitOnLastWindowClosed(False)
        # Poll source health at 1 Hz — drives the in-app chip badges always,
        # and the tray ring/tooltip + error toasts when a tray exists.
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._poll_source_status)
        self._status_timer.start(1000)

    # ------------------------------------------------------------------
    # System-tray helpers
    # ------------------------------------------------------------------

    def _any_recording(self) -> bool:
        return any(slot.is_capturing() for slot in self._state.slots)

    def _recording_source_count(self) -> int:
        return sum(1 for slot in self._state.slots if slot.is_capturing())

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _request_quit(self) -> None:
        """Tray → Quit: tear down and exit (bypasses close-to-tray)."""
        self._quitting = True
        self.close()
        if self._tray is not None:
            self._tray.hide()
        QApplication.instance().quit()

    def _sync_tray(self) -> None:
        if self._tray is not None:
            self._tray.refresh()

    def _rename_slot(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        name, ok = QInputDialog.getText(
            self, "Rename source", "Source name:", text=slot.name
        )
        if ok and name.strip():
            slot.name = name.strip()
            self._refresh_source_names()

    # Per-source record gain — discrete dB steps (a slider can come with the
    # UX overhaul). Applied to slot.buffer.gain so it takes effect live and
    # the level meter / clip badge reflect the post-gain signal.
    _GAIN_STEPS = [
        ("Mute", float("-inf")), ("−12 dB", -12.0), ("−6 dB", -6.0),
        ("−3 dB", -3.0), ("0 dB (unity)", 0.0), ("+3 dB", 3.0),
        ("+6 dB", 6.0), ("+12 dB", 12.0),
    ]

    def _populate_gain_menu(self, menu: QMenu, slot) -> None:
        group = QActionGroup(menu)
        group.setExclusive(True)
        current = slot.buffer.gain_db
        for label, db in self._GAIN_STEPS:
            act = QAction(label, menu)
            act.setCheckable(True)
            if db == float("-inf"):
                act.setChecked(current == float("-inf"))
            else:
                act.setChecked(abs(current - db) < 0.5)
            act.triggered.connect(
                lambda _c=False, d=db, s=slot: setattr(s.buffer, "gain_db", d)
            )
            group.addAction(act)
            menu.addAction(act)

    def _evaluate_slot(self, slot, dt: float) -> SourceStatus:
        """Build a snapshot for one slot and evaluate its health. Advances the
        per-slot silent-duration / xrun trackers (keyed by the slot's stable
        id so removing a slot can't mis-attribute another's state). `dt` is the
        real elapsed seconds since the last poll. Call once per poll tick."""
        key = slot.id
        capturing = slot.is_capturing()
        level = 0.0
        if capturing:
            # The one call worth guarding — touches numpy + the seqlock.
            try:
                levels = slot.buffer.get_rms_levels(0.2)
                level = float(max(levels)) if len(levels) else 0.0
            except Exception:
                level = 0.0
        # A deliberately-muted source (record gain 0) is silent on purpose —
        # don't raise a "No signal" warning for it.
        muted = slot.buffer.gain == 0.0
        if capturing and not muted and level < _SILENCE_MAG:
            self._silent_secs[key] = self._silent_secs.get(key, 0.0) + dt
        else:
            self._silent_secs[key] = 0.0
        dur = slot.buffer.duration
        fill = slot.buffer.buffered_seconds / dur if dur else 0.0  # property, not a call
        xr = slot.xrun_count()
        rate = max(0, xr - self._prev_xrun.get(key, xr))
        self._prev_xrun[key] = xr
        return evaluate(SourceSnapshot(
            capturing=capturing, peak=level,
            silent_seconds=self._silent_secs[key],
            buffer_fill=fill, xrun_rate=float(rate), error=slot.last_error(),
        ))

    def _poll_source_status(self) -> None:
        """1 Hz: evaluate every source, drive the in-app chip badges, roll up
        the worst severity for the tray, and toast when a source enters an
        error. Trackers are pruned to live slots so they can't go stale."""
        now = time.monotonic()
        dt = (now - self._last_poll_t) if self._last_poll_t is not None else 1.0
        self._last_poll_t = now

        slots = self._state.slots
        live = {s.id for s in slots}
        for tracker in (self._silent_secs, self._prev_xrun, self._prev_source_sev):
            for stale in [k for k in tracker if k not in live]:
                del tracker[stale]

        statuses = [self._evaluate_slot(s, dt) for s in slots]
        for slot, st in zip(slots, statuses):
            key = slot.id
            prev = self._prev_source_sev.get(key, Severity.OK)
            if st.severity is Severity.ERROR and prev is not Severity.ERROR and self._tray:
                self._tray.notify(st.message, f"{slot.name}: {st.message}")
            self._prev_source_sev[key] = st.severity
        # In-app per-source badge (always); tray roll-up + tooltip (if present).
        self.nav_bar.set_source_severities([s.severity for s in statuses])
        self._worst_sev = worst(statuses).severity
        if self._tray is not None:
            self._tray.refresh()

    def _set_notifications_enabled(self, enabled: bool) -> None:
        """Single source of truth for the notifications pref — persists it
        and keeps the tray menu toggle in sync, whether the change came from
        the tray menu or the Preferences page."""
        enabled = bool(enabled)
        self._show_notifications = enabled
        save_show_notifications(enabled)
        if self._tray is not None:
            self._tray.set_notifications_enabled(enabled)

    def _apply_global_hotkeys(self, enabled: bool) -> None:
        """(Re)build or tear down the global-hotkey source to match the pref."""
        if self._global_hotkeys is not None:
            self._global_hotkeys.close()  # removes native filter + OS registrations
            self._global_hotkeys = None
        if enabled and global_hotkeys_supported():
            # Bindings are derived from the live BindingTable so a record/checkout
            # key the user rebinds tracks here too. Register to this window's HWND
            # so WM_HOTKEY is delivered reliably (thread-queue NULL-hwnd messages
            # don't reach Qt's native filter).
            bindings = build_global_bindings(self._binding_table)
            self._global_hotkeys = GlobalHotkeySource(bindings, int(self.winId()))
            n = self._global_hotkeys.registered_count
            total = len(bindings)
            if not total:
                msg = "Global hotkeys: no global-capable key is bound"
            elif not n:
                msg = "Global hotkeys: none registered (combo already in use?)"
            else:
                msg = f"Global hotkeys active ({n}/{total})"
            self.statusBar().showMessage(msg, 6000)

    def _set_global_hotkeys_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._global_hotkeys_enabled = enabled
        save_global_hotkeys_enabled(enabled)
        self._apply_global_hotkeys(enabled)

    def _set_export_pool_dir(self, path_str: str) -> None:
        self._export_pool_dir = Path(path_str)
        save_export_pool_dir(path_str)

    def _set_export_bit_depth(self, depth: str) -> None:
        self._export_bit_depth = depth
        save_export_bit_depth(depth)

    def _open_preferences_dialog(self) -> None:
        dlg = PreferencesDialog(
            show_notifications=self._show_notifications,
            on_notifications_changed=self._set_notifications_enabled,
            global_hotkeys_enabled=self._global_hotkeys_enabled,
            on_global_hotkeys_changed=self._set_global_hotkeys_enabled,
            global_hotkeys_supported=global_hotkeys_supported(),
            export_pool_dir=str(self._export_pool_dir),
            on_export_pool_dir_changed=self._set_export_pool_dir,
            export_bit_depth=self._export_bit_depth,
            on_export_bit_depth_changed=self._set_export_bit_depth,
            parent=self,
        )
        dlg.exec()

    def _wire_selection_sync(self) -> None:
        """When the user drags a selection on a waveform, snapshot the
        buffer's absolute sample indices so the selection rides the audio.
        Each tick, _update_selection_display converts those absolute
        positions back into display fractions for both the linear panel
        and the disc ring."""
        def on_buffer_sel(start: float, end: float) -> None:
            if not self._state.slots:
                return
            slot = self._state.active_slot
            buf = slot.buffer
            # While frozen, fractions resolve against the snapshot so a
            # drag on the static waveform lands on the audio the user
            # actually sees, not on the live-advancing buffer.
            total, buffered_s = self._effective_total_and_buffered(buf)
            if buffered_s <= 0 or end <= start:
                return
            sr = int(buf.sample_rate)
            samples_visible = int(buffered_s * sr)
            oldest_visible = total - samples_visible
            abs_start = oldest_visible + int(start * samples_visible)
            abs_end = oldest_visible + int(end * samples_visible)
            self._buffer_sel_abs = (abs_start, abs_end)
            self._buffer_sel_mode = "user"

        def on_buffer_clear() -> None:
            self._buffer_sel_abs = None
            self._buffer_sel_mode = "default"

        def on_clip_sel(start: float, end: float) -> None:
            # The clip panel shows a fixed snapshot of a checkout's audio,
            # not the live buffer — so the drag fractions are already the
            # correct trim bounds for that clip. Store them against the
            # checkout id and write them back to the Checkout's trim
            # samples so save/export pick them up automatically.
            co = self._currently_displayed_checkout()
            if co is None or end <= start:
                return
            self._clip_trim_fracs[co.id] = (float(start), float(end))
            n = int(co.audio.shape[0])
            co.trim_in_samples = max(0, int(start * n))
            co.trim_out_samples = max(co.trim_in_samples, int(end * n))

        def on_clip_clear() -> None:
            co = self._currently_displayed_checkout()
            if co is None:
                return
            self._clip_trim_fracs.pop(co.id, None)
            co.trim_in_samples = 0
            co.trim_out_samples = 0

        self.buffer_panel.waveform.manualSelectionChanged.connect(on_buffer_sel)
        self.buffer_panel.waveform.manualSelectionCleared.connect(on_buffer_clear)
        self.clip_panel.waveform.manualSelectionChanged.connect(on_clip_sel)
        self.clip_panel.waveform.manualSelectionCleared.connect(on_clip_clear)

        self.clip_panel.waveform.dragOutRequested.connect(self._on_clip_drag_out)
        self.clip_panel.waveform.dragFullClipRequested.connect(self._on_clip_drag_full)

        # The buffer deck deliberately gets no dragFullClipRequested
        # connection — "full clip" has no meaning on a rolling ring.
        self.buffer_panel.waveform.dragOutRequested.connect(self._on_buffer_drag_out)

    def _wire_controls(self) -> None:
        # Transport
        self.center_bridge.start_btn.clicked.connect(lambda: invoke("transport.start_recording"))
        self.center_bridge.stop_btn.clicked.connect(lambda: invoke("transport.stop_recording"))
        # FREEZE toggles the buffer-panel display without stopping
        # capture — see _on_freeze_toggled.
        freeze_btn = self.buffer_controls[-1]
        freeze_btn.toggled.connect(self._on_freeze_toggled)

        # Track selection on either turntable → update active slot
        self.buffer_turntable.track_selected.connect(self._on_track_selected)
        self.clip_turntable.track_selected.connect(self._on_track_selected)

        # OUT → check out current buffer selection as a new clip
        register(Action(id="clip.checkout", name="Checkout",
                        category="Clip",
                        callable=self._on_checkout_clicked,
                        default_binding="Ctrl+Alt+O", is_global=True,
                        repeat_policy="ignore_repeat"))
        self.out_btn.clicked.connect(lambda: invoke("clip.checkout"))

        # SAVE (clip side, last button in clip_controls) → save current clip
        save_btn = self.clip_controls[-1]
        register(Action(id="clip.save", name="Save Clip",
                        category="Clip",
                        callable=lambda: self._save_current_clip(),
                        repeat_policy="ignore_repeat"))
        save_btn.clicked.connect(lambda: invoke("clip.save"))

        # Buffer controls: [FLUSH, −, +, ◀, ▶, FREEZE]. FLUSH wipes the
        # active slot's buffer; − / + step the duration preset; ◀ / ▶
        # shift the anchor by half the current duration (older / newer);
        # FREEZE wiring lives separately in the block below.
        register(Action(id="buffer.flush_active", name="Flush Active Buffer",
                        category="Buffer",
                        callable=self._on_flush_active_buffer,
                        repeat_policy="ignore_repeat"))
        self.buffer_controls[0].clicked.connect(lambda: invoke("buffer.flush_active"))

        register(Action(id="buffer.duration_shorter", name="Shorter Buffer Duration",
                        category="Buffer",
                        callable=lambda: self._nudge_buffer_duration(-1),
                        repeat_policy="fire"))
        self.buffer_controls[1].clicked.connect(
            lambda: invoke("buffer.duration_shorter")
        )

        register(Action(id="buffer.duration_longer", name="Longer Buffer Duration",
                        category="Buffer",
                        callable=lambda: self._nudge_buffer_duration(+1),
                        repeat_policy="fire"))
        self.buffer_controls[2].clicked.connect(
            lambda: invoke("buffer.duration_longer")
        )

        register(Action(id="buffer.anchor_newer", name="Shift Buffer Anchor Newer",
                        category="Buffer",
                        callable=lambda: self._nudge_buffer_anchor(+1),
                        repeat_policy="fire"))
        self.buffer_controls[3].clicked.connect(
            lambda: invoke("buffer.anchor_newer")
        )

        register(Action(id="buffer.anchor_older", name="Shift Buffer Anchor Older",
                        category="Buffer",
                        callable=lambda: self._nudge_buffer_anchor(-1),
                        repeat_policy="fire"))
        self.buffer_controls[4].clicked.connect(
            lambda: invoke("buffer.anchor_older")
        )

        # Clip controls: [PLAY, −, +, ◀, ▶, SAVE]. PLAY toggles playback
        # of the trimmed clip; − / + tighten / expand the trim window
        # around its centre; ◀ / ▶ shift the whole trim earlier / later.
        self.clip_controls[0].clicked.connect(lambda: invoke("transport.play_clip"))

        register(Action(id="clip.trim_shorter", name="Tighten Clip Trim",
                        category="Clip",
                        callable=lambda: self._nudge_clip_trim_span(-0.05),
                        repeat_policy="fire"))
        self.clip_controls[1].clicked.connect(
            lambda: invoke("clip.trim_shorter")
        )

        register(Action(id="clip.trim_longer", name="Expand Clip Trim",
                        category="Clip",
                        callable=lambda: self._nudge_clip_trim_span(+0.05),
                        repeat_policy="fire"))
        self.clip_controls[2].clicked.connect(
            lambda: invoke("clip.trim_longer")
        )

        register(Action(id="clip.trim_earlier", name="Shift Clip Trim Earlier",
                        category="Clip",
                        callable=lambda: self._nudge_clip_trim_shift(-0.05),
                        repeat_policy="fire"))
        self.clip_controls[3].clicked.connect(
            lambda: invoke("clip.trim_earlier")
        )

        register(Action(id="clip.trim_later", name="Shift Clip Trim Later",
                        category="Clip",
                        callable=lambda: self._nudge_clip_trim_shift(+0.05),
                        repeat_policy="fire"))
        self.clip_controls[4].clicked.connect(
            lambda: invoke("clip.trim_later")
        )

        # Right-click on clip waveform → save/discard context menu
        self.clip_panel.waveform.contextMenuRequested.connect(
            self._on_clip_panel_context_menu
        )

        # NavBar actions
        register(Action(id="buffer.arm_all", name="Arm All Sources",
                        category="Buffer",
                        callable=self._on_arm_all,
                        repeat_policy="ignore_repeat"))
        self.nav_bar.arm_all_btn.clicked.connect(lambda: invoke("buffer.arm_all"))

        register(Action(id="buffer.add_source", name="Add Source",
                        category="Buffer",
                        callable=self._on_add_source,
                        repeat_policy="ignore_repeat"))
        self.nav_bar.add_source_btn.clicked.connect(lambda: invoke("buffer.add_source"))
        # Per-source chips in the NavBar — NavBar forwards per-chip
        # signals so the wiring stays valid even as chips are created
        # dynamically when the user adds more sources.
        self.nav_bar.chipClicked.connect(self._on_source_chip_clicked)
        self.nav_bar.chipContextMenuRequested.connect(
            self._on_source_chip_context_menu
        )
        self._refresh_source_indicators()

    def _open_keybindings_dialog(self) -> None:
        dialog = KeybindingsDialog(self._binding_table)
        if dialog.exec():
            # Rebind may have moved a global action's key — resync so the global
            # hotkeys stay in lockstep with the in-focus bindings. Pass the live
            # pref state; _apply_global_hotkeys no-ops cleanly when disabled, so
            # the resync invariant is "rebuild after any binding change", full stop.
            self._apply_global_hotkeys(self._global_hotkeys_enabled)

    def _on_start_clicked(self) -> None:
        started, err = self._state.start_rolling()
        if err is not None:
            QMessageBox.warning(self, "Start capture failed", str(err))
            return
        self._refresh_source_indicators()
        self._sync_tray()

    def _on_stop_clicked(self) -> None:
        self._state.stop_rolling()
        self._refresh_source_indicators()
        self._sync_tray()

    def _on_toggle_recording(self) -> None:
        """One key to start/stop — stops if anything is rolling, else starts.
        This is the bindable, global-capable record action."""
        if self._any_recording():
            self._on_stop_clicked()
        else:
            self._on_start_clicked()

    # ------------------------------------------------------------------
    # Buffer-side transport controls (− / + / ◀ / ▶ / FLUSH)
    # ------------------------------------------------------------------

    def _on_flush_active_buffer(self) -> None:
        """FLUSH button — wipe the currently active slot's buffer."""
        self._flush_slot_buffer(self._state.active_slot_index)

    @staticmethod
    def _active_duration_s(slot) -> float:
        """Return the duration in seconds of slot's current DEFAULT_PRESETS
        entry, clamped to a valid preset index."""
        idx = max(0, min(len(DEFAULT_PRESETS) - 1, int(slot.duration_preset_idx)))
        return float(DEFAULT_PRESETS[idx])

    @staticmethod
    def _checkout_has_trim(co) -> bool:
        """True if the checkout has a non-trivial trim window (either
        in-marker past 0 or out-marker before end)."""
        return (
            co.trim_in_samples > 0
            or (
                co.trim_out_samples > 0
                and co.trim_out_samples < co.audio.shape[0]
            )
        )

    def _reset_buffer_selection_to_default(self) -> None:
        """Clear any user-dragged buffer selection so the next tick
        rebuilds it from the slot's preset + anchor."""
        self._buffer_sel_mode = "default"
        self._buffer_sel_abs = None

    def _nudge_buffer_duration(self, step: int) -> None:
        """− (step=-1) / + (step=+1): step through DEFAULT_PRESETS to
        shrink or grow the default buffer selection window. Also
        re-clamps the anchor so the selection stays inside the
        buffered audio and updates the duration label in the header."""
        if not self._state.slots:
            return
        slot = self._state.active_slot
        new_idx = max(
            0,
            min(len(DEFAULT_PRESETS) - 1, int(slot.duration_preset_idx) + step),
        )
        slot.duration_preset_idx = new_idx
        self._clamp_buffer_anchor(slot)
        self._reset_buffer_selection_to_default()
        self._update_selection_display()
        self._update_buffer_duration_label()

    def _nudge_buffer_anchor(self, direction: int) -> None:
        """◀ (direction=+1, older) / ▶ (direction=-1, newer): shift the
        buffer selection's anchor by half the current duration so
        repeated presses walk through the audio without overlap.
        Anchor is clamped so the full selection stays inside the
        buffered range — pressing ◀ past the edge parks at the edge
        instead of collapsing the selection below its preset length."""
        if not self._state.slots:
            return
        slot = self._state.active_slot
        step_s = self._active_duration_s(slot) * 0.5
        slot.anchor_offset_s = float(slot.anchor_offset_s) + step_s * direction
        self._clamp_buffer_anchor(slot)
        self._reset_buffer_selection_to_default()
        self._update_selection_display()

    def _clamp_buffer_anchor(self, slot) -> None:
        """Keep anchor_offset in [0, buffered_s − duration_s] so that
        anchor + duration never exceeds the available buffered audio.
        If the duration is longer than what's buffered, anchor parks
        at 0 (selection is as wide as the buffer allows)."""
        duration_s = self._active_duration_s(slot)
        buffered_s = float(slot.buffer.buffered_seconds)
        max_anchor = max(0.0, buffered_s - duration_s)
        slot.anchor_offset_s = max(
            0.0, min(max_anchor, float(slot.anchor_offset_s))
        )

    def _update_buffer_duration_label(self) -> None:
        """Sync the buffer panel header's duration readout (the '3:00'
        next to the source name) with the active slot's current
        duration preset."""
        if not self._state.slots:
            return
        seconds = int(self._active_duration_s(self._state.active_slot))
        m, s = seconds // 60, seconds % 60
        self.buffer_panel.set_duration_text(f"{m}:{s:02d}")

    # ------------------------------------------------------------------
    # FREEZE — hold the buffer-panel display static while capture rolls
    # ------------------------------------------------------------------

    def _on_freeze_toggled(self, checked: bool) -> None:
        """Enter / leave frozen state. While frozen, the linear buffer
        waveform, time labels and timeline are pinned to the snapshot
        taken at freeze time. Capture continues — the radial ring keeps
        spinning — and drag selections on the frozen panel resolve
        against the snapshot so the user can dig out a clip from a
        static waveform without chasing a live one."""
        if checked and self._state.slots:
            buf = self._state.active_slot.buffer
            self._buffer_frozen_total = int(buf.total_written)
            self._buffer_frozen_buffered_s = float(buf.buffered_seconds)
            self._buffer_frozen = True
            self.buffer_controls[-1].setText("FROZEN")
        else:
            self._buffer_frozen = False
            self.buffer_controls[-1].setText("FREEZE")
        # Refresh selection display so fractions recompute against the
        # correct (frozen or live) total/buffered reference.
        self._update_selection_display()

    # ------------------------------------------------------------------
    # Clip-side transport controls (PLAY / − / + / ◀ / ▶)
    # ------------------------------------------------------------------

    def _on_play_clip_clicked(self) -> None:
        """Toggle playback of the currently displayed clip. Plays from
        the in-marker to the out-marker; if no trim is set, plays the
        full clip. Bound to both the PLAY button and the Space key."""
        co = self._currently_displayed_checkout()
        player = self._state.scrub_player
        if player.is_playing:
            player.pause()
            # Explicit stop by the user — suppress LOOP auto-restart.
            self._intending_playback = False
            self._refresh_play_button()
            return
        if co is None:
            return
        has_trim = self._checkout_has_trim(co)
        audio = co.trimmed_audio() if has_trim else co.audio
        try:
            player.bind(audio, co.sample_rate)
            player.play()
        except Exception as e:
            QMessageBox.warning(
                self, "Playback failed", f"Could not start playback:\n\n{e}"
            )
            return
        self._intending_playback = True
        self._last_playback_error_shown = None  # fresh attempt: allow re-showing a repeat error
        self._refresh_play_button()

    def _refresh_play_button(self) -> None:
        """Keep PLAY button label in sync with the scrub player state."""
        self.clip_controls[0].setText(
            "STOP" if self._state.scrub_player.is_playing else "PLAY"
        )

    def _nudge_clip_trim_span(self, delta_frac: float) -> None:
        """− (delta=-0.05): tighten the trim around its centre.
        + (delta=+0.05): loosen it outward. Clamped to [0, 1]."""
        co = self._currently_displayed_checkout()
        if co is None or co.audio.shape[0] == 0:
            return
        fracs = self._clip_trim_fracs.get(co.id)
        start, end = fracs if fracs is not None else (0.0, 1.0)
        half = delta_frac / 2.0
        new_start = max(0.0, min(1.0, start - half))
        new_end = max(0.0, min(1.0, end + half))
        if new_end <= new_start:
            return
        self._apply_clip_trim(co, new_start, new_end)

    def _nudge_clip_trim_shift(self, delta_frac: float) -> None:
        """◀ / ▶: slide the whole trim window earlier / later by
        delta_frac, preserving its width. Hits the edges rather than
        wrapping."""
        co = self._currently_displayed_checkout()
        if co is None or co.audio.shape[0] == 0:
            return
        fracs = self._clip_trim_fracs.get(co.id)
        if fracs is None:
            return
        start, end = fracs
        width = end - start
        new_start = max(0.0, min(1.0 - width, start + delta_frac))
        new_end = new_start + width
        self._apply_clip_trim(co, new_start, new_end)

    def _apply_clip_trim(self, co, start: float, end: float) -> None:
        """Persist a new trim range for the given checkout and reflect
        it in both the panel waveform and the ring selection arc."""
        self._clip_trim_fracs[co.id] = (start, end)
        n = int(co.audio.shape[0])
        co.trim_in_samples = max(0, int(start * n))
        co.trim_out_samples = max(co.trim_in_samples, int(end * n))
        self.clip_panel.waveform.blockSignals(True)
        self.clip_panel.waveform.set_manual_selection(start, end)
        self.clip_panel.waveform.blockSignals(False)
        self._update_selection_display()

    def _on_track_selected(self, index: int) -> None:
        sender = self.sender()
        if sender is self.clip_turntable:
            # Clip click: just show that clip in the panel, don't change slot
            if not self._state.slots:
                return
            checkouts = list(self._state.active_slot.checkout_manager.list())
            if 0 <= index < len(checkouts):
                self._display_clip_in_panel(checkouts[index], index, len(checkouts))
            return
        # Buffer click: change the active slot
        try:
            self._state.set_active_slot_index(index)
        except IndexError:
            return  # clicked a track beyond current slot count — ignore
        # Mirror the selection index on the clip side if it has that many rings
        if index < self.clip_turntable.track_count():
            self.clip_turntable.select_track(index)
        self._refresh_source_names()
        self._refresh_source_indicators()
        self._update_buffer_duration_label()
        # Reset selection state for the new active slot so it shows its own
        # default; abs-sample snapshots belong to the prior slot. Clip
        # trim fractions are keyed by checkout id so they survive the
        # switch and don't need clearing.
        self._reset_buffer_selection_to_default()
        self._update_selection_display()
        # Refresh clip side to show the new slot's checkouts
        self._refresh_clip_side()

    def _resolve_buffer_selection_abs(self, slot) -> tuple[int, int] | None:
        """Resolve the buffer deck's current selection — a manually drawn
        band ("user" mode) or the automatic anchor/duration window
        ("default" mode) — to an absolute sample range, clamped to what
        the ring still holds. Returns None when the range is empty or has
        scrolled out entirely. Shared by the OUT button and the buffer
        drag-out so both accept exactly the same selections."""
        buf = slot.buffer
        total = int(buf.total_written)
        sr = int(buf.sample_rate)
        if self._buffer_sel_mode == "user" and self._buffer_sel_abs is not None:
            abs_start, abs_end = self._buffer_sel_abs
        else:
            # Default mode — slot.duration_preset_idx + anchor_offset_s
            # from "now"
            duration_s = self._active_duration_s(slot)
            anchor_s = max(0.0, slot.anchor_offset_s)
            abs_end = total - int(anchor_s * sr)
            abs_start = abs_end - int(duration_s * sr)
        # Clamp abs_start to what's still in the ring
        oldest_available = max(0, total - buf.buffer_size)
        abs_start = max(abs_start, oldest_available)
        if abs_end <= abs_start:
            return None
        return (abs_start, abs_end)

    def _on_checkout_clicked(self) -> None:
        if not self._state.slots:
            return
        slot = self._state.active_slot

        sel_abs = self._resolve_buffer_selection_abs(slot)
        if sel_abs is None:
            QMessageBox.warning(
                self, "Check out failed",
                "Selection is empty or has already scrolled out of the buffer."
            )
            return
        abs_start, abs_end = sel_abs

        try:
            slot.checkout_manager.create_from_abs_range(abs_start, abs_end)
        except Exception as e:
            QMessageBox.warning(self, "Check out failed", str(e))
            return

        # Clear buffer selection → return to default behavior
        self._buffer_sel_mode = "default"
        self._buffer_sel_abs = None

        # Refresh clip side to include the new checkout; auto-select it
        self._refresh_clip_side(auto_select_newest=True)

    def _refresh_clip_side(self, auto_select_newest: bool = False) -> None:
        """Populate the clip_turntable rings and clip panel with checkouts
        from the ACTIVE slot. Newest checkout = outermost ring (highest index)."""
        if not self._state.slots:
            return
        slot = self._state.active_slot
        checkouts = list(slot.checkout_manager.list())  # oldest first
        n = len(checkouts)

        # Drop cached bins for checkouts that no longer exist. The cache
        # spans ALL slots (checkout ids are globally unique), so prune
        # against every slot's live checkouts — pruning against just the
        # active slot would wipe other slots' entries on every switch.
        live_ids = {
            c.id
            for s in self._state.slots
            for c in s.checkout_manager.list()
        }
        for stale in [k for k in self._clip_bins_cache if k not in live_ids]:
            del self._clip_bins_cache[stale]

        # Resize clip turntable. Keep a minimum of 1 ring for visual
        # consistency when there are no checkouts yet.
        self.clip_turntable.set_track_count(max(n, 1))

        # Clear out any track waveforms / selections / statuses that no
        # longer correspond to a checkout.
        self.clip_turntable._track_waveforms.clear()
        self.clip_turntable._track_selections.clear()

        if n == 0:
            # Nothing to render; force a repaint so cleared rings show.
            self.clip_turntable.update()
            self.clip_panel.waveform.set_data(
                np.zeros((1, 2, 1), dtype=np.float32)
            )
            self.clip_panel.set_source_name("CLIP")
            self.clip_panel.set_duration_text("")
            self.clip_panel.set_clip_id("")
            self.clip_panel.set_times("0:00.00", "0:00.00")
            return

        # Plot each checkout onto its ring (bins cached — audio is immutable).
        for j, co in enumerate(checkouts):
            entry = self._clip_bins_cache.setdefault(co.id, {})
            amp = entry.get("ring_amp")
            if amp is None:
                bins = _peak_bins_from_audio(co.audio, n_bins=540)
                amp = (
                    (bins[:, 1, :].max(axis=1) - bins[:, 0, :].min(axis=1)) / 2.0
                ).astype(np.float32)
                amp = np.clip(amp, 0.0, 1.0)
                entry["ring_amp"] = amp
            self.clip_turntable.set_track_waveform(j, amp, fill_fraction=1.0)

        # Decide which clip's waveform the clip PANEL displays
        if auto_select_newest:
            sel_idx = n - 1
        else:
            sel_idx = max(0, min(self.clip_turntable.selected_track(), n - 1))
        self.clip_turntable.select_track(sel_idx)
        self._display_clip_in_panel(checkouts[sel_idx], sel_idx, n)

    def _display_clip_in_panel(self, co, index: int, total: int) -> None:
        """Render a single checkout's full audio into the clip WaveformPanel."""
        entry = self._clip_bins_cache.setdefault(co.id, {})
        bins = entry.get("panel_bins")
        if bins is None:
            bins = _peak_bins_from_audio(co.audio, n_bins=360)
            entry["panel_bins"] = bins
        self.clip_panel.waveform.set_data(bins)
        # Clip timeline = fixed clip duration; lets the selection band
        # render its duration label on top.
        if co.duration_seconds > 0:
            self.clip_panel.waveform.set_timeline(
                float(co.duration_seconds), anchor="left"
            )
        slot_name = (
            self._state.active_slot.name if self._state.slots else "?"
        )
        self.clip_panel.set_source_name(
            f"{slot_name.upper()} #{index + 1}/{total}"
        )
        self.clip_panel.set_duration_text(f"{co.duration_seconds:.1f}s")
        self.clip_panel.set_clip_id(co.id[:6].upper())
        self.clip_panel.set_times("0:00.00", f"{co.duration_seconds:.2f}s")
        # Restore any saved trim selection for this clip so the band stays
        # anchored to the audio when switching clips or reopening the app.
        fracs = self._clip_trim_fracs.get(co.id)
        self.clip_panel.waveform.blockSignals(True)
        if fracs is None:
            self.clip_panel.waveform.clear_manual_selection()
        else:
            self.clip_panel.waveform.set_manual_selection(*fracs)
        self.clip_panel.waveform.blockSignals(False)

    def _currently_displayed_checkout(self):
        """Return the Checkout object whose audio the clip panel is
        currently showing, or None if there are no checkouts yet."""
        if not self._state.slots:
            return None
        checkouts = list(self._state.active_slot.checkout_manager.list())
        if not checkouts:
            return None
        idx = max(0, min(self.clip_turntable.selected_track(), len(checkouts) - 1))
        return checkouts[idx]

    def _suggested_clip_filename(self, slot, co, suffix: str = "") -> str:
        base = f"flashback_{sanitize_source_name(slot.name)}_{co.id}"
        if suffix:
            base += f"_{suffix}"
        return base

    def _default_clip_save_dir(self):
        return Path.home() / "Documents"

    def _save_current_clip(self, fmt: str | None = None, trimmed: bool = True) -> None:
        """Prompt for a target path and save the clip currently shown in
        the clip panel. When `trimmed` is True, uses the stored trim
        fracs (set via drag on the clip waveform)."""
        co = self._currently_displayed_checkout()
        if co is None:
            QMessageBox.information(self, "Save", "No clip to save.")
            return
        slot = self._state.active_slot
        suffix = "trim" if trimmed and self._clip_trim_fracs.get(co.id) else ""
        default_ext = ".flac" if fmt == "FLAC" else ".wav"
        filter_spec = (
            "WAV audio (*.wav);;FLAC audio (*.flac)" if fmt is None
            else ("WAV audio (*.wav)" if fmt == "WAV" else "FLAC audio (*.flac)")
        )
        default_path = str(
            self._default_clip_save_dir()
            / f"{self._suggested_clip_filename(slot, co, suffix)}{default_ext}"
        )
        target, selected = QFileDialog.getSaveFileName(
            self, "Save clip", default_path, filter_spec
        )
        if not target:
            return
        # Resolve format from filter choice or file extension
        if fmt is None:
            resolved = "FLAC" if (
                selected.startswith("FLAC") or target.lower().endswith(".flac")
            ) else "WAV"
        else:
            resolved = fmt
        try:
            slot.checkout_manager.save(
                co.id, Path(target), fmt=resolved, trimmed=trimmed
            )
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.statusBar().showMessage(f"Saved {Path(target).name}", 4000)

    def _render_for_drag(self, slot, co, trimmed: bool):
        """Render `co` into the export pool; returns the path or None on
        failure (already reported to the user)."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return render_drag_file(
                slot.checkout_manager,
                co.id,
                self._export_pool_dir,
                slot.name,
                bit_depth=self._export_bit_depth,
                trimmed=trimmed,
            )
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return None
        finally:
            QApplication.restoreOverrideCursor()

    def _on_clip_drag_out(self, start_frac: float, end_frac: float) -> None:
        # The clip selection IS the trim (kept in sync by on_clip_sel),
        # so dragging the band exports the trimmed range.
        self._drag_current_clip(trimmed=True)

    def _on_clip_drag_full(self) -> None:
        self._drag_current_clip(trimmed=False)

    def _drag_current_clip(self, trimmed: bool) -> None:
        co = self._currently_displayed_checkout()
        if co is None:
            return
        slot = self._state.active_slot
        path = self._render_for_drag(slot, co, trimmed)
        if path is None:
            return
        self._complete_drag(
            slot, co, path, self.clip_panel.waveform,
            discard_on_cancel=False, auto_select_newest=False,
        )

    def _complete_drag(
        self,
        slot,
        co,
        path,
        source_widget,
        *,
        discard_on_cancel: bool,
        auto_select_newest: bool,
    ) -> None:
        """Shared tail of both decks' drag-out flows: run the blocking OS
        drag, then commit (mark saved + refresh) or roll back (delete the
        just-rendered file; discard the checkout too when it was created
        just for this drag)."""
        if perform_file_drag(source_widget, path):
            try:
                slot.checkout_manager.mark_saved(co.id)
            except KeyError:
                # Checkout was discarded while the drag loop ran; the
                # exported file is still valid — nothing to flip.
                pass
            self._refresh_clip_side(auto_select_newest=auto_select_newest)
            self.statusBar().showMessage(f"Exported {path.name}", 4000)
        else:
            if discard_on_cancel:
                slot.checkout_manager.discard(co.id)
            path.unlink(missing_ok=True)

    def _on_buffer_drag_out(self, start_frac: float, end_frac: float) -> None:
        """Snipe the current buffer selection straight out of the app:
        implicit checkout → render → OS drag. On accept the checkout
        stays on the clip deck as `saved` (the pool + deck form the
        sample bank); on cancel it is discarded.

        The buffer selection deliberately survives a successful drag so
        the same slice can be dragged onto several DAW tracks in a row;
        each repeat mints a new saved checkout + pool file by design.

        Works in both selection modes: a manually drawn band, or the
        automatic anchor/duration window painted in "default" mode —
        the same range the OUT button would check out."""
        slot = self._state.active_slot
        sel_abs = self._resolve_buffer_selection_abs(slot)
        if sel_abs is None:
            self.statusBar().showMessage(
                "Drag-out failed: selection is empty or has already "
                "scrolled out of the buffer.", 4000,
            )
            return
        co = None
        while co is None:
            try:
                co = slot.checkout_manager.create_from_abs_range(*sel_abs)
            except (RuntimeError, ValueError) as e:
                # At the active-checkout / RAM cap, make room by evicting
                # the oldest `saved` clip — its pool file is the durable
                # record. Any other failure (range lapped, etc.) reports.
                at_cap = (
                    "Maximum active checkouts" in str(e)
                    or "RAM cap" in str(e)
                )
                if not (at_cap and self._evict_oldest_saved_checkout(slot)):
                    self.statusBar().showMessage(f"Drag-out failed: {e}", 4000)
                    return
        path = self._render_for_drag(slot, co, trimmed=True)
        if path is None:
            slot.checkout_manager.discard(co.id)
            return
        self._complete_drag(
            slot, co, path, self.buffer_panel.waveform,
            discard_on_cancel=True, auto_select_newest=True,
        )

    def _evict_oldest_saved_checkout(self, slot) -> bool:
        """Discard the oldest checkout in `saved` state to make room for
        a new drag checkout. Saved clips live on durably as their export
        pool file; `pending` clips are the user's working set and are
        never evicted. Returns False when nothing was evictable."""
        for co in slot.checkout_manager.list():  # oldest first
            if co.state == "saved":
                slot.checkout_manager.discard(co.id)
                self._clip_trim_fracs.pop(co.id, None)
                self._clip_bins_cache.pop(co.id, None)
                return True
        return False

    def _discard_current_clip(self) -> None:
        co = self._currently_displayed_checkout()
        if co is None:
            return
        try:
            self._state.active_slot.checkout_manager.discard(co.id)
        except Exception as e:
            QMessageBox.warning(self, "Discard failed", str(e))
            return
        self._clip_trim_fracs.pop(co.id, None)
        self._refresh_clip_side()

    def _on_clip_panel_context_menu(self, global_pos) -> None:
        co = self._currently_displayed_checkout()
        if co is None:
            return
        has_trim = self._clip_trim_fracs.get(co.id) is not None
        menu = QMenu(self)
        act_save_wav = QAction(
            "Save trimmed as WAV…" if has_trim else "Save as WAV…", self
        )
        act_save_wav.triggered.connect(
            lambda: self._save_current_clip(fmt="WAV", trimmed=has_trim)
        )
        menu.addAction(act_save_wav)
        act_save_flac = QAction(
            "Save trimmed as FLAC…" if has_trim else "Save as FLAC…", self
        )
        act_save_flac.triggered.connect(
            lambda: self._save_current_clip(fmt="FLAC", trimmed=has_trim)
        )
        menu.addAction(act_save_flac)
        if has_trim:
            menu.addSeparator()
            act_save_full_wav = QAction("Save full clip as WAV…", self)
            act_save_full_wav.triggered.connect(
                lambda: self._save_current_clip(fmt="WAV", trimmed=False)
            )
            menu.addAction(act_save_full_wav)
            act_clear_trim = QAction("Clear trim selection", self)
            def _clear():
                self._clip_trim_fracs.pop(co.id, None)
                co.trim_in_samples = 0
                co.trim_out_samples = 0
                self.clip_panel.waveform.clear_manual_selection()
                self._update_selection_display()
            act_clear_trim.triggered.connect(_clear)
            menu.addAction(act_clear_trim)
        menu.addSeparator()
        act_discard = QAction("Discard clip", self)
        act_discard.triggered.connect(self._discard_current_clip)
        menu.addAction(act_discard)

        qpt = QPoint(int(global_pos.x()), int(global_pos.y()))
        menu.exec(qpt)

    def _on_arm_all(self) -> None:
        for slot in self._state.slots:
            slot.armed = True
            self._sync_slot_capture_to_armed(slot)
        self._refresh_source_indicators()

    def _on_source_chip_clicked(self, slot_idx: int) -> None:
        if not (0 <= slot_idx < len(self._state.slots)):
            return
        slot = self._state.slots[slot_idx]
        slot.armed = not slot.armed
        self._sync_slot_capture_to_armed(slot)
        self._refresh_source_indicators()

    def _sync_slot_capture_to_armed(self, slot) -> None:
        """If the global transport is rolling, bring this slot's capture
        state in line with its armed flag without touching others —
        arming starts its capture, unarming pauses it."""
        if not self._state.rolling:
            return
        try:
            if slot.armed and not slot.is_capturing():
                source = self._state.build_capture_for_slot(slot)
                slot.bind_capture(source)
                slot.start_capture()
            elif not slot.armed and slot.is_capturing():
                slot.stop_capture()
        except Exception as e:
            QMessageBox.warning(
                self,
                "Capture state change failed",
                f"Could not update capture on {slot.name!r}:\n\n{e}",
            )

    def _finalize_add_source(self, new_idx: int) -> None:
        """Refresh visuals after a slot has been appended. Also start the
        newly-added slot's capture immediately if the transport is
        already rolling and the new slot is armed by default."""
        n = len(self._state.slots)
        self.buffer_turntable.set_track_count(n)
        self._refresh_source_indicators()
        self._refresh_source_names()
        self._refresh_clip_side()
        if 0 <= new_idx < n:
            self._sync_slot_capture_to_armed(self._state.slots[new_idx])
            self._refresh_source_indicators()

    def _on_add_source(self) -> None:
        """Open the Configure Source dialog. The dialog carries the
        capture-source picker ("SOURCE INPUT" field) so the user chooses
        device / process / default in the same pass as name and buffer.
        No intermediate menu."""
        from flashback_sampler.app.add_source_dialog import AddSourceDialog
        active = self._state.active_slot
        default_name = f"Source {len(self._state.slots) + 1}"
        default_buffer_s = active.buffer_seconds
        max_buffer_s = max(3600.0, default_buffer_s * 4)
        dlg = AddSourceDialog(
            default_name=default_name,
            default_buffer_seconds=default_buffer_s,
            max_buffer_seconds=max_buffer_s,
            default_sample_rate=active.sample_rate,
            default_channels=active.channels,
            parent=self,
        )
        if dlg.exec() != AddSourceDialog.Accepted:
            return
        preset = dlg.result_preset()
        if preset is None:
            return
        name = dlg.result_name() or default_name
        device = dlg.result_device()
        preset = self._probe_and_notify(preset, device)
        try:
            self._state.add_slot(preset, name=name)
        except Exception as e:
            QMessageBox.warning(self, "Add source failed", str(e))
            return
        new_idx = len(self._state.slots) - 1
        if device is not None and 0 <= new_idx < len(self._state.slots):
            self._state.slots[new_idx].capture_spec = device
        self._finalize_add_source(new_idx)

    def _probe_and_notify(self, preset, device):
        """Rate-probe the requested preset against the chosen device;
        show the honest-fallback notice when the rate was adjusted."""
        adjusted, notice = apply_rate_probe(preset, device)
        if notice:
            QMessageBox.information(self, "Sample rate adjusted", notice)
        return adjusted

    def _refresh_source_indicators(self) -> None:
        """Update the NavBar source chips to reflect current slot armed/capturing state."""
        for i, chip in enumerate(self.nav_bar.source_slots):
            if i >= len(self._state.slots):
                chip.set_status("inactive")
                continue
            slot = self._state.slots[i]
            if self._state.rolling and slot.armed:
                chip.set_status("armed")
            elif slot.armed:
                chip.set_status("paused")   # armed but not rolling yet
            else:
                chip.set_status("inactive")

    def _effective_total_and_buffered(self, buf) -> tuple[int, float]:
        """Return (total_written, buffered_seconds) pinned to the
        freeze snapshot when the buffer panel is frozen; live values
        otherwise. Used by selection-fraction math so dragging on a
        frozen waveform targets the audio the user actually sees."""
        if self._buffer_frozen:
            return (self._buffer_frozen_total, self._buffer_frozen_buffered_s)
        return (int(buf.total_written), float(buf.buffered_seconds))

    def _update_selection_display(self) -> None:
        """Convert stored absolute sample positions (or default anchor /
        duration when in "default" mode) into current display fractions
        and apply them to both the linear WaveformPanel and the matching
        disc ring. Called every tick so user selections ride the audio as
        the buffer advances."""
        if not self._state.slots:
            return
        slot = self._state.active_slot
        buf = slot.buffer
        total, buffered_s = self._effective_total_and_buffered(buf)
        if buffered_s <= 0:
            # Nothing to show yet
            return
        sr = int(buf.sample_rate)

        def compute_fracs_buffer_side() -> tuple[float, float] | None:
            if self._buffer_sel_mode == "default":
                duration_s = self._active_duration_s(slot)
                anchor_s = max(0.0, slot.anchor_offset_s)
                end_ago = anchor_s
                start_ago = anchor_s + duration_s
            else:  # user
                if self._buffer_sel_abs is None:
                    return None
                abs_start, abs_end = self._buffer_sel_abs
                start_ago = (total - abs_start) / sr
                end_ago = (total - abs_end) / sr
            end_frac = 1.0 - end_ago / buffered_s
            start_frac = 1.0 - start_ago / buffered_s
            # Clamp to visible range
            end_frac = max(0.0, min(1.0, end_frac))
            start_frac = max(0.0, min(1.0, start_frac))
            if end_frac <= start_frac:
                return None
            return (start_frac, end_frac)

        def compute_fracs_clip_side() -> tuple[float, float] | None:
            # Clip selection lives in clip-local fractions — immutable
            # with respect to the advancing buffer. Look up by the id of
            # the currently displayed clip.
            co = self._currently_displayed_checkout()
            if co is None:
                return None
            fracs = self._clip_trim_fracs.get(co.id)
            if fracs is None:
                return None
            return fracs

        def apply(fracs: tuple[float, float] | None, color: str,
                  panel, turntable) -> None:
            # The ring that the selection belongs to depends on the side:
            # buffer rings mirror slot indices, clip rings mirror checkout
            # indices (whichever clip is currently focused on the panel).
            if turntable is self.clip_turntable:
                idx = self.clip_turntable.selected_track()
            else:
                idx = self._state.active_slot_index
            interacting = panel.waveform.is_user_interacting()
            if fracs is None:
                if not interacting:
                    panel.waveform.blockSignals(True)
                    panel.waveform.clear_manual_selection()
                    panel.waveform.blockSignals(False)
                turntable.set_track_selection(idx, None, None, color)
                return
            s, e = fracs
            if not interacting:
                panel.waveform.blockSignals(True)
                panel.waveform.set_manual_selection(s, e)
                panel.waveform.blockSignals(False)
            turntable.set_track_selection(idx, s, e, color)

        apply(compute_fracs_buffer_side(), SELECTION_COLOR_BUFFER,
              self.buffer_panel, self.buffer_turntable)
        apply(compute_fracs_clip_side(), SELECTION_COLOR_CLIP,
              self.clip_panel, self.clip_turntable)

    def _refresh_source_names(self) -> None:
        """Propagate slot names from state into NavBar chips and the active
        waveform panel's source label."""
        names = [slot.name for slot in self._state.slots]
        self.nav_bar.set_source_names(names)
        # Buffer panel shows the currently-active slot's name
        active_name = (
            self._state.active_slot.name if self._state.slots else "SOURCE 1"
        )
        self.buffer_panel.set_source_name(active_name.upper())
        # Clip panel's source label is set by _display_clip_in_panel to
        # "<slot_name> #<idx>/<total>" so it reflects the current clip,
        # not the active slot — nothing to do here.

    # ------------------------------------------------------------------
    # Live audio polling
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Pull peak-bin data from each slot's buffer and push into UI.
        Active slot drives the buffer WaveformPanel; each slot's bins
        also go to its corresponding track ring as a radial plot."""
        slots = self._state.slots
        active_idx = self._state.active_slot_index

        # Active slot → buffer panel's linear waveform view + timestamps.
        # While frozen, the panel stays pinned to the snapshot taken at
        # freeze time — the ring below still updates live.
        if 0 <= active_idx < len(slots) and not self._buffer_frozen:
            active_buf = slots[active_idx].buffer
            try:
                bins = active_buf.get_peak_bins(
                    seconds=active_buf.duration, n_bins=360
                )
                self.buffer_panel.waveform.set_data(bins)
            except Exception:
                pass  # capture may not be running yet
            # Update buffer panel time labels from real buffered_seconds
            try:
                buffered_s = float(active_buf.buffered_seconds)
                left = format_time_signed_cs(-buffered_s)
                right = "0:00.00"
                self.buffer_panel.set_times(left, right)
                # Feed the waveform's total duration so the selection
                # band can render the in/out duration label on top.
                if buffered_s > 0:
                    self.buffer_panel.waveform.set_timeline(
                        buffered_s, anchor="right"
                    )
            except Exception:
                pass

        # Each slot → its ring on the buffer turntable as a radial plot
        for i, slot in enumerate(slots):
            if i >= self.buffer_turntable.track_count():
                break
            try:
                buffered_s = slot.buffered_seconds()
                capacity_s = slot.buffer.duration
                fill_frac = 0.0
                if capacity_s > 0:
                    fill_frac = max(0.0, min(1.0, buffered_s / capacity_s))
                if fill_frac < 1e-4:
                    # No data yet — clear the ring waveform
                    self.buffer_turntable.set_track_waveform(
                        i, np.zeros(0, dtype=np.float32), fill_fraction=0.0
                    )
                    continue
                # Always request 360 bins against a capacity-based bin
                # grid. This pins each summary slot's bin assignment for
                # its lifetime: historical bars are computed once and
                # never re-aggregated as more audio arrives. Only the
                # bin holding the actively-growing newest slot changes
                # tick-to-tick. Without the fixed grid, both `n_samples`
                # and `n_bins` grow during fill — `bin_span` drifts and
                # each slot gets periodically reassigned to a different
                # bin, which shows up as the whole ring visibly pulsing.
                capacity_samples = slot.buffer.buffer_size
                display_n = 360
                rms = slot.buffer.get_summary_bins(
                    n_bins=display_n,
                    bin_span_samples=max(1, capacity_samples // display_n),
                )
                amp_full = np.clip(
                    rms.max(axis=1) * 3.0, 0.0, 1.0
                ).astype(np.float32)
                # Trim to the currently-filled bars. Oldest is bar 0,
                # newest sits just before the unfilled tail.
                n_filled = max(1, int(round(fill_frac * display_n)))
                amp = amp_full[:n_filled]
                self.buffer_turntable.set_track_waveform(i, amp, fill_fraction=fill_frac)
            except Exception:
                pass

        # Recompute selection fractions from stored abs samples (or defaults)
        # so user selections drift with audio and the radial arc updates to
        # the current fill fraction.
        self._update_selection_display()

        # Playhead + PLAY button + LOOP restart.
        self._update_clip_playback_state()

    def _update_clip_playback_state(self) -> None:
        """Drive the clip panel's playhead from the scrub player's
        cursor, keep PLAY / STOP label in sync, and auto-rewind when
        the LOOP button is toggled on and playback has drained."""
        player = self._state.scrub_player
        co = self._currently_displayed_checkout()
        # Playhead: express the current cursor position as a fraction
        # of the FULL clip audio so the playhead lines up with the
        # displayed waveform (not with the trimmed slice).
        if (
            co is not None
            and player.is_playing
            and co.audio.shape[0] > 0
        ):
            cursor_samples = int(player.cursor_samples)
            has_trim = self._checkout_has_trim(co)
            base_offset = co.trim_in_samples if has_trim else 0
            abs_sample = base_offset + cursor_samples
            frac = max(
                0.0, min(1.0, abs_sample / float(co.audio.shape[0]))
            )
            self.clip_panel.waveform.set_playhead(frac)
        else:
            self.clip_panel.waveform.set_playhead(None)
            just_stopped = (
                co is not None
                and not player.is_playing
                and getattr(self, "_was_playing_last_tick", False)
            )
            if just_stopped:
                # The native player opens its output device lazily on
                # the Zig render thread: an open failure never raises
                # here, it arrives later as last_error() plus playing
                # flipping back to 0. Show it once per distinct error —
                # this method runs every tick and would otherwise pop
                # the same dialog every ~100ms.
                err = player.last_error()
                if err and err != self._last_playback_error_shown:
                    self._last_playback_error_shown = err
                    QMessageBox.warning(
                        self, "Playback failed", f"Could not start playback:\n\n{err}"
                    )
            # LOOP: if checked, the user hasn't explicitly stopped,
            # a clip is bound, and playback just drained, restart.
            # Gating on _intending_playback keeps STOP-while-LOOPing
            # from immediately re-triggering playback.
            if self.loop_btn.isChecked() and self._intending_playback and just_stopped:
                try:
                    player.play()
                    self._last_playback_error_shown = None
                except Exception:
                    pass
        self._was_playing_last_tick = bool(player.is_playing)
        self._refresh_play_button()

    def closeEvent(self, event) -> None:
        # Close-to-tray: hide and keep capturing instead of tearing down,
        # unless the user explicitly quit (tray → Quit) or there's no tray.
        if not self._quitting and self._tray is not None and self._close_to_tray:
            event.ignore()
            self.hide()
            if not self._bg_notice_shown:
                self._bg_notice_shown = True
                self._tray.notify(
                    "Still running in the background",
                    "flashback-sampler keeps capturing. Right-click the tray "
                    "icon to stop or quit.",
                )
            return
        self._tick_timer.stop()
        self._status_timer.stop()  # stop polling source health before teardown
        if self._global_hotkeys is not None:
            self._global_hotkeys.close()  # release OS hotkey registrations
        try:
            self._state.scrub_player.pause()
        except Exception:
            pass
        self._state.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Per-source context menu
    # ------------------------------------------------------------------

    def _switch_to_slot(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        try:
            self._state.set_active_slot_index(slot_index)
        except IndexError:
            return
        # Mirror on both turntables
        if slot_index < self.buffer_turntable.track_count():
            self.buffer_turntable.select_track(slot_index)
        if slot_index < self.clip_turntable.track_count():
            self.clip_turntable.select_track(slot_index)
        self._refresh_source_names()
        # Reset buffer selection for the new active slot; clip-side
        # trims are keyed by checkout id and survive slot switches.
        self._reset_buffer_selection_to_default()
        self._update_selection_display()
        self._refresh_clip_side()
        self._refresh_source_indicators()
        self._update_buffer_duration_label()

    def _on_source_chip_context_menu(
        self, slot_index: int, global_pos: QPoint
    ) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        can_remove = len(self._state.slots) > 1
        has_buffered = slot.buffered_seconds() > 0.1

        menu = QMenu(self)

        switch_act = QAction(f"Switch to {slot.name}", self)
        switch_act.setEnabled(slot_index != self._state.active_slot_index)
        switch_act.triggered.connect(
            lambda _c=False, i=slot_index: self._switch_to_slot(i)
        )
        menu.addAction(switch_act)

        prime_label = "Stop Recording" if slot.is_capturing() else "Start Recording"
        prime_act = QAction(prime_label, self)
        prime_act.triggered.connect(
            lambda _c=False, i=slot_index: self._on_source_chip_clicked(i)
        )
        menu.addAction(prime_act)

        rename_act = QAction("Rename…", self)
        rename_act.triggered.connect(
            lambda _c=False, i=slot_index: self._rename_slot(i)
        )
        menu.addAction(rename_act)

        menu.addSeparator()

        # Select Source Input(s) — mirrors the AddSourceDialog's
        # SOURCE INPUT menu (Default / From Device… / From Process…)
        # so the add-source flow and the per-slot reroute flow feel
        # identical. Device list hidden behind a sub-submenu instead
        # of inlined so the top-level stays short.
        src_menu = menu.addMenu("Select Source Input(s)")
        self._populate_slot_capture_source_menu(src_menu, slot_index)

        gain_menu = menu.addMenu("Record Gain")
        self._populate_gain_menu(gain_menu, slot)

        menu.addSeparator()

        flush_act = QAction("Flush Buffer…", self)
        flush_act.setEnabled(has_buffered)
        flush_act.triggered.connect(
            lambda _c=False, i=slot_index: self._flush_slot_buffer(i)
        )
        menu.addAction(flush_act)

        menu.addSeparator()

        remove_act = QAction("Remove Source…", self)
        remove_act.setEnabled(can_remove)
        remove_act.triggered.connect(
            lambda _c=False, i=slot_index: self._remove_slot_with_confirmation(i)
        )
        menu.addAction(remove_act)

        qpt = QPoint(int(global_pos.x()), int(global_pos.y()))
        menu.exec(qpt)

    def _populate_slot_capture_source_menu(
        self, src_menu: QMenu, slot_index: int
    ) -> None:
        """Build the 'Select Source Input(s)' submenu for one slot.
        Structure matches AddSourceDialog's SOURCE INPUT menu: Default,
        From Device… (nested device list), From Process… (picker),
        plus Add Another Input (mux) and, when applicable, a summary
        of the slot's current muxed inputs."""
        slot = self._state.slots[slot_index]
        muxed = list(slot.capture_specs)
        is_mux = len(muxed) >= 2
        is_single = len(muxed) == 1
        current_single = muxed[0] if is_single else None

        group = QActionGroup(src_menu)
        group.setExclusive(True)

        global_default = QAction("Default (global)", src_menu)
        global_default.setCheckable(True)
        global_default.setChecked(not muxed)
        global_default.triggered.connect(
            lambda _c=False, i=slot_index: self._set_slot_capture_spec(i, None)
        )
        group.addAction(global_default)
        src_menu.addAction(global_default)

        src_menu.addSeparator()

        # From Device… — nested submenu keeps the top level short.
        dev_menu = src_menu.addMenu("From Device…")
        devices = list_capture_devices()
        if not devices:
            hint = QAction("(no capture devices)", dev_menu)
            hint.setEnabled(False)
            dev_menu.addAction(hint)
        else:
            for dev in devices:
                label = dev.name + ("   [default]" if dev.is_default else "")
                act = QAction(label, dev_menu)
                act.setCheckable(True)
                if (
                    current_single is not None
                    and current_single.kind == dev.kind
                    and current_single.id == dev.id
                ):
                    act.setChecked(True)
                group.addAction(act)
                dev_menu.addAction(act)
                act.triggered.connect(
                    lambda _c=False, d=dev, i=slot_index: self._set_slot_capture_spec(i, d)
                )

        proc_act = QAction("From Process…", src_menu)
        proc_act.triggered.connect(
            lambda _c=False, i=slot_index: self._pick_process_for_slot(i)
        )
        src_menu.addAction(proc_act)

        src_menu.addSeparator()

        # Mux — add more inputs to this slot so they share its buffer.
        add_mux_menu = src_menu.addMenu("Add Another Input (mux)…")
        mux_dev_menu = add_mux_menu.addMenu("From Device…")
        if not devices:
            hint = QAction("(no capture devices)", mux_dev_menu)
            hint.setEnabled(False)
            mux_dev_menu.addAction(hint)
        else:
            for dev in devices:
                label = dev.name + ("   [default]" if dev.is_default else "")
                act = QAction(label, mux_dev_menu)
                act.triggered.connect(
                    lambda _c=False, d=dev, i=slot_index: self._add_mux_input_to_slot(i, d)
                )
                mux_dev_menu.addAction(act)
        mux_proc_act = QAction("From Process…", add_mux_menu)
        mux_proc_act.triggered.connect(
            lambda _c=False, i=slot_index: self._add_mux_input_from_process(i)
        )
        add_mux_menu.addAction(mux_proc_act)

        if is_mux:
            src_menu.addSeparator()
            hdr = QAction(f"Muxed Inputs ({len(muxed)})", src_menu)
            hdr.setEnabled(False)
            src_menu.addAction(hdr)
            for idx, dev in enumerate(muxed):
                remove_act = QAction(f"Remove: {dev.name}", src_menu)
                remove_act.triggered.connect(
                    lambda _c=False, i=slot_index, j=idx: self._remove_mux_input(i, j)
                )
                src_menu.addAction(remove_act)

    def _pick_process_for_slot(self, slot_index: int) -> None:
        """Open the ProcessPickerDialog and, on accept, set the slot's
        capture_spec to a per-process CaptureDevice."""
        dlg = ProcessPickerDialog(parent=self)
        if dlg.exec() != ProcessPickerDialog.Accepted:
            return
        device = dlg.result_device()
        if device is None:
            return
        self._set_slot_capture_spec(slot_index, device)

    def _add_mux_input_from_process(self, slot_index: int) -> None:
        dlg = ProcessPickerDialog(parent=self)
        if dlg.exec() != ProcessPickerDialog.Accepted:
            return
        device = dlg.result_device()
        if device is None:
            return
        self._add_mux_input_to_slot(slot_index, device)

    def _add_mux_input_to_slot(
        self, slot_index: int, device: CaptureDevice
    ) -> None:
        """Append another capture input to the slot so its samples are
        summed into the same ring buffer. Shows a first-time warning
        explaining the bitrate/loudness tradeoff. Restarts the slot's
        capture so the new mux takes effect immediately."""
        if not (0 <= slot_index < len(self._state.slots)):
            return
        if not self._confirm_mux_first_time():
            return
        slot = self._state.slots[slot_index]
        # If the slot was following the global default (no local specs),
        # prepend that global spec so the first click becomes a 2-input
        # mux of [global, new_device] rather than replacing the global.
        if not slot.capture_specs:
            effective = self._state.effective_capture_spec_for_slot(slot)
            if effective is not None:
                slot.capture_specs.append(effective)
        slot.capture_specs.append(device)
        self._restart_slot_capture_if_rolling(slot, slot_index)

    def _remove_mux_input(self, slot_index: int, input_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        if not (0 <= input_index < len(slot.capture_specs)):
            return
        del slot.capture_specs[input_index]
        self._restart_slot_capture_if_rolling(slot, slot_index)

    def _confirm_mux_first_time(self) -> bool:
        """Show a one-time explainer the first time the user asks to
        mux. Subsequent adds proceed silently. Returns True if the
        user wants to proceed."""
        if getattr(self, "_mux_warning_shown", False):
            return True
        reply = QMessageBox.question(
            self,
            "Mux another input?",
            (
                "Muxing sums multiple capture inputs into this slot's "
                "single ring buffer. You keep one slot's RAM footprint "
                "at the same sample rate and channel count, but the "
                "inputs lose their separate identity — they blend into "
                "one stream.\n\n"
                "Because inputs are summed, peaks can add up. Consider "
                "lowering each source's gain before mixing if you hear "
                "clipping.\n\n"
                "Continue?"
            ),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return False
        self._mux_warning_shown = True
        return True

    def _restart_slot_capture_if_rolling(self, slot, slot_index: int) -> None:
        """After mutating `slot.capture_specs`, rebuild and restart the
        capture so the new spec list takes effect. No-op when the slot
        isn't currently capturing."""
        if not slot.is_capturing():
            return
        try:
            slot.stop_capture()
            new_source = self._state.build_capture_for_slot(slot)
            slot.bind_capture(new_source)
            slot.start_capture()
        except Exception as e:
            QMessageBox.warning(
                self,
                "Capture restart failed",
                f"Could not update capture on {slot.name!r}:\n\n{e}",
            )

    def _set_slot_capture_spec(
        self, slot_index: int, device: CaptureDevice | None
    ) -> None:
        """Set a slot's per-slot capture override (or clear it to follow
        the global default). If the slot is currently capturing, stop
        and restart with the new device so the change takes effect
        immediately."""
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        slot.capture_spec = device
        self._restart_slot_capture_if_rolling(slot, slot_index)

    def _flush_slot_buffer(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        buffered = slot.buffered_seconds()
        reply = QMessageBox.question(
            self,
            "Flush buffer?",
            f"Discard {buffered:.1f}s of buffered audio on {slot.name!r}?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        slot.buffer.flush()

    def _remove_slot_with_confirmation(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        reply = QMessageBox.question(
            self,
            "Remove source?",
            (
                f"This will stop capture on {slot.name!r} and discard its "
                f"{slot.buffered_seconds():.1f} s of buffered audio.\n\n"
                "Existing checkouts on this slot will also be lost — "
                "they live in the slot's CheckoutManager."
            ),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self._state.remove_slot(slot_index)
        except Exception as e:
            QMessageBox.warning(self, "Remove failed", str(e))
            return
        # Adjust buffer turntable track count to match the new slot count;
        # the clip turntable now reflects checkouts via _refresh_clip_side.
        n = len(self._state.slots)
        self.buffer_turntable.set_track_count(max(n, 1))
        # Mirror selection on the (now-current) active slot
        active_idx = self._state.active_slot_index
        if 0 <= active_idx < self.buffer_turntable.track_count():
            self.buffer_turntable.select_track(active_idx)
        self._refresh_source_names()
        self._refresh_source_indicators()
        self._refresh_clip_side()
