"""Системное управление: курсор, клики, скролл, dwell-логика, HiDPI, права ОС.

Координаты везде — логические (device-independent) точки Qt в глобальном
пространстве рабочего стола. Перевод в «нативные» координаты делается здесь:

* macOS: Quartz работает в точках (points) — совпадают с логическими Qt,
  Retina-масштаб учитывать не нужно. Курсор двигаем через pynput
  (CGEventPost mouseMoved), чтобы приложения получали hover-события.
* Windows: Qt 6 включает Per-Monitor-DPI-Aware v2, поэтому WinAPI работает в
  физических пикселях. Перемещение выполняем через QCursor.setPos — Qt сам
  пересчитывает логические координаты в нативные с учётом масштаба каждого
  монитора; Windows генерирует WM_MOUSEMOVE, hover работает.
"""
from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtGui import QCursor, QGuiApplication

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
class CursorController:
    """Перемещение системного курсора и синтез кликов/скролла."""

    def __init__(self) -> None:
        # pynput импортируем после создания QApplication: на Windows Qt должен
        # первым выставить DPI awareness процесса.
        from pynput.mouse import Button, Controller

        self._mouse = Controller()
        self._Button = Button
        self._last_pos: tuple[int, int] | None = None

    def move(self, x: float, y: float) -> None:
        xi, yi = int(round(x)), int(round(y))
        if self._last_pos == (xi, yi):
            return
        self._last_pos = (xi, yi)
        try:
            if IS_WIN:
                screen = QGuiApplication.screenAt(QPoint(xi, yi)) or QGuiApplication.primaryScreen()
                QCursor.setPos(screen, xi, yi)
            else:
                self._mouse.position = (xi, yi)
        except Exception as e:  # pragma: no cover
            print(f"[os_controller] move failed: {e}", file=sys.stderr)

    def position(self) -> tuple[int, int]:
        p = QCursor.pos()
        return p.x(), p.y()

    def left_click(self) -> None:
        self._mouse.click(self._Button.left, 1)

    def double_click(self) -> None:
        self._mouse.click(self._Button.left, 2)

    def right_click(self) -> None:
        self._mouse.click(self._Button.right, 1)

    def scroll(self, steps: int) -> None:
        """steps > 0 — вверх, < 0 — вниз."""
        self._mouse.scroll(0, steps)

    def click(self, mode: str) -> None:
        if mode == "double":
            self.double_click()
        elif mode == "right":
            self.right_click()
        else:
            self.left_click()


# ---------------------------------------------------------------------------
@dataclass
class DwellState:
    progress: float = 0.0          # 0..1
    fired: bool = False            # сработал ли клик на этом шаге
    anchor: tuple[float, float] | None = None
    locked: bool = False           # после клика — ждём выхода из радиуса или cooldown


class DwellDetector:
    """Логика dwell-клика.

    Взгляд «фиксируется», если все точки остаются в радиусе R от центра
    фиксации (центроид точек с начала фиксации) в течение T секунд.
    После срабатывания детектор блокируется, пока взгляд не выйдет за R
    или не истечёт cooldown — тогда отсчёт начинается заново.
    """

    def __init__(self, dwell_time: float = 1.2, radius: float = 45.0, cooldown: float = 1.0):
        self.dwell_time = dwell_time
        self.radius = radius
        self.cooldown = cooldown
        self.reset()

    def reset(self) -> None:
        self._sum_x = self._sum_y = 0.0
        self._n = 0
        self._start: float | None = None
        self._locked = False
        self._lock_until = 0.0

    @property
    def anchor(self) -> tuple[float, float] | None:
        if self._n == 0:
            return None
        return self._sum_x / self._n, self._sum_y / self._n

    def lock_until_exit(self, x: float, y: float) -> None:
        """Не кликать, пока взгляд не выйдет из радиуса вокруг (x, y)."""
        self._restart(x, y, time.monotonic())
        self._locked = True
        self._lock_until = float("inf")

    def _restart(self, x: float, y: float, t: float) -> None:
        self._sum_x, self._sum_y, self._n = x, y, 1
        self._start = t

    def update(self, x: float, y: float, t: float | None = None, dwell_time: float | None = None) -> DwellState:
        t = time.monotonic() if t is None else t
        T = dwell_time if dwell_time is not None else self.dwell_time
        anchor = self.anchor
        if anchor is None or math.hypot(x - anchor[0], y - anchor[1]) > self.radius:
            # Вышли из радиуса — новая фиксация, блокировка снимается
            self._locked = False
            self._restart(x, y, t)
            return DwellState(0.0, False, (x, y), False)

        # Внутри радиуса: центроид медленно «подтягивается» к взгляду (гасит дрейф)
        if self._n < 200:
            self._sum_x += x
            self._sum_y += y
            self._n += 1

        if self._locked:
            if t >= self._lock_until:
                self._locked = False
                self._start = t
            return DwellState(0.0, False, self.anchor, self._locked)

        progress = (t - (self._start or t)) / max(T, 1e-3)
        if progress >= 1.0:
            self._locked = True
            self._lock_until = t + self.cooldown
            return DwellState(1.0, True, self.anchor, True)
        return DwellState(max(0.0, progress), False, self.anchor, False)


