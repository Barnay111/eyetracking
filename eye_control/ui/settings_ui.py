"""Окно настроек: только крупные кнопки «−/+», чтобы всем можно было управлять взглядом."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import (
    CALIB_GRIDS,
    DWELL_RADIUS_RANGE,
    DWELL_TIME_RANGE,
    KEY_DWELL_RANGE,
    MAGNET_RADIUS_RANGE,
    PROFILES_DIR,
    SMOOTHING_RANGE,
)
from ui.common import DraggablePanel, apply_non_activating, button_at, setup_floating_window


def list_cameras() -> list[str]:
    try:
        from PySide6.QtMultimedia import QMediaDevices

        names = [d.description() for d in QMediaDevices.videoInputs()]
        if names:
            return names
    except Exception:
        pass
    return [f"Камера {i}" for i in range(3)]


class Stepper(QWidget):
    changed = Signal(object)

    def __init__(self, title, value, lo, hi, step, fmt, size):
        super().__init__()
        self.value, self.lo, self.hi, self.step, self.fmt = value, lo, hi, step, fmt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title)
        self.title.setMinimumWidth(220)
        self.title.setStyleSheet("font-size: 17px;")
        self.minus = QPushButton("−")
        self.plus = QPushButton("+")
        for b in (self.minus, self.plus):
            b.setFixedSize(size, size)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet("font-size: 28px;")
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumWidth(170)
        self.label.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.minus.clicked.connect(lambda: self._set(self.value - self.step))
        self.plus.clicked.connect(lambda: self._set(self.value + self.step))
        lay.addWidget(self.title)
        lay.addWidget(self.minus)
        lay.addWidget(self.label)
        lay.addWidget(self.plus)
        self._refresh()

    def _set(self, v):
        v = min(max(v, self.lo), self.hi)
        if isinstance(self.step, float):
            v = round(v, 2)
        if v != self.value:
            self.value = v
            self._refresh()
            self.changed.emit(v)

    def _refresh(self):
        self.label.setText(self.fmt(self.value))
        self.minus.setEnabled(self.value > self.lo)
        self.plus.setEnabled(self.value < self.hi)


class SettingsWindow(DraggablePanel):
    settings_changed = Signal()
    camera_changed = Signal(int)
    save_profile = Signal(str)
    load_profile = Signal(str)

    def __init__(self, settings, dataset=None):
        super().__init__(None)
        self.settings = settings
        self.dataset = dataset
        setup_floating_window(self)
        self.setWindowTitle("Настройки")

        panel = QWidget(self)
        panel.setObjectName("Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        root = QVBoxLayout(panel)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        head = QLabel("Настройки")
        head.setStyleSheet("font-size: 24px; font-weight: 800;")
        root.addWidget(head)

        sz = 56
        self._cams = list_cameras()
        cam_idx = min(settings.camera_index, max(len(self._cams) - 1, 0))
        self.cam = Stepper(
            "Камера",
            cam_idx,
            0,
            max(len(self._cams) - 1, 0),
            1,
            lambda i: (self._cams[i] if i < len(self._cams) else f"#{i}")[:22],
            sz,
        )
        self.cam.changed.connect(self._on_camera)
        self.dwell = Stepper(
            "Время dwell-клика", settings.dwell_time, *DWELL_TIME_RANGE, 0.1, lambda v: f"{v:.1f} с", sz
        )
        self.dwell.changed.connect(lambda v: self._set("dwell_time", v))
        self.radius = Stepper(
            "Радиус фиксации", settings.dwell_radius, *DWELL_RADIUS_RANGE, 5, lambda v: f"{v} px", sz
        )
        self.radius.changed.connect(lambda v: self._set("dwell_radius", int(v)))
        self.smooth = Stepper(
            "Сила сглаживания", settings.smoothing, *SMOOTHING_RANGE, 1, lambda v: f"{v} / 10", sz
        )
        self.smooth.changed.connect(lambda v: self._set("smoothing", int(v)))
        self.cooldown = Stepper(
            "Пауза между кликами", settings.click_cooldown, 0.3, 3.0, 0.1, lambda v: f"{v:.1f} с", sz
        )
        self.cooldown.changed.connect(lambda v: self._set("click_cooldown", v))
        self.resume = Stepper(
            "Удержание «Возобновить»", settings.resume_dwell_time, 1.0, 4.0, 0.5, lambda v: f"{v:.1f} с", sz
        )
        self.resume.changed.connect(lambda v: self._set("resume_dwell_time", v))
        self._grid_choices = sorted(CALIB_GRIDS)
        self.calib_n = Stepper(
            "Точек калибровки", self._grid_choices.index(settings.calib_points), 0,
            len(self._grid_choices) - 1, 1,
            lambda i: f"{self._grid_choices[i]} (~{self._calib_minutes(self._grid_choices[i]):.1f} мин)", sz,
        )
        self.calib_n.changed.connect(lambda i: self._set("calib_points", self._grid_choices[i]))
        self.pursuit = Stepper(
            "Слежение за точкой", int(settings.calib_pursuit_time), 0, 60, 10,
            lambda v: f"{v} с" if v else "выкл.", sz,
        )
        self.pursuit.changed.connect(lambda v: (self._set("calib_pursuit_time", float(v)), self.calib_n._refresh()))
        self.rest = Stepper(
            "Перерыв в калибровке", int(settings.calib_rest_time), 0, 60, 5,
            lambda v: f"{v} с / {self.settings.calib_rest_every} точек" if v else "выкл.", sz,
        )
        self.rest.changed.connect(lambda v: (self._set("calib_rest_time", float(v)),
                                             self._set("calib_rest_every", 12 if v else 0),
                                             self.calib_n._refresh()))
        self.key_dwell = Stepper(
            "Dwell клавиш", settings.key_dwell_time, *KEY_DWELL_RANGE, 0.1, lambda v: f"{v:.1f} с", sz
        )
        self.key_dwell.changed.connect(lambda v: self._set("key_dwell_time", v))
        self.magnet_r = Stepper(
            "Радиус магнита", settings.magnet_radius, *MAGNET_RADIUS_RANGE, 10,
            lambda v: f"{v} px" if v else "выкл.", sz,
        )
        self.magnet_r.changed.connect(lambda v: self._set("magnet_radius", int(v)))
        self.stab = Stepper(
            "Стабилизация курсора", settings.stabilize_radius, 0, 100, 5,
            lambda v: f"{v} px" if v else "выкл.", sz,
        )
        self.stab.changed.connect(lambda v: self._set("stabilize_radius", int(v)))
        steppers = QGridLayout()
        steppers.setHorizontalSpacing(28)
        steppers.setVerticalSpacing(8)
        left = (self.dwell, self.key_dwell, self.radius, self.magnet_r, self.smooth, self.cooldown)
        right = (self.stab, self.cam, self.resume, self.calib_n, self.pursuit, self.rest)
        for col, ws in enumerate((left, right)):
            for row, w in enumerate(ws):
                steppers.addWidget(w, row, col)
        root.addLayout(steppers)

        self.stats = QLabel()
        self.stats.setStyleSheet("font-size: 15px; color: #9FB3C8;")
        root.addWidget(self.stats)
        self.refresh_stats()

        toggles = QGridLayout()
        self.btn_move_paused = QPushButton()
        self.btn_move_paused.clicked.connect(self._toggle_move_paused)
        self.btn_indicator = QPushButton()
        self.btn_indicator.clicked.connect(self._toggle_indicator)
        self.btn_magnet = QPushButton()
        self.btn_magnet.clicked.connect(self._toggle_magnet)
        self.btn_adjust = QPushButton()
        self.btn_adjust.clicked.connect(self._toggle_adjust)
        for i, b in enumerate((self.btn_move_paused, self.btn_indicator, self.btn_magnet, self.btn_adjust)):
            b.setMinimumHeight(sz)
            b.setFocusPolicy(Qt.NoFocus)
            toggles.addWidget(b, 0, i)
        root.addLayout(toggles)

        prof = QHBoxLayout()
        b_save = QPushButton("💾 Сохранить профиль…")
        b_load = QPushButton("📂 Загрузить профиль…")
        b_close = QPushButton("✓ Готово")
        b_close.setObjectName("Accent")
        b_save.clicked.connect(self._save_profile)
        b_load.clicked.connect(self._load_profile)
        b_close.clicked.connect(self.hide)
        for b in (b_save, b_load, b_close):
            b.setMinimumHeight(sz)
            b.setFocusPolicy(Qt.NoFocus)
            prof.addWidget(b)
        root.addLayout(prof)
        self._refresh_toggles()
        self.adjustSize()

    def _calib_minutes(self, n: int) -> float:
        s = self.settings
        per_point = 0.4 + s.calib_settle_time + s.calib_sample_time
        rest = ((n - 1) // max(s.calib_rest_every, 1)) * s.calib_rest_time if s.calib_rest_every else 0.0
        return ((n + 5) * per_point + rest + s.calib_pursuit_time + 5) / 60.0

    def refresh_stats(self):
        if self.dataset is None:
            self.stats.setText("")
            return
        n_s, n_x = self.dataset.stats()
        n_x_txt = f"{n_x:,}".replace(",", " ")
        self.stats.setText(f"🧠 В памяти: {n_s} сессий, {n_x_txt} сэмплов взгляда")

    # ------------------------------------------------------------------
    def _set(self, name, value):
        setattr(self.settings, name, value)
        self.settings.clamp()
        self.settings.save()
        self.settings_changed.emit()

    def _on_camera(self, idx):
        self._set("camera_index", int(idx))
        self.camera_changed.emit(int(idx))

    def _toggle_move_paused(self):
        self._set("move_cursor_when_paused", not self.settings.move_cursor_when_paused)
        self._refresh_toggles()

    def _toggle_indicator(self):
        self._set("show_gaze_indicator", not self.settings.show_gaze_indicator)
        self._refresh_toggles()

    def _toggle_adjust(self):
        self._set("quick_adjust_on_start", not self.settings.quick_adjust_on_start)
        self._refresh_toggles()

    def _toggle_magnet(self):
        self._set("magnet_enabled", not self.settings.magnet_enabled)
        self._refresh_toggles()

    def _refresh_toggles(self):
        s = self.settings
        self.btn_move_paused.setText(
            "Курсор в паузе: " + ("двигается" if s.move_cursor_when_paused else "стоит")
        )
        self.btn_indicator.setText("Индикатор: " + ("ВКЛ" if s.show_gaze_indicator else "ВЫКЛ"))
        self.btn_magnet.setText("🧲 Магнит: " + ("ВКЛ" if s.magnet_enabled else "ВЫКЛ"))
        self.btn_adjust.setText("⚡ Подстройка при запуске: " + ("ВКЛ" if s.quick_adjust_on_start else "ВЫКЛ"))

    def _save_profile(self):
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(None, "Сохранить профиль", str(PROFILES_DIR / "profile.pkl"), "Profile (*.pkl)")
        if path:
            self.save_profile.emit(path)

    def _load_profile(self):
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(None, "Загрузить профиль", str(PROFILES_DIR), "Profile (*.pkl)")
        if path:
            self.load_profile.emit(path)

    # ------------------------------------------------------------------
    def showEvent(self, e):
        super().showEvent(e)
        apply_non_activating(self)

    def show_centered(self):
        self.refresh_stats()
        self.adjustSize()
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.left() + (g.width() - self.width()) // 2, g.top() + (g.height() - self.height()) // 3)
        self.show()
        self.raise_()

    def button_at(self, global_pos: QPoint):
        return button_at(self, global_pos)
