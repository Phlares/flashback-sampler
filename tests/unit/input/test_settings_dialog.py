import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.input.core import (
    Action, BindingTable, clear_registry, register,
)
from flashback_sampler.input.ui.settings_dialog import KeybindingsDialog


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def test_dialog_shows_all_registered_actions(qapp, tmp_path):
    register(Action(id="t.a", name="Alpha", category="Test",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="Beta", category="Test",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    shown_ids = {row.action.id for row in dialog._rows}
    assert shown_ids == {"t.a", "t.b"}


def test_dialog_shows_category_headers(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="Transport",
                    callable=lambda: None))
    register(Action(id="t.b", name="B", category="Deck",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    from PySide6.QtWidgets import QLabel
    header_text = " ".join(lbl.text() for lbl in dialog.findChildren(QLabel))
    assert "Transport" in header_text
    assert "Deck" in header_text


def test_rebind_updates_edit_buffer(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+Shift+F20")
    assert dialog._edit_buffer == {"Ctrl+Shift+F20": "t.a"}
    # Label updated
    row = next(r for r in dialog._rows if r.action.id == "t.a")
    assert row.binding_label.text() == "Ctrl+Shift+F20"


def test_rebind_replaces_previous_binding_for_same_action(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "F20")
    dialog._apply_rebind("t.a", "F21")
    assert dialog._edit_buffer == {"F21": "t.a"}


def test_rebind_to_conflicting_code_replaces_previous_owner(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="B", category="T",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    # Bind F13 to t.b — conflicts with t.a's default
    dialog._apply_rebind("t.b", "F13")
    assert dialog._edit_buffer["F13"] == "t.b"
    # t.a must now show (unbound)
    row_a = next(r for r in dialog._rows if r.action.id == "t.a")
    assert row_a.binding_label.text() == "(unbound)"


def test_attempt_rebind_with_conflict_asks_for_confirmation(qapp, tmp_path):
    register(Action(id="t.a", name="Alpha", category="T",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="Beta", category="T",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)

    # Simulate user declining the confirmation — rebind should NOT apply.
    dialog._confirm_conflict_replace = lambda code, owner_id: False
    dialog._attempt_rebind("t.b", "F13")
    assert "F13" not in dialog._edit_buffer

    # Simulate user accepting — rebind should apply.
    dialog._confirm_conflict_replace = lambda code, owner_id: True
    dialog._attempt_rebind("t.b", "F13")
    assert dialog._edit_buffer.get("F13") == "t.b"


def test_dialog_hides_non_bindable_actions(qapp, tmp_path):
    # Internal primitives (bindable=False) must not clutter the dialog — only
    # the user-facing rebindable actions appear.
    register(Action(id="t.a", name="Alpha", category="T",
                    callable=lambda: None, bindable=True))
    register(Action(id="t.hidden", name="Hidden", category="T",
                    callable=lambda: None, bindable=False))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    assert {row.action.id for row in dialog._rows} == {"t.a"}


def test_rebind_global_action_rejects_bare_key(qapp, tmp_path):
    # A global-capable action must keep a modifier so it can register as a
    # Win32 global hotkey. A bare-key rebind is refused, leaving the buffer
    # untouched.
    register(Action(id="rec", name="Toggle Recording", category="Transport",
                    callable=lambda: None, default_binding="Ctrl+Alt+R",
                    is_global=True))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    warned = []
    dialog._warn_modifier_required = lambda code: warned.append(code)
    dialog._attempt_rebind("rec", "P")  # bare key — not allowed for global
    assert warned == ["P"]
    assert "P" not in dialog._edit_buffer
    # A modifier-qualified chord is accepted.
    dialog._attempt_rebind("rec", "Ctrl+Alt+P")
    assert dialog._edit_buffer.get("Ctrl+Alt+P") == "rec"


def test_reset_all_clears_edit_buffer(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog._apply_reset_all()
    assert dialog._edit_buffer == {}
    row = next(r for r in dialog._rows if r.action.id == "t.a")
    assert row.binding_label.text() == "F13"  # default


def test_filter_hides_non_matching_rows(qapp, tmp_path):
    register(Action(id="t.a", name="Alpha", category="T",
                    callable=lambda: None))
    register(Action(id="t.b", name="Beta", category="T",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_filter("alp")
    visible = {r.action.id for r in dialog._rows if not r.binding_label.parentWidget().isHidden()}
    assert visible == {"t.a"}


def test_ok_writes_edit_buffer_to_table_and_disk(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "b.json"
    table = BindingTable(storage_path=path)
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog.accept()
    assert table.overrides_snapshot() == {"Ctrl+R": "t.a"}
    assert path.exists()


def test_cancel_does_not_touch_table_or_disk(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "b.json"
    table = BindingTable(storage_path=path)
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog.reject()
    assert table.overrides_snapshot() == {}
    assert not path.exists()