# ---------------------------------------------------------------------------
class ScrollZoneController:
    """В режиме «Скролл»: взгляд в верхней/нижней зоне экрана → прокрутка."""

    def __init__(self, cursor: CursorController, settings) -> None:
        self.cursor = cursor
        self.settings = settings
        self._last = 0.0
        self._enter: float | None = None
        self._dir = 0

    def reset(self) -> None:
        self._enter = None
        self._dir = 0

    def zone_direction(self, x: float, y: float) -> int:
        screen = QGuiApplication.screenAt(QPoint(int(x), int(y))) or QGuiApplication.primaryScreen()
        g = screen.geometry()
        ratio = self.settings.scroll_zone_ratio
        if y < g.top() + g.height() * ratio:
            return 1
        if y > g.bottom() - g.height() * ratio:
            return -1
        return 0

    def update(self, x: float, y: float, t: float | None = None) -> tuple[int, float]:
        """Возвращает (направление, прогресс активации 0..1)."""
        t = time.monotonic() if t is None else t
        d = self.zone_direction(x, y)
        if d == 0:
            self.reset()
            return 0, 0.0
        if d != self._dir:
            self._dir, self._enter = d, t
        delay = 0.4  # небольшая задержка, чтобы случайный взгляд не листал страницу
        prog = min(1.0, (t - (self._enter or t)) / delay)
        if prog >= 1.0 and t - self._last >= self.settings.scroll_interval:
            self._last = t
            try:
                self.cursor.scroll(d * self.settings.scroll_speed)
            except Exception as e:  # pragma: no cover
                print(f"[os_controller] scroll failed: {e}", file=sys.stderr)
        return d, prog


# ---------------------------------------------------------------------------
class KeyboardOutput:
    """Печать текста в активное окно системы."""

    def __init__(self) -> None:
        from pynput.keyboard import Controller, Key

        self._kb = Controller()
        self._Key = Key

    def type_text(self, text: str) -> None:
        self._kb.type(text)

    def backspace(self) -> None:
        self._kb.tap(self._Key.backspace)

    def enter(self) -> None:
        self._kb.tap(self._Key.enter)

    def paste(self) -> None:
        mod = self._Key.cmd if IS_MAC else self._Key.ctrl
        with self._kb.pressed(mod):
            self._kb.tap("v")

    def shortcut(self, keys: list[str]) -> None:
        """Сочетание клавиш: ["cmd", "w"], ["alt", "f4"], ["ctrl", "cmd", "f"].
        Все клавиши, кроме последней, зажимаются как модификаторы."""
        def k(name: str):
            return getattr(self._Key, name) if len(name) > 1 else name

        mods = [k(n) for n in keys[:-1]]
        for m in mods:
            self._kb.press(m)
        try:
            self._kb.tap(k(keys[-1]))
        finally:
            for m in reversed(mods):
                self._kb.release(m)


