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
    categories = set(dialog._category_labels())
    assert "Transport" in categories
    assert "Deck" in categories


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
    assert table._overrides == {"Ctrl+R": "t.a"}
    assert path.exists()


def test_cancel_does_not_touch_table_or_disk(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "b.json"
    table = BindingTable(storage_path=path)
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog.reject()
    assert table._overrides == {}
    assert not path.exists()
