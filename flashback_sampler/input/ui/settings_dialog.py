from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from flashback_sampler.input.core import (
    Action,
    BindingTable,
    all_actions,
)


@dataclass
class _Row:
    action: Action
    binding_label: QLabel
    rebind_btn: QPushButton
    clear_btn: QPushButton


class _RebindModal(QDialog):
    def __init__(self, action_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Rebind: {action_name}")
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Press a key or chord for '{action_name}' (Esc to cancel):"))
        self._edit = QKeySequenceEdit()
        lay.addWidget(self._edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def selected_code(self) -> str | None:
        seq = self._edit.keySequence()
        text = seq.toString(QKeySequence.PortableText)
        return text or None


class KeybindingsDialog(QDialog):
    """Dialog for viewing and editing key bindings for every registered action.

    Construction is idempotent: snapshots the registry and current bindings
    into an in-memory edit buffer. Accept (OK) writes through to the passed-in
    ``BindingTable`` and saves it to disk; reject (Cancel) discards the buffer.
    """

    def __init__(self, table: BindingTable) -> None:
        super().__init__()
        self.setWindowTitle("Keybindings")
        self.resize(640, 480)
        self._table = table
        self._edit_buffer: dict[str, str | None] = table.overrides_snapshot()
        self._rows: list[_Row] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Filter row
        filter_row = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter actions…")
        filter_row.addWidget(self._filter)
        self._reset_all_btn = QPushButton("Reset all to defaults")
        filter_row.addWidget(self._reset_all_btn)
        root.addLayout(filter_row)
        self._filter.textChanged.connect(self._apply_filter)
        self._reset_all_btn.clicked.connect(self._apply_reset_all)

        # Scrollable action list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._list_layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        # Populate grouped by category
        by_category: dict[str, list[Action]] = {}
        for a in sorted(all_actions(), key=lambda a: (a.category, a.name)):
            by_category.setdefault(a.category, []).append(a)

        for category, acts in by_category.items():
            header = QLabel(f"<b>{category}</b>")
            self._list_layout.addWidget(header)
            for a in acts:
                self._add_row(a)

        self._list_layout.addStretch(1)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_row(self, action: Action) -> None:
        row = QHBoxLayout()
        name = QLabel(action.name)
        name.setMinimumWidth(200)
        row.addWidget(name)

        current = self._current_binding(action)
        binding_label = QLabel(current if current else "(unbound)")
        binding_label.setMinimumWidth(140)
        row.addWidget(binding_label)

        rebind_btn = QPushButton("Rebind")
        clear_btn = QPushButton("×")
        clear_btn.setFixedWidth(28)
        row.addWidget(rebind_btn)
        row.addWidget(clear_btn)
        rebind_btn.clicked.connect(lambda _=False, aid=action.id, name=action.name:
                                   self._open_rebind(aid, name))
        clear_btn.clicked.connect(lambda _=False, aid=action.id: self._apply_clear(aid))
        row.addStretch(1)

        container = QWidget()
        container.setLayout(row)
        self._list_layout.addWidget(container)

        self._rows.append(_Row(action=action,
                               binding_label=binding_label,
                               rebind_btn=rebind_btn,
                               clear_btn=clear_btn))

    def _current_binding(self, action: Action) -> str | None:
        # Check edit buffer first: explicit override or explicit null
        for code, aid in self._edit_buffer.items():
            if aid == action.id:
                return code
        # If default binding is taken by another action, it's unbound
        if action.default_binding in self._edit_buffer:
            # Either it's null (explicitly unbound) or mapped to a different action
            owner = self._edit_buffer[action.default_binding]
            if owner is None or owner != action.id:
                return None
        return action.default_binding

    def _open_rebind(self, action_id: str, action_name: str) -> None:
        modal = _RebindModal(action_name, parent=self)
        if modal.exec() == QDialog.Accepted:
            code = modal.selected_code()
            if code:
                self._attempt_rebind(action_id, code)

    def _attempt_rebind(self, action_id: str, code: str) -> None:
        """Check for a conflict before applying the rebind.

        If ``code`` is already bound to a different action (via override or
        default), prompts the user via ``_confirm_conflict_replace``. Applies
        the rebind only if no conflict or the user confirms.
        """
        # Compute the current owner of ``code`` using the edit buffer + defaults
        current_owner: str | None = None
        if code in self._edit_buffer:
            current_owner = self._edit_buffer[code]
        else:
            for a in all_actions():
                if a.default_binding == code:
                    current_owner = a.id
                    break
        if current_owner is not None and current_owner != action_id:
            if not self._confirm_conflict_replace(code, current_owner):
                return
        self._apply_rebind(action_id, code)

    def _confirm_conflict_replace(self, code: str, current_owner_id: str) -> bool:
        """Shown to the user when a rebind would override an existing binding.

        Separate method so tests can override without invoking QMessageBox.
        Returns True to proceed with the replacement, False to cancel.
        """
        from PySide6.QtWidgets import QMessageBox
        owner = next((a for a in all_actions() if a.id == current_owner_id), None)
        owner_name = owner.name if owner else current_owner_id
        reply = QMessageBox.question(
            self,
            "Binding conflict",
            f"{code} is already bound to '{owner_name}'. Replace?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _apply_rebind(self, action_id: str, code: str) -> None:
        # Evict any existing owner of this code and any previous override for this action
        self._edit_buffer = {c: aid for c, aid in self._edit_buffer.items()
                             if c != code and aid != action_id}
        # Assign code to the new action
        self._edit_buffer[code] = action_id
        self._refresh_labels()

    def _apply_clear(self, action_id: str) -> None:
        a = next((x for x in all_actions() if x.id == action_id), None)
        # Drop any overrides mapping to this action
        self._edit_buffer = {c: aid for c, aid in self._edit_buffer.items() if aid != action_id}
        # If the action has a default binding, record null to suppress it
        if a and a.default_binding:
            self._edit_buffer[a.default_binding] = None
        self._refresh_labels()

    def _apply_reset_all(self) -> None:
        self._edit_buffer = {}
        self._refresh_labels()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in self._rows:
            container = row.binding_label.parentWidget()
            if not needle:
                container.setVisible(True)
                continue
            hay = f"{row.action.name} {row.action.category} {row.action.id}".lower()
            container.setVisible(needle in hay)

    def accept(self) -> None:
        # Commit the edit buffer through the table's single normalization path.
        self._table.replace_overrides(self._edit_buffer)
        self._table.save()
        super().accept()

    def _refresh_labels(self) -> None:
        for row in self._rows:
            current = self._current_binding(row.action)
            row.binding_label.setText(current if current else "(unbound)")
