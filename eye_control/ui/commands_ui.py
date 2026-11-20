"""Панель команд: управление окнами и приложениями крупными кнопками.

Горячие клавиши уходят в активное приложение. Наши окна фокус не забирают,
поэтому активным остаётся то окно, с которым вы работали (его имя видно в
заголовке панели). Приложения для запуска берутся из Dock (macOS) или из
настроек (`command_apps`), часто запускаемые — первыми.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileInfo, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFileIconProvider,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config import IS_MAC
from core.os_controller import dock_apps, frontmost_app
from ui.common import DraggablePanel, apply_non_activating, button_at, setup_floating_window

# Команда → сочетание клавиш для macOS / Windows
SHORTCUTS = {
    "back": (["cmd", "["], ["alt", "left"]),
    "forward": (["cmd", "]"], ["alt", "right"]),
    "switch": (["cmd", "tab"], ["alt", "tab"]),
    "search": (["cmd", "space"], ["cmd"]),          # Spotlight / меню «Пуск»
    "minimize": (["cmd", "m"], ["cmd", "down"]),
    "close_window": (["cmd", "w"], ["ctrl", "w"]),
    "quit_app": (["cmd", "q"], ["alt", "f4"]),
    "fullscreen": (["ctrl", "cmd", "f"], ["f11"]),
    "new_tab": (["cmd", "t"], ["ctrl", "t"]),
}
MAX_APPS = 12
APP_COLS = 4


class CommandsWindow(DraggablePanel):
    shortcut = Signal(list)       # сочетание клавиш для активного приложения
    scroll = Signal(int)          # >0 вверх, <0 вниз
    launch = Signal(str, str)     # (название, путь)
    open_keyboard = Signal()
    closed = Signal()

    def __init__(self, settings, dataset=None):
        super().__init__(None)
        self.settings = settings
        self.dataset = dataset
        setup_floating_window(self)
        self.setWindowTitle("Команды")
        self._own_pid = None

        panel = QWidget(self)
        panel.setObjectName("Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        root = QVBoxLayout(panel)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel("Команды")
        self.title.setStyleSheet("font-size: 20px; font-weight: 800;")
        head.addWidget(self.title, 1)
        self.btn_close = self._mk("✕ Скрыть", self._close, "Danger", w=150)
        head.addWidget(self.btn_close)
        root.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(8)
        items = [
            ("◀\nНазад", lambda: self._send("back")),
            ("Вперёд\n▶", lambda: self._send("forward")),
            ("⇄\nДругое окно", lambda: self._send("switch")),
            ("🔎\nПоиск", self._search),
            ("▲\nВверх", lambda: self.scroll.emit(1)),
            ("▼\nВниз", lambda: self.scroll.emit(-1)),
            ("＋\nНовая вкладка", lambda: self._send("new_tab")),
            ("⤢\nВо весь экран", lambda: self._send("fullscreen")),
            ("—\nСвернуть", lambda: self._send("minimize")),
            ("▢\nЗакрыть окно", lambda: self._send("close_window")),
        ]
        self.repeat_buttons = []  # можно «держать» взглядом — нажимаются повторно
        for i, (text, slot) in enumerate(items):
            b = self._mk(text, slot)
            grid.addWidget(b, i // 4, i % 4)
            if text.startswith(("▲", "▼")):
                self.repeat_buttons.append(b)
        self.btn_quit_app = self._mk("", self._quit_app_pressed, "Danger")
        grid.addWidget(self.btn_quit_app, len(items) // 4, len(items) % 4, 1, 4 - len(items) % 4)
        root.addLayout(grid)

        self.apps_label = QLabel("Приложения")
        self.apps_label.setStyleSheet("font-size: 16px; color: #9FB3C8;")
        root.addWidget(self.apps_label)
        self._apps_grid = QGridLayout()
        self._apps_grid.setSpacing(8)
        root.addLayout(self._apps_grid)
        self._app_buttons: list[QToolButton] = []

        self._quit_timer = QTimer(self)
        self._quit_timer.setSingleShot(True)
        self._quit_timer.timeout.connect(self._refresh_target)
        self._target_timer = QTimer(self)
        self._target_timer.timeout.connect(self._refresh_target)
        self._target_name = None

    # ------------------------------------------------------------------
    def _mk(self, text, slot, obj_name=None, w=150, h=80) -> QPushButton:
        b = QPushButton(text)
        b.setMinimumSize(w, h)
        b.setFocusPolicy(Qt.NoFocus)
        if obj_name:
            b.setObjectName(obj_name)
        b.clicked.connect(slot)
        return b

    def _apps(self) -> list[tuple[str, str]]:
        apps = dock_apps()
        for path in self.settings.command_apps:
            apps.append((Path(path).stem, path))
        seen, uniq = set(), []
        for name, path in apps:
            if path not in seen:
                seen.add(path)
                uniq.append((name, path))
        counts = self.dataset.count_events("app_launch", "path") if self.dataset is not None else {}
        # Часто запускаемые — первыми, остальные в порядке Dock
        order = {p: i for i, (_, p) in enumerate(uniq)}
        uniq.sort(key=lambda a: (-counts.get(a[1], 0), order[a[1]]))
        return uniq[:MAX_APPS]

    def _rebuild_apps(self):
        for b in self._app_buttons:
            self._apps_grid.removeWidget(b)
            b.deleteLater()
        self._app_buttons = []
        icons = QFileIconProvider()
        apps = self._apps()
        for i, (name, path) in enumerate(apps):
            b = QToolButton()
            b.setText(name[:16])
            b.setIcon(icons.icon(QFileInfo(path)))
            b.setIconSize(QSize(40, 40))
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setMinimumSize(150, 86)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(
                "QToolButton { background: #3A3F4B; color: white; border: 2px solid #4C5260;"
                " border-radius: 14px; font-size: 14px; font-weight: 600; padding: 4px; }"
            )
            b.clicked.connect(lambda _=False, n=name, p=path: self._launch(n, p))
            self._apps_grid.addWidget(b, i // APP_COLS, i % APP_COLS)
            self._app_buttons.append(b)
        self.apps_label.setVisible(bool(apps))
        if not apps:
            self.apps_label.setText("Приложения: добавьте пути в command_apps (settings.json)")
            self.apps_label.setVisible(True)

    # ------------------------------------------------------------------
    def _target(self):
        app = frontmost_app()
        if app is None:
            return None
        name, pid = app
        if self._own_pid is None:
            import os

            self._own_pid = os.getpid()
        return None if pid == self._own_pid else name

    def _refresh_target(self):
        self._target_name = self._target()
        if IS_MAC:
            t = self._target_name or "— (сначала кликните взглядом по нужному окну)"
            self.title.setText(f"Команды → {t}")
        if not self._quit_timer.isActive():
            self.btn_quit_app.setText(f"✕ Выйти из {self._target_name}" if self._target_name else "✕ Выйти из приложения")

    def _send(self, cmd: str):
        if IS_MAC and self._target() is None:
            # Активна сама Eye Control — ⌘Q/⌘W закрыли бы её, а не нужное окно
            self.title.setText("⚠️ Сначала кликните взглядом по окну программы")
            return
        mac, win = SHORTCUTS[cmd]
        self.shortcut.emit(mac if IS_MAC else win)
        QTimer.singleShot(400, self._refresh_target)

    def _search(self):
        """Spotlight / «Пуск» + экранная клавиатура: так открывается любое приложение."""
        mac, win = SHORTCUTS["search"]
        self.shortcut.emit(mac if IS_MAC else win)  # глобальное сочетание — активное окно не важно
        self._close()  # не загораживать строку поиска
        self.open_keyboard.emit()

    def _quit_app_pressed(self):
        """Выход из приложения — в два нажатия, как и выход из Eye Control."""
        if self._quit_timer.isActive():
            self._quit_timer.stop()
            self._send("quit_app")
            self._refresh_target()
            return
        if IS_MAC and self._target() is None:
            self._send("quit_app")  # покажет предупреждение
            return
        self.btn_quit_app.setText(f"⚠️ Точно выйти из {self._target_name or 'приложения'}?")
        self._quit_timer.start(4000)

    def _launch(self, name: str, path: str):
        self.launch.emit(name, path)
        QTimer.singleShot(1200, self._refresh_target)

    def _close(self):
        self.hide()
        self.closed.emit()

    # ------------------------------------------------------------------
    def showEvent(self, e):
        super().showEvent(e)
        apply_non_activating(self)
        self._target_timer.start(1000)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._target_timer.stop()

    def show_centered(self):
        self._rebuild_apps()
        self._refresh_target()
        self.adjustSize()
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.left() + (g.width() - self.width()) // 2, g.top() + 12)
        self.show()
        self.raise_()

    def button_at(self, global_pos: QPoint):
        return button_at(self, global_pos)