def frontmost_app() -> tuple[str, int] | None:
    """(имя, pid) активного приложения — туда уйдут горячие клавиши. Только macOS."""
    if not IS_MAC:
        return None
    try:
        import AppKit

        a = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return (str(a.localizedName()), int(a.processIdentifier())) if a is not None else None
    except Exception:
        return None


def dock_apps() -> list[tuple[str, str]]:
    """Приложения из Dock пользователя: [(название, путь к .app)]. Только macOS."""
    if not IS_MAC:
        return []
    import plistlib
    import subprocess
    from urllib.parse import unquote, urlparse

    out = [("Finder", "/System/Library/CoreServices/Finder.app")]
    try:
        raw = subprocess.run(["defaults", "export", "com.apple.dock", "-"], capture_output=True, timeout=3).stdout
        for tile in plistlib.loads(raw).get("persistent-apps", []):
            td = tile.get("tile-data", {})
            url = td.get("file-data", {}).get("_CFURLString", "")
            path = unquote(urlparse(url).path).rstrip("/")
            if path.endswith(".app") and Path(path).exists():
                out.append((td.get("file-label") or Path(path).stem, path))
    except Exception as e:
        print(f"[os_controller] не удалось прочитать Dock: {e}", file=sys.stderr)
    return out


def launch_app(path: str) -> None:
    import subprocess

    if IS_MAC:
        subprocess.Popen(["open", path])
    elif IS_WIN:
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen([path])


# ---------------------------------------------------------------------------
class GlobalHotkeys(QObject):
    """Глобальная горячая клавиша паузы (pynput). Событие доставляется в GUI-поток."""

    pause_toggled = Signal()

    def __init__(self, combo: str, parent=None):
        super().__init__(parent)
        self._listener = None
        try:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys({combo: self.pause_toggled.emit})
            self._listener.daemon = True
            self._listener.start()
        except Exception as e:
            print(f"[os_controller] Глобальная горячая клавиша недоступна: {e}", file=sys.stderr)
            self._listener = None

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
def make_window_non_activating(widget) -> None:
    """Окно не забирает фокус у активного приложения при клике по нему.

    На Windows дополнительно выставляем WS_EX_NOACTIVATE. На macOS работает
    флаг Qt.WindowDoesNotAcceptFocus у Tool-окна, а кнопки наших окон
    нажимаются программно (без системного клика), так что фокус целевого
    приложения не теряется.
    """
    if not IS_WIN:
        return
    try:
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOPMOST = 0x00000008
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_ = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        style = get(hwnd, GWL_EXSTYLE)
        set_(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
    except Exception as e:  # pragma: no cover
        print(f"[os_controller] WS_EX_NOACTIVATE failed: {e}", file=sys.stderr)


def raise_above_menubar(widget) -> None:
    """macOS: поднять окно выше Dock и строки меню (NSStatusWindowLevel).

    Окна Qt «поверх всех» на macOS живут на уровне NSFloatingWindowLevel (3),
    а Dock (20) и строка меню (24) выше — рисунок поверх них не виден. Вызывать
    после show(). Окно также остаётся на всех рабочих столах.
    """
    if not IS_MAC:
        return
    try:
        import objc
        from ctypes import c_void_p

        view = objc.objc_object(c_void_p=c_void_p(int(widget.winId())))
        win = view.window()
        if win is not None:
            win.setLevel_(25)  # NSStatusWindowLevel
            # canJoinAllSpaces | stationary | ignoresCycle | fullScreenAuxiliary
            win.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) | (1 << 8))
    except Exception as e:  # pragma: no cover
        print(f"[os_controller] не удалось поднять окно над меню: {e}", file=sys.stderr)


def check_accessibility_permission() -> bool:
    """macOS: есть ли у процесса право «Универсальный доступ» (нужно для управления мышью)."""
    if not IS_MAC:
        return True
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return True  # не смогли проверить — не блокируем запуск


def open_accessibility_settings() -> None:
    if IS_MAC:
        import subprocess

        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]
        )


def open_camera_settings() -> None:
    import subprocess

    if IS_MAC:
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera"])
    elif IS_WIN:
        subprocess.Popen(["cmd", "/c", "start", "ms-settings:privacy-webcam"], shell=False)
