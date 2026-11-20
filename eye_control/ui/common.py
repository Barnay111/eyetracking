"""Общие UI-утилиты: стиль крупных кнопок и «взглядовое» нажатие кнопок своих окон."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QWidget

from core.os_controller import make_window_non_activating

BUTTON_QSS = """
QWidget#Panel {
    background: rgba(28, 30, 36, 235);
    border: 1px solid rgba(255,255,255,40);
    border-radius: 18px;
}
QPushButton {
    background: #3A3F4B;
    color: white;
    border: 2px solid #4C5260;
    border-radius: 14px;
    font-size: 17px;
    font-weight: 600;
    padding: 6px;
}
QPushButton:hover { background: #4A5263; border-color: #2D9CDB; }
QPushButton:pressed { background: #2D9CDB; }
QPushButton:checked { background: #2D9CDB; border-color: #8FD0FF; }
QPushButton#Danger { background: #8E2B2B; }
QPushButton#Accent { background: #2E7D4F; }
QPushButton#Double { background: #8A6D1A; border: 3px solid #F2C94C; }
QLabel { color: #DDE3EA; font-size: 14px; }
"""


def setup_floating_window(w: QWidget, accept_focus: bool = False) -> None:
    """Плавающее окно поверх всех, без рамки, не забирает фокус."""
    flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
    if not accept_focus:
        flags |= Qt.WindowDoesNotAcceptFocus
    w.setWindowFlags(flags)
    w.setAttribute(Qt.WA_TranslucentBackground)
    w.setAttribute(Qt.WA_ShowWithoutActivating)
    w.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
    w.setStyleSheet(BUTTON_QSS)


def apply_non_activating(w: QWidget) -> None:
    make_window_non_activating(w)


def button_at(window: QWidget, global_pos: QPoint) -> QAbstractButton | None:
    """Кнопка нашего окна под глобальной точкой (или None)."""
    if not window.isVisible() or not window.frameGeometry().contains(global_pos):
        return None
    child = window.childAt(window.mapFromGlobal(global_pos))
    while child is not None and child is not window:
        if isinstance(child, QAbstractButton):
            return child if child.isEnabled() and child.isVisible() else None
        child = child.parentWidget()
    return None


class DraggablePanel(QWidget):
    """Окно, которое можно перетащить мышью за фон (для сопровождающего)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag: QPoint | None = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None
