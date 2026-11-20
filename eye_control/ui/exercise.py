"""Упражнение «Тренировка»: взгляд по очереди на реальные цели вашего экрана.

Подсвечиваются значки Dock, пункты меню, значки строки меню и кнопки панели —
ровно там, куда вы смотрите при работе. Кадры фиксации на каждой цели идут в
обучение с меткой «центр цели», так модель привыкает именно к вашему экрану.

Окно не забирает фокус (иначе строка меню сменилась бы на меню Eye Control) и
прозрачно в «окне» вокруг цели. Остановить: задержать взгляд на «Стоп» 1.5 с
или нажать её мышью.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPushButton, QWidget

from core.os_controller import make_window_non_activating, raise_above_menubar

MOVE_T = 0.35
SETTLE_T = 0.6
COLLECT_T = 1.0
STOP_DWELL = 1.5
GROUP_NAMES = {"dock": "Dock", "menu": "меню", "extras": "значки меню", "toolbar": "панель"}

WAIT_FACE, MOVE, SETTLE, COLLECT, DONE = range(5)


class ExerciseWindow(QWidget):
    finished = Signal(bool, str, dict)

    def __init__(self, engine, screen, targets):
        super().__init__(None)
        self.engine = engine
        self.targets = targets
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())

        self.btn_stop = QPushButton("■ Стоп", self)
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #8E2B2B; color: white; font-size: 22px; font-weight: 700;"
            " border: 2px solid #C0504D; border-radius: 16px; }")
        self.btn_stop.resize(220, 90)
        g = self.geometry()
        self.btn_stop.move((g.width() - 220) // 2, int(g.height() * 0.42))
        self.btn_stop.clicked.connect(self.cancel)
        self._stop_since = None

        self._i = 0
        self._state = WAIT_FACE
        self._t_state = time.monotonic()
        self._misses: dict[str, list[float]] = {}
        self._skipped = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    def start(self):
        self.engine.clear_pending()
        self.show()
        make_window_non_activating(self)
        raise_above_menubar(self)
        self._timer.start(16)

    def on_gaze(self, x: float, y: float):
        """Остановка взглядом: удержание на «Стоп»."""
        r = self.btn_stop.geometry().translated(self.geometry().topLeft())
        if r.adjusted(-30, -30, 30, 30).contains(int(x), int(y)):
            if self._stop_since is None:
                self._stop_since = time.monotonic()
            elif time.monotonic() - self._stop_since >= STOP_DWELL:
                self.cancel()
        else:
            self._stop_since = None

    def cancel(self):
        if self._state == DONE:
            return
        self._timer.stop()
        self._state = DONE
        self.close()
        # То, что уже собрано, не выбрасываем — это полезные данные
        n = sum(len(v) for v in self._misses.values())
        if n >= 5:
            self._finish_engine()
            self.finished.emit(True, f"Тренировка остановлена: учтено целей {n}", self._summary())
        else:
            self.engine.clear_pending()
            self.finished.emit(False, "Тренировка отменена", {})

    def _set(self, st):
        self._state = st
        self._t_state = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        dt = now - self._t_state
        face = self.engine.face_present
        if self._state == WAIT_FACE:
            if face and dt > 0.4:
                self._set(MOVE)
        elif self._state == MOVE:
            if dt >= MOVE_T:
                self._set(SETTLE)
        elif self._state == SETTLE:
            if dt >= SETTLE_T:
                self._set(COLLECT)
        elif self._state == COLLECT:
            if not face:
                self._set(WAIT_FACE)
            elif dt >= COLLECT_T:
                t = self.targets[self._i]
                cx, cy = t.center
                miss = self.engine.add_adjust_point(cx, cy, now - COLLECT_T, now, kind="exercise")
                if miss is None:
                    self._skipped += 1
                else:
                    self._misses.setdefault(t.group, []).append(miss)
                self._i += 1
                if self._i >= len(self.targets):
                    self._done()
                else:
                    self._set(MOVE)
        self.update()

    def _finish_engine(self):
        self.engine.finish_adjust(tolerance=220.0)

    def _summary(self) -> dict:
        return {g: round(sorted(v)[len(v) // 2]) for g, v in self._misses.items() if v}

    def _done(self):
        self._timer.stop()
        self._state = DONE
        self.close()
        self._finish_engine()
        summ = self._summary()
        parts = [f"{GROUP_NAMES.get(g, g)} {v} px" for g, v in summ.items()]
        msg = f"Тренировка: {len(self.targets) - self._skipped} целей. Промах до обучения — " + ", ".join(parts)
        self.finished.emit(True, msg, summ)

    # ------------------------------------------------------------------
    def paintEvent(self, _):
        if self._state == DONE or not self.targets:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = self.geometry()
        t = self.targets[min(self._i, len(self.targets) - 1)]
        rect = QRectF(t.x - g.x() - 8, t.y - g.y() - 8, t.w + 16, t.h + 16)

        # Затемнение с «окном» вокруг цели
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        path.addRect(QRectF(self.rect()))
        path.addRoundedRect(rect, 12, 12)
        p.fillPath(path, QColor(10, 12, 16, 150))

        # Пульсирующая рамка
        dt = time.monotonic() - self._t_state
        pulse = 0.5 + 0.5 * math.sin(dt * 8)
        color = QColor("#27AE60") if self._state == COLLECT else QColor("#F2C94C")
        p.setPen(QPen(color, 4 + 2 * pulse))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, 12, 12)
        if self._state in (SETTLE, COLLECT):
            total = SETTLE_T + COLLECT_T
            done = (dt if self._state == SETTLE else SETTLE_T + dt) / total
            c = QPointF(rect.center())
            p.setPen(QPen(QColor(255, 255, 255, 200), 3))
            rr = max(rect.width(), rect.height()) / 2 + 14
            p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), 90 * 16, -int(360 * 16 * min(done, 1.0)))

        # Подпись цели: над Dock, под меню
        p.setFont(QFont("", 20, QFont.Bold))
        label = t.label[:28]
        lw = max(220.0, len(label) * 13.0)
        ly = rect.top() - 54 if rect.center().y() > g.height() / 2 else rect.bottom() + 14
        lx = min(max(rect.center().x() - lw / 2, 10), g.width() - lw - 10)
        lab = QRectF(lx, ly, lw, 40)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRoundedRect(lab, 10, 10)
        p.setPen(QColor("white"))
        p.drawText(lab, Qt.AlignCenter, label)

        # Заголовок и прогресс
        p.setFont(QFont("", 20, QFont.Bold))
        p.setPen(QColor("#DDE3EA"))
        head = (f"Тренировка: смотрите на подсвеченную цель  ({min(self._i + 1, len(self.targets))}"
                f"/{len(self.targets)})") if self._state != WAIT_FACE else "Тренировка — посмотрите в камеру"
        p.drawText(QRectF(0, g.height() * 0.30, g.width(), 40), Qt.AlignHCenter, head)
        p.setFont(QFont("", 14))
        p.setPen(QColor("#9FB3C8"))
        p.drawText(QRectF(0, g.height() * 0.30 + 42, g.width(), 30), Qt.AlignHCenter,
                   "Остановить — задержите взгляд на «Стоп»")
