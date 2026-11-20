"""Полноэкранная калибровка (PySide6, без cv2.imshow).

Этапы:
  WAIT_FACE → COUNTDOWN
  → сетка N точек (9…99, «змейкой»): [MOVE → SETTLE → COLLECT], каждые 20 точек — REST
  → PURSUIT: точка плавно летает по фигуре Лиссажу, глаза следят (много сэмплов за секунды)
  → TRAIN (в фоне: текущая сессия + вся история из датасета)
  → 5 проверочных точек → VALIDATE (честная ошибка, затем точки идут в обучение) → RESULT

Сэмплы пишет GazeEngine в своём потоке с текущей целью. Если лицо пропадает,
таймеры сбора/слежения «замораживаются». Esc — отмена: уже собранные данные
всё равно сохраняются в датасет.
"""
from __future__ import annotations

import math
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from config import CALIB_GRIDS

MARGIN = 0.06
# Проверочные точки — между узлами сетки (доли рабочей области)
VALIDATION_POINTS = [(0.17, 0.2), (0.83, 0.2), (0.83, 0.8), (0.17, 0.8), (0.5, 0.62)]
PURSUIT_LAG = 0.12       # сек: глаз отстаёт от движущейся цели
PURSUIT_WARMUP = 1.2     # сек в начале слежения без записи (глаз «цепляется»)
PURSUIT_GROUP_SEC = 1.0  # каждые N сек слежения — новая группа (для кросс-валидации)
FIRST_POINT_EXTRA_SETTLE = 0.9  # сек: после паузы/обучения глазам нужно больше времени найти точку
MOVE_TIME = 0.4

(WAIT_FACE, COUNTDOWN, MOVE, SETTLE, COLLECT, REST, PURSUIT,
 TRAIN, VALIDATE, RESULT) = range(10)


def grid_points(n_points: int, w: float, h: float) -> list[QPointF]:
    cols, rows = CALIB_GRIDS[n_points]
    mx, my = w * MARGIN, h * MARGIN
    pts = []
    for r in range(rows):
        cs = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)  # «змейка»: без длинных прыжков
        for c in cs:
            pts.append(QPointF(mx + c * (w - 2 * mx) / (cols - 1), my + r * (h - 2 * my) / (rows - 1)))
    return pts


