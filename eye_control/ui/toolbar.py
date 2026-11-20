"""Плавающая панель управления с крупными кнопками (удобно попадать взглядом)."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from config import CLICK_MODE_LABELS, CLICK_MODES
from ui.common import DraggablePanel, apply_non_activating, button_at, setup_floating_window


class Toolbar(DraggablePanel):
    calibrate_requested = Signal()
    pause_toggled = Signal()
    keyboard_toggled = Signal()
    click_mode_changed = Signal(str)
    double_once_requested = Signal()
    magnifier_requested = Signal()
    commands_requested = Signal()
    adjust_requested = Signal()
    exercise_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, settings):
        super().__init__(None)
        self.settings = settings
        setup_floating_window(self)
        self.setWindowTitle("Eye Control")

        panel = QWidget(self)
        panel.setObjectName("Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        lay = QGridLayout(panel)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(8)
        self._n = 0
        self._quit_timer = QTimer(self)
        self._quit_timer.setSingleShot(True)
        self._quit_timer.timeout.connect(self._reset_quit)

        size = settings.toolbar_button_size

        def mk(text, slot, obj_name=None):
            b = QPushButton(text)
            b.setFixedSize(size + 24, size)
            b.setFocusPolicy(Qt.NoFocus)
            if obj_name:
                b.setObjectName(obj_name)
            b.clicked.connect(slot)
            lay.addWidget(b, self._n // 2, self._n % 2)
            self._n += 1
            return b

        self.btn_calib = mk("🎯\nКалибровка", self.calibrate_requested.emit)
        self.btn_adjust = mk("⚡\nПодстройка", self.adjust_requested.emit)
        self.btn_exercise = mk("🏋️\nТренировка", self.exercise_requested.emit)
        self.btn_pause = mk("⏸\nПауза", self.pause_toggled.emit)
        self.btn_keyboard = mk("⌨️\nКлавиатура", self.keyboard_toggled.emit)
        self.btn_commands = mk("🧭\nКоманды", self.commands_requested.emit)
        self.btn_double = mk("", self.double_once_requested.emit)
        self.btn_zoom = mk("", self.magnifier_requested.emit)
        self.btn_mode = mk("", self._cycle_mode)
        self.btn_settings = mk("⚙️\nНастройки", self.settings_requested.emit)
        self.btn_quit = mk("✕\nВыход", self._on_quit_pressed, "Danger")

        self.status = QLabel("…")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setFixedWidth(2 * (size + 24) + 8)
        lay.addWidget(self.status, (self._n + 1) // 2, 0, 1, 2)

        self._update_mode_label()
        self.set_double_armed(False)
        self.set_zoom_armed(False)
        self.adjustSize()
        self._place_default()

    def showEvent(self, e):
        super().showEvent(e)
        apply_non_activating(self)

    def _place_default(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.right() - self.width() - 12, g.top() + (g.height() - self.height()) // 2)

    # ------------------------------------------------------------------
    def _cycle_mode(self):
        i = CLICK_MODES.index(self.settings.click_mode)
        self.settings.click_mode = CLICK_MODES[(i + 1) % len(CLICK_MODES)]
        self._update_mode_label()
        self.click_mode_changed.emit(self.settings.click_mode)

    def _update_mode_label(self):
        icons = {"left": "👆", "double": "✌️", "right": "🖱", "scroll": "↕️"}
        m = self.settings.click_mode
        self.btn_mode.setText(f"{icons.get(m, '')}\n{CLICK_MODE_LABELS[m]}")

    def set_double_armed(self, armed: bool):
        """Кнопка «Двойной»: одноразовый двойной клик для следующей фиксации."""
        self.btn_double.setText("✌️ ×2\nЖду цель…" if armed else "✌️\nОткрыть ×2")
        self.btn_double.setObjectName("Double" if armed else "")
        self.btn_double.style().unpolish(self.btn_double)
        self.btn_double.style().polish(self.btn_double)

    def _on_quit_pressed(self):
        """Выход в два нажатия: случайная фиксация на «Выход» не закрывает программу."""
        if self._quit_timer.isActive():
            self._quit_timer.stop()
            self.quit_requested.emit()
            return
        self.btn_quit.setText("⚠️ Точно\nвыйти?")
        self._quit_timer.start(4000)

    def _reset_quit(self):
        self.btn_quit.setText("✕\nВыход")

    def set_zoom_armed(self, armed: bool):
        """Кнопка «Лупа»: следующая фиксация откроет увеличенную область."""
        self.btn_zoom.setText("🔍 ×3\nЖду цель…" if armed else "🔍\nЛупа")
        self.btn_zoom.setObjectName("Double" if armed else "")
        self.btn_zoom.style().unpolish(self.btn_zoom)
        self.btn_zoom.style().polish(self.btn_zoom)

    def set_paused(self, paused: bool):
        if paused:
            self.btn_pause.setText("▶\nВозобновить")
            self.btn_pause.setObjectName("Accent")
        else:
            self.btn_pause.setText("⏸\nПауза")
            self.btn_pause.setObjectName("")
        self.btn_pause.style().unpolish(self.btn_pause)
        self.btn_pause.style().polish(self.btn_pause)

    def set_keyboard_visible(self, v: bool):
        self.btn_keyboard.setText("⌨️\nСкрыть" if v else "⌨️\nКлавиатура")

    def set_status(self, text: str):
        self.status.setText(text)

    def pause_button_rect(self) -> QRect:
        b = self.btn_pause
        return QRect(b.mapToGlobal(QPoint(0, 0)), b.size())

    def button_at(self, global_pos: QPoint):
        return button_at(self, global_pos)
