"""Лупа для точного клика.

Снимок области экрана вокруг точки фиксации показывается увеличенным в N раз.
Ошибка трекинга внутри лупы та же (~80 px), но в исходных координатах она
становится в N раз меньше — так можно попасть по мелкой кнопке или ссылке.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QPushButton, QWidget

from ui.common import apply_non_activating, button_at, setup_floating_window

SRC_W, SRC_H = 250, 156       # размер исходной области (логические точки)
CLOSE_SIZE = 84


class Magnifier(QWidget):
    def __init__(self, zoom: float = 3.0):
        super().__init__(None)
        setup_floating_window(self)
        self.setWindowTitle("Лупа")
        self.zoom = zoom
        self.src = QRect()
        self._pix: QPixmap | None = None
        self.btn_close = QPushButton("✕", self)
        self.btn_close.setObjectName("Danger")
        self.btn_close.setFixedSize(CLOSE_SIZE, CLOSE_SIZE)
        self.btn_close.setFocusPolicy(Qt.NoFocus)
        self.btn_close.setStyleSheet("font-size: 30px;")
        self.btn_close.clicked.connect(self.hide)

    # ------------------------------------------------------------------
    @staticmethod
    def source_rect(cx: float, cy: float, zoom: float) -> QRect:
        """Исходная область вокруг точки, целиком в пределах её экрана."""
        screen = QGuiApplication.screenAt(QPoint(int(cx), int(cy))) or QGuiApplication.primaryScreen()
        g = screen.geometry()
        w, h = SRC_W, SRC_H
        x = int(min(max(cx - w / 2, g.left()), g.right() + 1 - w))
        y = int(min(max(cy - h / 2, g.top()), g.bottom() + 1 - h))
        return QRect(x, y, w, h)

    @staticmethod
    def grab(src: QRect) -> QPixmap | None:
        screen = QGuiApplication.screenAt(src.center()) or QGuiApplication.primaryScreen()
        g = screen.geometry()
        pix = screen.grabWindow(0, src.x() - g.x(), src.y() - g.y(), src.width(), src.height())
        return None if pix.isNull() else pix

    def open(self, src: QRect, pix: QPixmap):
        self.src = QRect(src)
        self._pix = pix
        w, h = int(src.width() * self.zoom), int(src.height() * self.zoom)
        self.setFixedSize(w, h)
        self.btn_close.move(w - CLOSE_SIZE - 10, 10)
        screen = QGuiApplication.screenAt(src.center()) or QGuiApplication.primaryScreen()
        a = screen.availableGeometry()
        x = int(min(max(src.center().x() - w / 2, a.left()), a.right() + 1 - w))
        y = int(min(max(src.center().y() - h / 2, a.top()), a.bottom() + 1 - h))
        self.move(x, y)
        self.show()
        self.raise_()
        self.update()

    def map_to_source(self, global_pos: QPoint) -> QPoint:
        """Точка взгляда в лупе → точка на настоящем экране."""
        local = self.mapFromGlobal(global_pos)
        sx = self.src.x() + local.x() / self.zoom
        sy = self.src.y() + local.y() / self.zoom
        sx = min(max(sx, self.src.left()), self.src.right())
        sy = min(max(sy, self.src.top()), self.src.bottom())
        return QPoint(int(round(sx)), int(round(sy)))

    def button_at(self, global_pos: QPoint):
        return button_at(self, global_pos)

    # ------------------------------------------------------------------
    def showEvent(self, e):
        super().showEvent(e)
        apply_non_activating(self)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect())
        if self._pix is not None:
            p.drawPixmap(r, self._pix, QRectF(self._pix.rect()))
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#F2C94C"), 6))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(3, 3, -3, -3), 12, 12)
        # Подсказка
        p.setPen(QColor(255, 255, 255, 230))
        f = p.font()
        f.setPointSize(15)
        f.setBold(True)
        p.setFont(f)
        tip = QRectF(14, r.height() - 40, r.width() - 28, 30)
        p.fillRect(tip, QColor(0, 0, 0, 140))
        p.drawText(tip, Qt.AlignCenter, "🔍 Лупа ×%g — задержите взгляд на цели" % self.zoom)