class CalibrationWindow(QWidget):
    finished = Signal(bool, str)  # ok, message

    def __init__(self, engine, settings, screen):
        super().__init__(None)
        self.engine = engine
        self.settings = settings
        self._screen = screen
        self.setWindowTitle("Калибровка")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setCursor(Qt.BlankCursor)
        self.setGeometry(screen.geometry())

        g = screen.geometry()
        w, h = g.width(), g.height()
        self._grid = grid_points(settings.calib_points, w, h)
        mx, my = w * MARGIN, h * MARGIN
        self._val = [QPointF(mx + fx * (w - 2 * mx), my + fy * (h - 2 * my)) for fx, fy in VALIDATION_POINTS]
        self._points = self._grid + self._val
        self._n_grid = len(self._grid)

        self._idx = 0
        self._first_after_pause = True
        self._state = WAIT_FACE
        self._t0 = time.monotonic()
        self._last_tick = time.monotonic()
        self._collect_elapsed = 0.0
        self._pursuit_elapsed = 0.0
        self._pursuit_group_t = -1.0
        self._dot = QPointF(self._points[0])
        self._msg = ""
        self._ok = False
        self._job: threading.Thread | None = None
        self._job_result = None
        self._after_job = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        engine.start_session((g.x(), g.y(), g.width(), g.height()))

    # ------------------------------------------------------------------
    def start(self):
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and self._state == REST:
            self._t0 -= self.settings.calib_rest_time  # пропустить отдых
            return
        if e.key() == Qt.Key_Escape:
            self.engine.end_collect()
            n = self.engine.save_current_session()
            self._finish(False, f"Калибровка отменена (собранные {n} сэмплов сохранены)" if n else
                         "Калибровка отменена")
        else:
            super().keyPressEvent(e)

    def _goto(self, state):
        self._state = state
        self._t0 = time.monotonic()

    def _global(self, p: QPointF) -> tuple[float, float]:
        g = self._screen.geometry()
        return g.x() + p.x(), g.y() + p.y()

    def _finish(self, ok: bool, msg: str):
        if not self._timer.isActive():
            return
        self._timer.stop()
        self.engine.end_collect()
        self.close()
        self.finished.emit(ok, msg)

    def _run_job(self, fn, after):
        """Обучение в фоновом потоке, чтобы анимация не замирала."""
        self._job_result = None
        self._after_job = after

        def work():
            try:
                self._job_result = fn()
            except Exception as e:  # pragma: no cover
                self._job_result = (False, f"Ошибка обучения: {e}", 0.0)

        self._job = threading.Thread(target=work, daemon=True)
        self._job.start()

    # ------------------------------------------------------------------
    def _next_point(self):
        """После сбора точки: отдых / слежение / обучение / следующая точка."""
        self.engine.end_collect()
        self._idx += 1
        if self._idx == self._n_grid:
            if self.settings.calib_pursuit_time > 0:
                self._pursuit_elapsed = 0.0
                self._pursuit_group_t = -1.0
                self._goto(PURSUIT)
            else:
                self._start_training()
        elif self._idx >= len(self._points):
            self._goto(VALIDATE)
            self._run_job(self.engine.finish_validation, self._on_validated)
        elif self._idx < self._n_grid and self.settings.calib_rest_every and \
                self._idx % self.settings.calib_rest_every == 0:
            self._goto(REST)
        else:
            self._goto(MOVE)

    def _start_training(self):
        self.engine.end_collect()
        self._goto(TRAIN)
        self._run_job(self.engine.train, self._on_trained)

    def _on_trained(self, res):
        ok, msg, _ = res
        self._ok, self._msg = ok, msg
        if ok:
            self._dot = QPointF(self._val[0])
            self._first_after_pause = True
            self._goto(SETTLE)  # к проверочным точкам
        else:
            self._goto(RESULT)

    def _on_validated(self, res):
        self._ok, self._msg, _ = res
        self._goto(RESULT)

    def _pursuit_pos(self, t: float) -> QPointF:
        w, h = self.width(), self.height()
        mx, my = w * MARGIN, h * MARGIN
        ax, ay = (w - 2 * mx) / 2, (h - 2 * my) / 2
        # Лиссажу 3:2 — покрывает весь экран; скорость умеренная, чтобы глаза успевали
        x = w / 2 + ax * math.sin(2 * math.pi * t / 9.0)
        y = h / 2 + ay * math.sin(2 * math.pi * t / 6.0 + math.pi / 4)
        return QPointF(x, y)

    def _tick(self):
        now = time.monotonic()
        dt = min(now - self._last_tick, 0.1)
        self._last_tick = now
        el = now - self._t0
        face = self.engine.face_present
        s = self._state

        if s == WAIT_FACE:
            if face:
                self._goto(COUNTDOWN)
        elif s == COUNTDOWN:
            if not face:
                self._goto(WAIT_FACE)
            elif el >= 2.5:
                self._idx = 0
                self._dot = QPointF(self._points[0])
                self._goto(SETTLE)
        elif s == REST:
            if not face:
                self._t0 += dt  # отдых отсчитывается, только пока вы перед камерой
            if el >= self.settings.calib_rest_time:
                self._first_after_pause = True
                self._goto(MOVE)
        elif s == MOVE:
            prev = self._points[self._idx - 1]
            cur = self._points[self._idx]
            t = min(el / MOVE_TIME, 1.0)
            e = t * t * (3 - 2 * t)
            self._dot = prev + (cur - prev) * e
            if t >= 1.0:
                self._goto(SETTLE)
        elif s == SETTLE:
            # Глаза «доезжают» до точки — сэмплы не собираем (саккада + стабилизация)
            self._dot = QPointF(self._points[self._idx])
            settle = self.settings.calib_settle_time
            if self._first_after_pause:
                settle += FIRST_POINT_EXTRA_SETTLE
            if el >= settle and face:
                self._first_after_pause = False
                self._collect_elapsed = 0.0
                kind = "val" if self._idx >= self._n_grid else "grid"
                self.engine.begin_collect(*self._global(self._points[self._idx]), kind=kind)
                self._goto(COLLECT)
        elif s == COLLECT:
            if face:
                self._collect_elapsed += dt
            if self._collect_elapsed >= self.settings.calib_sample_time:
                self._next_point()
        elif s == PURSUIT:
            if face:
                self._pursuit_elapsed += dt
            t = self._pursuit_elapsed
            self._dot = self._pursuit_pos(t)
            if t >= PURSUIT_WARMUP and face:
                target = self._global(self._pursuit_pos(max(t - PURSUIT_LAG, 0.0)))
                if t - self._pursuit_group_t >= PURSUIT_GROUP_SEC:
                    self._pursuit_group_t = t
                    self.engine.begin_collect(*target, kind="pursuit")
                else:
                    self.engine.update_target(*target)
            elif not face:
                self.engine.end_collect()
                self._pursuit_group_t = -1.0
            if t >= self.settings.calib_pursuit_time:
                self._start_training()
        elif s in (TRAIN, VALIDATE):
            if self._job is not None and not self._job.is_alive() and self._job_result is not None:
                res, after = self._job_result, self._after_job
                self._job, self._job_result, self._after_job = None, None, None
                after(res)
        elif s == RESULT:
            if el >= (3.0 if self._ok else 5.0):
                self._finish(self._ok, self._msg)
        self.update()

    # ------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(18, 18, 22))
        w, h = self.width(), self.height()
        el = time.monotonic() - self._t0
        s = self._state

        def text(t, y, size=26, color="white"):
            p.setPen(QColor(color))
            p.setFont(QFont("", size, QFont.Bold))
            p.drawText(QRectF(0, y, w, 60), Qt.AlignCenter, t)

        if s == WAIT_FACE:
            text("Лицо не найдено — сядьте напротив камеры", h / 2 - 30, 30, "#EB5757")
            text("Esc — отмена", h - 80, 16, "#888")
            return
        if s == COUNTDOWN:
            n = self._n_grid
            extra = f" + слежение {self.settings.calib_pursuit_time:.0f} с" if self.settings.calib_pursuit_time else ""
            text("Смотрите на зелёную точку, голову держите неподвижно", h / 2 - 160, 28)
            text(f"{n} точек{extra} + 5 проверочных", h / 2 - 110, 20, "#AAB")
            t = min(el / 2.5, 1.0)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#27AE60"))
            c = QPointF(w / 2, h / 2)
            p.drawPie(QRectF(c.x() - 50, c.y() - 50, 100, 100), 90 * 16, int(360 * 16 * (1 - t)))
            return
        if s == REST:
            left = max(0.0, self.settings.calib_rest_time - el)
            text("Перерыв — закройте глаза, поморгайте, расслабьтесь", h / 2 - 90, 30)
            text(f"Продолжим через {left:.0f} с", h / 2 - 30, 26, "#AAB")
            text("Пробел — продолжить сразу   ·   Esc — закончить (данные сохранятся)", h / 2 + 30, 16, "#778")
            # круговой таймер
            c = QPointF(w / 2, h / 2 + 140)
            frac = left / max(self.settings.calib_rest_time, 1e-3)
            p.setPen(QPen(QColor("#2D9CDB"), 8, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(c.x() - 45, c.y() - 45, 90, 90), 90 * 16, int(360 * 16 * frac))
            self._draw_progress(p, w, h)
            return
        if s in (TRAIN, VALIDATE):
            dots = "." * (1 + int(el * 3) % 3)
            text(("Обучение модели на всей истории" if s == TRAIN else "Проверка точности") + dots, h / 2 - 30, 30)
            return
        if s == RESULT:
            text(self._msg, h / 2 - 30, 24, "#27AE60" if self._ok else "#EB5757")
            return

        # Точка
        if s == PURSUIT:
            label = f"Следите за точкой глазами   {max(0.0, self.settings.calib_pursuit_time - self._pursuit_elapsed):.0f} с"
            color = QColor("#F2C94C")
        elif self._idx < self._n_grid:
            label = f"Точка {self._idx + 1} из {self._n_grid}"
            color = QColor("#27AE60")
        else:
            label = f"Проверка точности: {self._idx - self._n_grid + 1} из {len(self._val)}"
            color = QColor("#2D9CDB")
        text(label, h - 90, 18, "#888")
        self._draw_progress(p, w, h)

        c = self._dot
        pulse = 1.0 + 0.25 * math.sin(time.monotonic() * 2 * math.pi * 1.5)
        r = 16 * (pulse if s in (MOVE, SETTLE) else 1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(c, r, r)
        p.setBrush(QColor("white"))
        p.drawEllipse(c, 4, 4)

        if s == COLLECT:
            prog = self._collect_elapsed / max(self.settings.calib_sample_time, 1e-3)
            p.setPen(QPen(QColor("white"), 5, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            rr = 36
            p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), 90 * 16, -int(360 * 16 * prog))
        if s in (COLLECT, PURSUIT) and not self.engine.face_present:
            text("Лицо потеряно — сбор приостановлен", 40, 22, "#EB5757")

    def _draw_progress(self, p, w, h):
        total = self._n_grid + len(self._val)
        frac = min(self._idx / max(total, 1), 1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRect(QRectF(0, h - 6, w, 6))
        p.setBrush(QColor("#27AE60"))
        p.drawRect(QRectF(0, h - 6, w * frac, 6))
