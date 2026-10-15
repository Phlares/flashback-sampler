"""PySide6 6.12 crashes at interpreter exit when top-level widgets from
earlier tests are still alive (#93). These two tests run in file order:
the first leaks a widget the way a real test does, the second checks
that the suite's teardown deleted it."""
from PySide6.QtWidgets import QWidget

_held = []


def test_a_test_leaves_a_top_level_widget(qapp):
    _held.append(QWidget())
    assert qapp.topLevelWidgets()


def test_the_next_test_starts_with_no_top_level_widgets(qapp):
    assert qapp.topLevelWidgets() == []
