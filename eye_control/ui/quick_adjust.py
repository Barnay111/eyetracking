"""Быстрая подстройка: 5 точек ≈ 8 секунд при запуске программы.

Каждый день поза, расстояние и свет немного другие, и модель, обученная на прошлых
сессиях, ошибается на новой в среднем на ~140 px. Если дообучить её на 5 точках
сегодняшней позы с большим весом, ошибка падает примерно до ~77 px — почти как
после полной калибровки на 63 точки (проверено на сохранённых сессиях).
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

POINTS = [(0.5, 0.5), (0.15, 0.15), (0.85, 0.15), (0.85, 0.85), (0.15, 0.85)]
MOVE_T = 0.45      # перелёт точки
SETTLE_T = 0.55    # взгляд догоняет точку — кадры не берём
COLLECT_T = 1.0    # сбор кадров

WAIT_FACE, MOVE, SETTLE, COLLECT, DONE = range(5)


class QuickAdjustWindow(QWidget):
    finished = Signal(bool, str)

    def __init__(self, engine, screen):
        super().__init__(None)
        self.engine = engine
        self._screen = screen
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.BlankCursor)

        g = screen.geometry()
        self._targets = [(g.x() + fx * g.width(), g.y() + fy * g.height()) for fx, fy in POINTS]
        self._i = 0
        self._state = WAIT_FACE
        self._t_state = time.monotonic()
        self._from = self._targets[0]
        self._misses: list[float] = []
        self._skipped = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    def start(self):
        self.engine.clear_adjust()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._timer.start(16)

    def _set(self, st):
        self._state = st
        self._t_state = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        dt = now - self._t_state
        face = self.engine.face_present
        if self._state == WAIT_FACE:
            if face and dt > 0.5:
                self._set(SETTLE)
        elif self._state == MOVE:
            if dt >= MOVE_T:
                self._set(SETTLE)
        elif self._state == SETTLE:
            if dt >= SETTLE_T:
                self._set(COLLECT)
        elif self._state == COLLECT:
            if not face:  # моргнули/отвернулись надолго — повторяем точку
                self._set(WAIT_FACE)
            elif dt >= COLLECT_T:
                tx, ty = self._targets[self._i]
                miss = self.engine.add_adjust_point(tx, ty, now - COLLECT_T, now)
                if miss is None:
                    self._skipped += 1
                else:
                    self._misses.append(miss)
                self._next()
        self.update()

    def _next(self):
        self._from = self._targets[self._i]
        self._i += 1
        if self._i >= len(self._targets):
            self._finish()
        else:
            self._set(MOVE)

    def _finish(self):
        self._timer.stop()
        self._state = DONE
        self.close()
        if len(self._misses) < 3:
            self.finished.emit(False, "Подстройка не удалась (мало точек) — попробуйте ещё раз или откалибруйте")
            return
        bx, by = self.engine.finish_adjust()
        before = sorted(self._misses)[len(self._misses) // 2]
        msg = f"Подстройка: промах до {before:.0f} px, сдвиг ({bx:+.0f}, {by:+.0f})"
        if self._skipped:
            msg += f", пропущено точек: {self._skipped}"
        self.finished.emit(True, msg)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._timer.stop()
            self.close()
            self.engine.clear_adjust()
            self.finished.emit(False, "Подстройка отменена")
        else:
            super().keyPressEvent(e)

    # ------------------------------------------------------------------
    def _pos(self):
        g = self.geometry()
        tx, ty = self._targets[min(self._i, len(self._targets) - 1)]
        if self._state == MOVE:
            k = min((time.monotonic() - self._t_state) / MOVE_T, 1.0)
            k = 0.5 - 0.5 * math.cos(math.pi * k)
            fx, fy = self._from
            tx, ty = fx + (tx - fx) * k, fy + (ty - fy) * k
        return QPointF(tx - g.x(), ty - g.y())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(12, 14, 18, 215))

        p.setPen(QColor("#DDE3EA"))
        p.setFont(QFont("", 22, QFont.Bold))
        n = len(self._targets)
        if self._state == WAIT_FACE:
            title = "Подстройка под сегодняшнюю позу — посмотрите в камеру"
        else:
            title = f"Подстройка: смотрите на зелёную точку  ({min(self._i + 1, n)}/{n})"
        p.drawText(QRectF(0, 40, self.width(), 40), Qt.AlignHCenter, title)
        p.setFont(QFont("", 15))
        p.setPen(QColor("#9FB3C8"))
        p.drawText(QRectF(0, 84, self.width(), 30), Qt.AlignHCenter, "≈ 8 секунд · Esc — пропустить")

        if self._state == DONE:
            return
        c = self._pos()
        dt = time.monotonic() - self._t_state
        # Сжимающееся кольцо: показывает, сколько ещё смотреть
        if self._state in (SETTLE, COLLECT):
            total = SETTLE_T + COLLECT_T
            done = (dt if self._state == SETTLE else SETTLE_T + dt) / total
            r = 46 * (1.0 - min(done, 1.0)) + 16
            p.setPen(QPen(QColor(39, 174, 96, 200), 4))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#27AE60"))
        p.drawEllipse(c, 13, 13)
        p.setBrush(QColor("white"))
        p.drawEllipse(c, 4, 4)
