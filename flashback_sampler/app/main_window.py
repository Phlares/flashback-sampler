"""
MainWindow — the Erebus chassis.

M6 wires in the custom-painted widgets — live BufferTrack with
waveform + thermal level meter, RotaryKnob as the anchor-offset scrub
control, the 8-preset DurationPreset cluster, and CheckoutTrack for
reviewing the selected clip.

M7-8 deferred: TactileButton paintEvent, Monaspace fonts, device
picker, trim handles on the checkout clip.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QPainter
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from flashback_sampler.app.audio_devices import (
    CaptureDevice,
    OutputDevice,
    list_capture_devices,
    list_output_devices,
)
from flashback_sampler.app.config import load_config, save_config
from flashback_sampler.app.state import AppState
from flashback_sampler.app.widgets.buffer_track import (
    BufferTrack,
    compute_anchor_section,
)
from flashback_sampler.app.widgets.checkout_track import CheckoutTrack
from flashback_sampler.app.widgets.tactile_button import TactileButton
from flashback_sampler.app.widgets.topo_background import paint_topo_background
from flashback_sampler.app.widgets.duration_preset import (
    DEFAULT_PRESETS,
    DurationPreset,
)
from flashback_sampler.app.widgets.rotary_knob import RotaryKnob


# Legacy names kept as module-level exports so existing tests and any
# import-sites don't break after the M6 refactor.
DURATION_PRESETS_S: tuple[float, ...] = DEFAULT_PRESETS
DEFAULT_DURATION_INDEX = 4  # 180 s = 3:00


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self._state = state
        self._start_time: float = 0.0
        self._previewing_id: str | None = None
        self._anchor_offset_s: float = 0.0  # driven by rotary

        self.setWindowTitle("flashback-sampler")
        self.setMinimumSize(920, 720)
        self.resize(1120, 820)
        self._device_name: str = "(NOT CAPTURING)"

        self._build_ui()
        self._build_menus()
        self._restore_device_selection()
        self._refresh_device_menus()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)  # ~30 Hz
        self._refresh_timer.timeout.connect(self._tick)
        self._refresh_timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = _ChassisWidget(self)
        self.setCentralWidget(root)

        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(24, 20, 24, 12)
        vbox.setSpacing(14)

        # --- Title strip -----------------------------------------------
        title = QLabel("FLASHBACK")
        title.setProperty("role", "readout")
        vbox.addWidget(title)

        # --- Track 1: live buffer view --------------------------------
        self._buffer_track = BufferTrack(channels=self._state.channels)
        vbox.addWidget(self._buffer_track, 2)

        # --- Transport cluster row: capture | rotary | presets | ck out ─
        transport_row = QHBoxLayout()
        transport_row.setSpacing(18)

        # Left column: capture + flush buttons
        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        self._capture_btn = TactileButton("START CAPTURE", variant="primary")
        self._capture_btn.clicked.connect(self._toggle_capture)
        self._capture_btn.setMinimumHeight(48)
        left_col.addWidget(self._capture_btn)

        self._flush_btn = TactileButton("FLUSH BUFFER", variant="secondary")
        self._flush_btn.clicked.connect(self._flush_buffer)
        left_col.addWidget(self._flush_btn)
        left_col.addStretch(1)
        transport_row.addLayout(left_col, 1)

        # Center: rotary knob (anchor offset scrub)
        rotary_col = QVBoxLayout()
        rotary_col.setSpacing(4)
        rotary_col.setAlignment(Qt.AlignCenter)
        rotary_cap = QLabel("ANCHOR")
        rotary_cap.setProperty("role", "label")
        rotary_cap.setAlignment(Qt.AlignCenter)
        rotary_col.addWidget(rotary_cap)

        self._rotary = RotaryKnob(diameter=140)
        self._rotary.setRange(0.0, max(1.0, self._state.buffer.duration))
        self._rotary.setValue(0.0)
        self._rotary.setDefaultValue(0.0)
        self._rotary.setHubText("NOW")
        self._rotary.valueChanged.connect(self._on_anchor_changed)
        rotary_col.addWidget(self._rotary, 0, Qt.AlignCenter)

        rotary_hint = QLabel("DBL-CLICK = NOW")
        rotary_hint.setProperty("role", "label")
        rotary_hint.setAlignment(Qt.AlignCenter)
        rotary_col.addWidget(rotary_hint)
        transport_row.addLayout(rotary_col, 0)

        # Right-center: 8-preset duration cluster
        preset_col = QVBoxLayout()
        preset_col.setSpacing(4)
        preset_cap = QLabel("DURATION")
        preset_cap.setProperty("role", "label")
        preset_cap.setAlignment(Qt.AlignCenter)
        preset_col.addWidget(preset_cap)
        self._presets = DurationPreset(default_index=DEFAULT_DURATION_INDEX)
        self._presets.setMinimumWidth(90)
        self._presets.durationChanged.connect(self._on_duration_changed)
        preset_col.addWidget(self._presets, 1)
        transport_row.addLayout(preset_col, 0)

        # Right column: big CHECK OUT CTA stack
        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        right_col.addStretch(1)
        self._checkout_btn = TactileButton(
            f"CHECK OUT {_mmss(self._presets.active_duration())}",
            variant="primary",
        )
        self._checkout_btn.setMinimumHeight(60)
        self._checkout_btn.clicked.connect(self._create_checkout)
        self._checkout_btn.setEnabled(False)
        right_col.addWidget(self._checkout_btn)
        right_col.addStretch(1)
        transport_row.addLayout(right_col, 1)

        vbox.addLayout(transport_row, 0)

        # --- Track 2: checkout clip view (starts empty) ---------------
        self._checkout_track = CheckoutTrack()
        self._checkout_track.seekRequested.connect(self._on_clip_seek)
        vbox.addWidget(self._checkout_track, 2)

        # --- Checkout list ---------------------------------------------
        list_label = QLabel("CHECKED-OUT CLIPS")
        list_label.setProperty("role", "label")
        vbox.addWidget(list_label)

        self._list = QListWidget()
        self._list.setMaximumHeight(110)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        vbox.addWidget(self._list, 0)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)

        self._preview_btn = TactileButton("PREVIEW", variant="secondary")
        self._preview_btn.clicked.connect(self._toggle_preview)
        self._preview_btn.setEnabled(False)
        action_row.addWidget(self._preview_btn)

        self._save_btn = TactileButton("SAVE", variant="primary")
        self._save_btn.clicked.connect(self._save_selected)
        self._save_btn.setEnabled(False)
        action_row.addWidget(self._save_btn)

        self._discard_btn = TactileButton("DISCARD", variant="secondary")
        self._discard_btn.clicked.connect(self._discard_selected)
        self._discard_btn.setEnabled(False)
        action_row.addWidget(self._discard_btn)

        action_row.addStretch(1)
        vbox.addLayout(action_row)

        # --- Status bar ------------------------------------------------
        sb = self.statusBar()
        self._device_label = QLabel("DEV  (not capturing)")
        sb.addWidget(self._device_label)
        self._sr_label = QLabel(f"SR  {self._state.sample_rate} HZ")
        sb.addPermanentWidget(self._sr_label)
        self._xrun_label = QLabel("XR  00")
        sb.addPermanentWidget(self._xrun_label)

    # ------------------------------------------------------------------
    # Menu bar & device pickers
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()
        audio_menu = menu_bar.addMenu("&Audio")

        self._capture_menu: QMenu = audio_menu.addMenu("Capture Source")
        self._output_menu: QMenu = audio_menu.addMenu("Preview Output")

        audio_menu.addSeparator()
        refresh_act = QAction("Refresh Device List", self)
        refresh_act.triggered.connect(self._refresh_device_menus)
        audio_menu.addAction(refresh_act)

        # Action groups keep the radio-button exclusivity inside each submenu
        self._capture_action_group: QActionGroup | None = None
        self._output_action_group: QActionGroup | None = None

    def _restore_device_selection(self) -> None:
        """
        Pull the last-used capture and output device IDs from config.json
        and apply them to AppState. Matching is by (kind, id) for capture
        and by name for output (IDs are unstable across device hot-plug).
        """
        cfg = load_config()

        cap_cfg = cfg.get("capture_source") or {}
        if cap_cfg:
            want_kind = cap_cfg.get("kind")
            want_id = cap_cfg.get("id")
            for dev in list_capture_devices():
                if dev.kind == want_kind and dev.id == want_id:
                    self._state.set_capture_spec(dev)
                    break

        out_cfg = cfg.get("preview_output") or {}
        if out_cfg:
            want_name = out_cfg.get("name")
            for dev in list_output_devices():
                if dev.name == want_name:
                    self._state.set_output_spec(dev)
                    break

    def _refresh_device_menus(self) -> None:
        # Capture submenu
        self._capture_menu.clear()
        self._capture_action_group = QActionGroup(self)
        self._capture_action_group.setExclusive(True)

        current_cap = self._state.capture_spec
        cap_devs = list_capture_devices()
        if not cap_devs:
            placeholder = QAction("(no capture devices)", self)
            placeholder.setEnabled(False)
            self._capture_menu.addAction(placeholder)
        else:
            for dev in cap_devs:
                label = dev.name + ("   [default]" if dev.is_default else "")
                act = QAction(label, self)
                act.setCheckable(True)
                act.setData(dev)
                if current_cap is not None and (
                    current_cap.kind == dev.kind and current_cap.id == dev.id
                ):
                    act.setChecked(True)
                self._capture_action_group.addAction(act)
                self._capture_menu.addAction(act)
                act.triggered.connect(
                    lambda _checked=False, d=dev: self._on_capture_selected(d)
                )

        # Output submenu
        self._output_menu.clear()
        self._output_action_group = QActionGroup(self)
        self._output_action_group.setExclusive(True)

        current_out = self._state.output_spec
        out_devs = list_output_devices()
        if not out_devs:
            placeholder = QAction("(no output devices)", self)
            placeholder.setEnabled(False)
            self._output_menu.addAction(placeholder)
        else:
            for dev in out_devs:
                label = dev.name + ("   [default]" if dev.is_default else "")
                act = QAction(label, self)
                act.setCheckable(True)
                act.setData(dev)
                if current_out is not None and current_out.id == dev.id:
                    act.setChecked(True)
                self._output_action_group.addAction(act)
                self._output_menu.addAction(act)
                act.triggered.connect(
                    lambda _checked=False, d=dev: self._on_output_selected(d)
                )

    def _on_capture_selected(self, device: CaptureDevice) -> None:
        was_running = self._state.is_capturing()
        if was_running:
            try:
                self._state.capture.stop()
            except Exception:  # pragma: no cover
                pass
        self._state.set_capture_spec(device)
        self._persist_device_selection()
        if was_running:
            try:
                new_cap = self._state.build_capture()
                new_cap.start()
                self._state.set_capture(new_cap)
                self._device_name = device.name.upper()
                self._device_label.setText(f"DEV  {self._device_name}")
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Capture failed",
                    f"Could not switch capture source:\n\n{e}",
                )
                self._capture_btn.setText("START CAPTURE")
                self._device_name = f"{device.name} (not started)".upper()

    def _on_output_selected(self, device: OutputDevice) -> None:
        was_previewing = self._previewing_id is not None
        if was_previewing:
            self._stop_preview()
        self._state.set_output_spec(device)
        self._persist_device_selection()
        # The user can click PREVIEW again on the new output

    def _persist_device_selection(self) -> None:
        cfg = load_config()
        cap = self._state.capture_spec
        out = self._state.output_spec
        if cap is not None:
            cfg["capture_source"] = {
                "kind": cap.kind,
                "id": cap.id,
                "name": cap.name,
            }
        if out is not None:
            cfg["preview_output"] = {"id": out.id, "name": out.name}
        save_config(cfg)

    # ------------------------------------------------------------------
    # Tick — pulls status from the core at ~30 Hz
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        buf = self._state.buffer
        rms = buf.get_rms_levels(window_seconds=0.1)
        bs = buf.buffered_seconds
        bins = buf.get_peak_bins(seconds=buf.duration, n_bins=360)

        self._buffer_track.update_waveform(bins)
        self._buffer_track.update_levels(rms)
        self._buffer_track.update_readouts(
            buffered_s=bs,
            capacity_s=buf.duration,
            sample_rate=buf.sample_rate,
            channels=buf.channels,
            device_name=self._device_name,
        )

        # Rotary now spans the full buffered audio — max ≈ buffered_s
        # (minus an epsilon so the anchor never points past the ring
        # head). If the rotary sits past buffered_s - duration, the
        # prospective clip clips against the oldest sample and becomes
        # shorter than the preset; the section band visualizes that.
        current_dur = self._current_duration_s()
        rotary_max = max(0.001, bs - 0.001)
        self._rotary.setRange(0.0, rotary_max)

        # Anchor SECTION band on Track 1 — highlights the [start, end]
        # range of the prospective checkout. Dashed ember on the start
        # edge, solid on the end edge, translucent ember fill between.
        section = compute_anchor_section(
            anchor_offset_s=self._anchor_offset_s,
            duration_s=current_dur,
            buffered_s=bs,
        )
        if section is None:
            self._buffer_track.set_anchor_section(None, None)
        else:
            self._buffer_track.set_anchor_section(*section)

        bs = buf.buffered_seconds

        # Checkout is allowed whenever the buffer has anything in it,
        # regardless of whether capture is currently running — user can
        # stop capture and still pull a clip from what they recorded.
        self._checkout_btn.setEnabled(bs > 0.5)

        if self._state.is_capturing():
            cap_src = self._state.capture
            xruns = cap_src.xrun_count() if hasattr(cap_src, "xrun_count") else 0
            self._xrun_label.setText(f"XR  {xruns:02d}")

        # Feed scrub-player cursor into the checkout track playhead
        if self._checkout_track.current_checkout_id() is not None:
            self._checkout_track.set_cursor(
                self._state.scrub_player.cursor_seconds
            )

        # Auto-flip the preview button back when playback drains naturally
        if self._previewing_id is not None and not self._state.scrub_player.is_playing:
            self._previewing_id = None
            self._preview_btn.setText("PREVIEW")

    # ------------------------------------------------------------------
    # Capture control
    # ------------------------------------------------------------------

    def _toggle_capture(self) -> None:
        if self._state.is_capturing():
            self._state.capture.stop()
            self._capture_btn.setText("START CAPTURE")
            self._device_label.setText("DEV  (stopped)")
            self._device_name = "(STOPPED, BUFFER HELD)"
            # NOTE: do NOT disable the checkout button — buffered audio
            # from before the stop is still valid to check out. The tick
            # loop will re-enable it on the next pass based on buffered
            # seconds.
            return

        try:
            cap = self._state.build_capture()
            cap.start()
            self._state.set_capture(cap)
        except Exception as e:  # pragma: no cover — hardware path
            QMessageBox.critical(
                self,
                "Capture failed",
                f"Could not start capture:\n\n{e}",
            )
            return

        self._start_time = time.monotonic()
        self._capture_btn.setText("STOP CAPTURE")
        spec_name = self._state.capture_spec.name if self._state.capture_spec else "?"
        self._device_label.setText(f"DEV  {spec_name.upper()}")
        self._device_name = spec_name.upper()

    # ------------------------------------------------------------------
    # Flush (destructive, confirmation required)
    # ------------------------------------------------------------------

    def _flush_buffer(self) -> None:
        bs = self._state.buffer.buffered_seconds
        if bs <= 0.1:
            # Nothing to flush — silently no-op to avoid a pointless modal
            return
        active_count = len(self._state.checkout_manager.list())
        detail = (
            f"This will discard {_mmss(bs)} of buffered audio.\n\n"
            "Capture will continue from empty if it is running.\n"
        )
        if active_count > 0:
            detail += (
                f"\n{active_count} checked-out clip"
                f"{'s' if active_count != 1 else ''} will NOT be affected — "
                "checkouts are immutable snapshots held in their own memory."
            )
        reply = QMessageBox.question(
            self,
            "Flush ring buffer?",
            detail,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        self._state.buffer.flush()

    # ------------------------------------------------------------------
    # Duration presets + anchor rotary
    # ------------------------------------------------------------------

    def _on_duration_changed(self, dur_s: float) -> None:
        self._checkout_btn.setText(f"CHECK OUT {_mmss(dur_s)}")

    def _current_duration_s(self) -> float:
        return self._presets.active_duration()

    def _on_anchor_changed(self, offset_s: float) -> None:
        self._anchor_offset_s = max(0.0, float(offset_s))
        if self._anchor_offset_s < 0.5:
            self._rotary.setHubText("NOW")
        else:
            self._rotary.setHubText(f"-{_mmss(self._anchor_offset_s)}")

    def _refresh_rotary_range(self) -> None:
        """Rotary max follows the buffer capacity (in seconds)."""
        new_max = max(1.0, float(self._state.buffer.duration))
        self._rotary.setRange(0.0, new_max)

    # ------------------------------------------------------------------
    # Checkout control
    # ------------------------------------------------------------------

    def _create_checkout(self) -> None:
        try:
            co = self._state.checkout_manager.create(
                duration_s=self._current_duration_s(),
                anchor_offset_s=self._anchor_offset_s,
            )
        except Exception as e:
            QMessageBox.warning(self, "Checkout failed", str(e))
            return
        self._refresh_checkout_list()
        # Auto-select the new one
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == co.id:
                self._list.setCurrentRow(i)
                break

    def _refresh_checkout_list(self) -> None:
        prev_selected_id = None
        cur = self._list.currentItem()
        if cur is not None:
            prev_selected_id = cur.data(Qt.UserRole)

        self._list.clear()
        for co in self._state.checkout_manager.list():
            mins = int(co.duration_seconds // 60)
            secs = int(co.duration_seconds - mins * 60)
            label = (
                f"  {co.id}   {mins:02d}:{secs:02d}"
                f"   {co.ram_bytes / 1024 / 1024:5.1f} MB"
                f"   [{co.state}]"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, co.id)
            self._list.addItem(item)
            if prev_selected_id == co.id:
                self._list.setCurrentItem(item)

    def _selected_checkout_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_selection_changed(self) -> None:
        sel = self._selected_checkout_id()
        has = sel is not None
        self._save_btn.setEnabled(has)
        self._discard_btn.setEnabled(has)
        self._preview_btn.setEnabled(has)
        # If the user switches selection mid-preview, stop the old playback
        if self._previewing_id is not None and sel != self._previewing_id:
            self._stop_preview()
        # Feed the selected checkout into Track 2
        if sel is None:
            self._checkout_track.set_checkout(None)
        else:
            try:
                co = self._state.checkout_manager.get(sel)
                self._checkout_track.set_checkout(co)
            except KeyError:
                self._checkout_track.set_checkout(None)

    def _on_clip_seek(self, seconds: float) -> None:
        """User clicked the Track 2 waveform — seek the scrub player."""
        self._state.scrub_player.seek(seconds)
        # If the scrub player has a source bound, nudge the playhead
        # label immediately for snappier feedback.
        self._checkout_track.set_cursor(seconds)

    # ------------------------------------------------------------------
    # Preview — wire ScrubPlayer to the selected checkout
    # ------------------------------------------------------------------

    def _toggle_preview(self) -> None:
        if self._previewing_id is not None:
            self._stop_preview()
            return

        cid = self._selected_checkout_id()
        if cid is None:
            return
        try:
            co = self._state.checkout_manager.get(cid)
        except KeyError:
            return

        player = self._state.scrub_player
        try:
            player.bind(co.audio)
            player.open()  # lazy — first call creates the output stream
            player.play()
        except Exception as e:
            QMessageBox.warning(
                self,
                "Preview failed",
                f"Could not start preview playback:\n\n{e}",
            )
            return

        self._previewing_id = cid
        self._preview_btn.setText("STOP PREVIEW")

    def _stop_preview(self) -> None:
        try:
            self._state.scrub_player.pause()
        except Exception:  # pragma: no cover
            pass
        self._previewing_id = None
        self._preview_btn.setText("▶  PREVIEW")

    def _save_selected(self) -> None:
        cid = self._selected_checkout_id()
        if cid is None:
            return
        target, selected = QFileDialog.getSaveFileName(
            self,
            "Save checkout",
            str(Path.home() / "Documents" / f"flashback_{cid}.wav"),
            "WAV audio (*.wav);;FLAC audio (*.flac)",
        )
        if not target:
            return
        fmt = "FLAC" if selected.startswith("FLAC") or target.lower().endswith(
            ".flac"
        ) else "WAV"
        try:
            self._state.checkout_manager.save(cid, Path(target), fmt=fmt)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self._refresh_checkout_list()

    def _discard_selected(self) -> None:
        cid = self._selected_checkout_id()
        if cid is None:
            return
        if self._previewing_id == cid:
            self._stop_preview()
        try:
            self._state.checkout_manager.discard(cid)
        except Exception as e:
            QMessageBox.warning(self, "Discard failed", str(e))
            return
        self._refresh_checkout_list()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._refresh_timer.stop()
        self._state.shutdown()
        super().closeEvent(event)


class _ChassisWidget(QWidget):
    """
    Central widget for MainWindow. Owns the topographical background
    paint so child widgets (BufferTrack, CheckoutTrack, buttons) sit
    on top of the pattern. Uses an objectName so the global QSS
    QWidget rule can be neutralised for this specific instance.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChassisRoot")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        paint_topo_background(painter, self.width(), self.height())
        painter.end()
        super().paintEvent(event)


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    return f"{m:02d}:{s:02d}"
