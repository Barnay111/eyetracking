"""Прозрачный click-through оверлей: индикатор взгляда и прогресс dwell-таймера.

По одному окну на каждый монитор (окно на macOS не может корректно
растягиваться на несколько экранов). Все окна прозрачны для ввода
(WindowTransparentForInput), поэтому системные клики проходят сквозь них.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from core.os_controller import make_window_non_activating, raise_above_menubar


class _ScreenOverlay(QWidget):
    def __init__(self, screen, state: "GazeOverlay"):
        super().__init__(None)
        self._state = state
        self._screen = screen
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setGeometry(screen.geometry())

    def showEvent(self, e):
        super().showEvent(e)
        make_window_non_activating(self)
        raise_above_menubar(self)  # кольцо взгляда видно и над Dock / строкой меню

    def paintEvent(self, _):
        st = self._state
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = self.geometry()

        # Баннер «лицо не найдено» — только на основном экране
        if st.banner and self._screen == QGuiApplication.primaryScreen():
            p.setFont(QFont("", 20, QFont.Bold))
            r = QRectF(g.width() / 2 - 260, 24, 520, 56)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(180, 30, 30, 210))
            p.drawRoundedRect(r, 14, 14)
            p.setPen(QColor("white"))
            p.drawText(r, Qt.AlignCenter, st.banner)

        if st.pos is None or not st.visible:
            return
        local = QPointF(st.pos[0] - g.x(), st.pos[1] - g.y())
        if not self.rect().adjusted(-200, -200, 200, 200).contains(local.toPoint()):
            return

        base = QColor(st.color)
        r = st.radius

        # Цель магнита: рамка вокруг кнопки/ссылки, к которой притянулся взгляд
        if st.target_rect is not None:
            tx, ty, tw, th = st.target_rect
            fr = QRectF(tx - g.x() - 4, ty - g.y() - 4, tw + 8, th + 8)
            hl = QColor(base)
            hl.setAlpha(60)
            p.setBrush(hl)
            hl.setAlpha(230)
            p.setPen(QPen(hl, 3))
            p.drawRoundedRect(fr, 8, 8)
            r = min(r, 22)

        # Полупрозрачная заливка + контур точки взгляда
        fill = QColor(base)
        fill.setAlpha(40)
        p.setBrush(fill)
        outline = QColor(base)
        outline.setAlpha(160)
        p.setPen(QPen(outline, 2))
        p.drawEllipse(local, r, r)

        # Центральная точка
        p.setPen(Qt.NoPen)
        dot = QColor(base)
        dot.setAlpha(220)
        p.setBrush(dot)
        p.drawEllipse(local, 4, 4)

        # Круговой прогресс dwell
        if st.progress > 0.0:
            arc = QColor(base)
            arc.setAlpha(240)
            pen = QPen(arc, 6, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            rr = r + 6
            rect = QRectF(local.x() - rr, local.y() - rr, 2 * rr, 2 * rr)
            p.drawArc(rect, 90 * 16, -int(360 * 16 * min(st.progress, 1.0)))

        # Подпись режима (например, «×2» для двойного клика)
        if st.label:
            p.setPen(QColor(st.color))
            p.setFont(QFont("", 18, QFont.Bold))
            p.drawText(QRectF(local.x() + r * 0.55, local.y() - r - 34, 70, 30), Qt.AlignCenter, st.label)

        # Стрелка скролла
        if st.scroll_dir != 0:
            p.setPen(QPen(base, 4))
            p.setFont(QFont("", 28, QFont.Bold))
            txt = "▲" if st.scroll_dir > 0 else "▼"
            p.drawText(QRectF(local.x() - 30, local.y() - r - 50, 60, 40), Qt.AlignCenter, txt)


class GazeOverlay:
    """Фасад над окнами оверлея всех экранов."""

    COLOR_ACTIVE = "#2D9CDB"
    COLOR_FIRE = "#27AE60"
    COLOR_PAUSED = "#9E9E9E"
    COLOR_RESUME = "#F2994A"
    COLOR_SCROLL = "#9B51E0"
    COLOR_DOUBLE = "#F2C94C"

    def __init__(self):
        self.pos: tuple[float, float] | None = None
        self.progress = 0.0
        self.radius = 45
        self.color = self.COLOR_ACTIVE
        self.visible = True
        self.scroll_dir = 0
        self.banner = ""
        self.label = ""
        self.target_rect = None
        self._windows: list[_ScreenOverlay] = []
        self.rebuild()
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda *_: self.rebuild())
        app.screenRemoved.connect(lambda *_: self.rebuild())

    def rebuild(self):
        for w in self._windows:
            w.close()
            w.deleteLater()
        self._windows = [_ScreenOverlay(s, self) for s in QGuiApplication.screens()]
        for w in self._windows:
            w.show()

    def set_state(self, pos=None, progress=None, color=None, radius=None, scroll_dir=None, banner=None, label=None, target_rect=False):
        if target_rect is not False:
            self.target_rect = target_rect
        if label is not None:
            self.label = label
        if pos is not None:
            self.pos = pos
        if progress is not None:
            self.progress = progress
        if color is not None:
            self.color = color
        if radius is not None:
            self.radius = radius
        if scroll_dir is not None:
            self.scroll_dir = scroll_dir
        if banner is not None:
            self.banner = banner
        self.repaint()

    def hide_pointer(self):
        self.pos = None
        self.target_rect = None
        self.progress = 0.0
        self.repaint()

    def set_visible(self, v: bool):
        self.visible = v
        self.repaint()

    def raise_all(self):
        for w in self._windows:
            w.raise_()

    def repaint(self):
        for w in self._windows:
            w.update()

    def set_hidden(self, hidden: bool):
        for w in self._windows:
            w.setVisible(not hidden)
