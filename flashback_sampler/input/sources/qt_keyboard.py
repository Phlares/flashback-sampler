from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QWidget

from flashback_sampler.input.core import BindingTable, invoke
from flashback_sampler.input.core.events import InputEvent


class KeyboardSource(QObject):
    """Qt event filter that converts QKeyEvent presses into InputEvents,
    resolves them through a BindingTable, and dispatches via ``invoke``.

    Installs itself as an event filter on ``target_widget`` on construction.
    """

    def __init__(self, table: BindingTable, target_widget: QWidget) -> None:
        super().__init__(target_widget)
        self._table = table
        target_widget.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.KeyPress:
            return False
        assert isinstance(event, QKeyEvent)
        code = self._event_to_code(event)
        if code is None:
            return False
        ie = InputEvent(
            source="keyboard",
            kind="press",
            code=code,
            is_repeat=event.isAutoRepeat(),
        )
        action_id = self._table.resolve(ie)
        if action_id is None:
            return False
        invoke(action_id, is_repeat=ie.is_repeat)
        return True  # consumed

    @staticmethod
    def _event_to_code(event: QKeyEvent) -> str | None:
        # Ignore modifier-only presses
        if event.key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return None
        seq = QKeySequence(event.keyCombination()).toString(QKeySequence.PortableText)
        return seq or None
