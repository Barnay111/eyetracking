"""«Магнит»: притягивание точки взгляда к ближайшему кликабельному элементу.

Веб-камера даёт ошибку ~60–100 px, поэтому вместо «сырого» взгляда dwell
отсчитывается по цели: ближайшей кнопке / ссылке / пункту меню в радиусе R.
Пока взгляд остаётся рядом с целью, точка фиксации стоит неподвижно в её
центре — dwell-кольцо заполняется, даже если взгляд «гуляет» на 50–80 px.

Источники целей:
* кнопки собственных окон (панель, клавиатура, настройки) — на всех ОС;
* элементы других приложений: macOS — Accessibility API (AX),
  Windows — UI Automation (пакет `uiautomation`). Запросы идут в фоновом
  потоке с кэшем по сетке, потому что каждый hit-test — это IPC на 3–20 мс.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field

from config import IS_MAC, IS_WIN

# Роли, которые имеет смысл нажимать
CLICKABLE_ROLES = {
    "AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXMenuButton", "AXCheckBox",
    "AXRadioButton", "AXPopUpButton", "AXComboBox", "AXTextField", "AXTextArea", "AXCell",
    "AXRow", "AXDisclosureTriangle", "AXDockItem", "AXTab", "AXImage", "AXIncrementor",
    "AXColorWell", "AXSegmentedControl",
}
MAX_AREA = 250_000          # больше — это контейнер, а не цель
MAX_IMAGE_SIDE = 220        # AXImage принимаем только как иконку
BIG_SIDE = 160              # по более длинной стороне цель «точечно» не центрируем
PARENT_DEPTH = 4            # сколько уровней вверх искать кликабельного предка
CACHE_CELL = 12             # px
CACHE_TTL = 2.0             # сек


@dataclass
class Target:
    rect: tuple[float, float, float, float]  # x, y, w, h (логические точки)
    role: str
    point: tuple[float, float] = (0.0, 0.0)  # куда кликать (фиксируется при захвате)
    button: object = field(default=None, repr=False)  # QAbstractButton своего окна

    def key(self):
        return (self.role, tuple(round(v) for v in self.rect), id(self.button) if self.button else 0)

    def distance(self, x: float, y: float) -> float:
        rx, ry, rw, rh = self.rect
        dx = max(rx - x, 0.0, x - (rx + rw))
        dy = max(ry - y, 0.0, y - (ry + rh))
        return math.hypot(dx, dy)

    def aim(self, x: float, y: float) -> tuple[float, float]:
        """Точка клика: центр маленькой цели; у длинной (строка списка, поле ввода)
        по длинной оси остаёмся там, куда смотрим, но не ближе 12 px к краю."""
        rx, ry, rw, rh = self.rect

        def axis(lo, size, v):
            if size <= BIG_SIDE:
                return lo + size / 2
            m = min(12.0, size / 2)
            return min(max(v, lo + m), lo + size - m)

        return axis(rx, rw, x), axis(ry, rh, y)


# ---------------------------------------------------------------------------
class _Finder:
    """Фоновый поиск кликабельных элементов вокруг точки (общая часть)."""

    name = "?"

    def __init__(self):
        self._pid = os.getpid()
        self._cache: dict[tuple[int, int], tuple[float, Target | None]] = {}
        self._cond = threading.Condition()
        self._query: tuple[float, float, float] | None = None
        self._result: tuple[tuple[float, float], list[Target]] = ((0.0, 0.0), [])
        self._running = True
        self._thread = threading.Thread(target=self._run, name=f"magnet-{self.name}", daemon=True)
        self._thread.start()

    def _run(self):
        with self._thread_context():
            self._loop()

    def _thread_context(self):
        import contextlib

        return contextlib.nullcontext()

    def _hit(self, x, y) -> Target | None:
        raise NotImplementedError

    def request(self, x: float, y: float, radius: float):
        with self._cond:
            self._query = (x, y, radius)
            self._cond.notify()

    def result(self) -> tuple[tuple[float, float], list[Target]]:
        return self._result

    def stop(self):
        with self._cond:
            self._running = False
            self._cond.notify()

    # --------------------------------------------------------------
    def _loop(self):
        last = None
        while True:
            with self._cond:
                while self._running and (self._query is None or self._query == last):
                    self._cond.wait(0.5)
                if not self._running:
                    return
                q = last = self._query
            x, y, r = q
            try:
                found = self._scan(x, y, r)
            except Exception as e:  # AX/UIA иногда кидают при закрытии окон
                print(f"[magnet] {self.name} error: {e}")
                found = []
            self._result = ((x, y), found)
            time.sleep(0.03)  # не грузить чужие процессы IPC-запросами

    def _scan(self, x, y, r) -> list[Target]:
        pts = [(x, y)]
        for k, (rad, n) in enumerate(((r * 0.5, 6), (r, 10))):
            for i in range(n):
                a = 2 * math.pi * (i + 0.5 * k) / n
                pts.append((x + rad * math.cos(a), y + rad * math.sin(a)))
        now = time.monotonic()
        if len(self._cache) > 4000:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_TTL}
        out: dict = {}
        for px, py in pts:
            cell = (int(px // CACHE_CELL), int(py // CACHE_CELL))
            hit = self._cache.get(cell)
            if hit is None or now - hit[0] > CACHE_TTL:
                t = self._hit(px, py)
                self._cache[cell] = (now, t)
            else:
                t = hit[1]
            if t is not None:
                out[t.key()] = t
        return list(out.values())

    @staticmethod
    def _accept(role: str, rect) -> bool:
        if rect is None:
            return False
        w, h = rect[2], rect[3]
        ok = 4 <= w and 4 <= h and w * h <= MAX_AREA
        if role in ("AXImage", "ImageControl"):
            ok = ok and w <= MAX_IMAGE_SIDE and h <= MAX_IMAGE_SIDE
        return ok


class _AXFinder(_Finder):
    """macOS: Accessibility API."""

    name = "AX"

    def __init__(self):
        import ApplicationServices as AS  # pyobjc, ставится вместе с pynput

        self.AS = AS
        self._sys = AS.AXUIElementCreateSystemWide()
        AS.AXUIElementSetMessagingTimeout(self._sys, 0.15)
        super().__init__()

    def _hit(self, x, y) -> Target | None:
        AS = self.AS
        err, el = AS.AXUIElementCopyElementAtPosition(self._sys, float(x), float(y), None)
        if err != 0 or el is None:
            return None
        err, pid = AS.AXUIElementGetPid(el, None)
        if err == 0 and pid == self._pid:
            return None  # свои окна обрабатываются отдельно
        for _ in range(PARENT_DEPTH):
            err, vals = AS.AXUIElementCopyMultipleAttributeValues(
                el, ["AXRole", "AXPosition", "AXSize", "AXParent"], 0, None
            )
            if err != 0 or not vals:
                return None
            role, pos, size, parent = vals
            if isinstance(role, str) and role in CLICKABLE_ROLES:
                rect = self._rect(pos, size)
                if self._accept(role, rect):
                    return Target(rect=rect, role=role)
            if parent is None or not hasattr(parent, "__class__") or "AXUIElement" not in type(parent).__name__:
                return None
            el = parent
        return None

    def _rect(self, pos, size):
        AS = self.AS
        try:
            ok1, p = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
            ok2, s = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        except Exception:
            return None
        if not (ok1 and ok2):
            return None
        return (float(p.x), float(p.y), float(s.width), float(s.height))


UIA_CLICKABLE = {
    "ButtonControl", "HyperlinkControl", "MenuItemControl", "ListItemControl", "TabItemControl",
    "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "EditControl", "TreeItemControl",
    "SplitButtonControl", "DataItemControl", "ImageControl",
}


class _UIAFinder(_Finder):
    """Windows: UI Automation. UIA работает в физических пикселях, Qt — в логических,
    поэтому координаты пересчитываются по снимку геометрии экранов (из GUI-потока).
    Не тестировалось на реальном Windows — при ошибках магнит работает только для своих кнопок."""

    name = "UIA"

    def __init__(self, screens):
        import uiautomation  # noqa: F401  (pip install uiautomation; только Windows)

        self._screens = screens  # [(x, y, w, h, dpr)] в логических координатах
        super().__init__()

    def set_screens(self, screens):
        self._screens = screens

    def _thread_context(self):
        import uiautomation as auto

        return auto.UIAutomationInitializerInThread(debug=False)

    def _screen(self, x, y):
        for sx, sy, sw, sh, dpr in self._screens:
            if sx <= x < sx + sw and sy <= y < sy + sh:
                return sx, sy, dpr
        return (0, 0, 1.0) if not self._screens else self._screens[0][:2] + (self._screens[0][4],)

    def _to_phys(self, x, y):
        sx, sy, dpr = self._screen(x, y)
        return int(sx + (x - sx) * dpr), int(sy + (y - sy) * dpr), (sx, sy, dpr)

    def _hit(self, x, y) -> Target | None:
        import uiautomation as auto

        px, py, (sx, sy, dpr) = self._to_phys(x, y)
        c = auto.ControlFromPoint(px, py)
        for _ in range(PARENT_DEPTH):
            if c is None:
                return None
            if c.ProcessId == self._pid:
                return None
            role = c.ControlTypeName
            if role in UIA_CLICKABLE:
                r = c.BoundingRectangle
                rect = (sx + (r.left - sx) / dpr, sy + (r.top - sy) / dpr,
                        (r.right - r.left) / dpr, (r.bottom - r.top) / dpr)
                if self._accept(role, rect):
                    return Target(rect=rect, role=role)
            c = c.GetParentControl()
        return None


def _screens_snapshot():
    from PySide6.QtGui import QGuiApplication

    out = []
    for s in QGuiApplication.screens():
        g = s.geometry()
        out.append((g.x(), g.y(), g.width(), g.height(), float(s.devicePixelRatio())))
    return out


# ---------------------------------------------------------------------------
class Magnet:
    """Выбор цели для текущей точки взгляда (вызывается из GUI-потока)."""

    def __init__(self, settings):
        self.settings = settings
        self.current: Target | None = None
        self._ax = None
        try:
            if IS_MAC:
                self._ax = _AXFinder()
            elif IS_WIN:
                self._ax = _UIAFinder(_screens_snapshot())
        except Exception as e:
            print(f"[magnet] Accessibility/UIA недоступен, магнит только для своих окон: {e}")

    def stop(self):
        if self._ax is not None:
            self._ax.stop()

    def reset(self):
        self.current = None

    def update(self, x: float, y: float, own_window=None, own_buttons=None) -> Target | None:
        """own_window — наше окно под взглядом (или None); own_buttons — кнопки
        всех видимых своих окон (для притяжения к краю панели снаружи)."""
        if not self.settings.magnet_enabled or self.settings.magnet_radius <= 0:
            self.current = None
            return None
        r = float(self.settings.magnet_radius)

        cands: list[Target] = []
        for b in own_buttons or ():
            tl = b.mapToGlobal(b.rect().topLeft())
            cands.append(Target(rect=(tl.x(), tl.y(), b.width(), b.height()), role="own", button=b))
        if own_window is None and self._ax is not None:
            self._ax.request(x, y, r)
            (qx, qy), ax_found = self._ax.result()
            if math.hypot(qx - x, qy - y) <= r * 1.5:  # результат для близкой точки
                cands.extend(ax_found)

        best, best_d = None, 1e9
        for c in cands:
            d = c.distance(x, y)
            if d > r:
                continue
            area = c.rect[2] * c.rect[3]
            # своим кнопкам — приоритет; при равенстве — меньшая (более точная) цель
            score = (d, 0 if c.button is not None else 1, area)
            if best is None or score < best_d:
                best, best_d = c, score

        cur = self.current
        if cur is not None:
            d_cur = cur.distance(x, y)
            still_valid = cur.button is None or cur.button.isVisible()
            keep = still_valid and d_cur <= r * 1.25
            if keep and best is not None and best.key() != cur.key():
                # переключаемся, только если взгляд уже внутри новой цели, а старую покинул
                keep = not (best.distance(x, y) == 0.0 and d_cur > 0.0)
            if keep:
                return cur
        if best is not None:
            best.point = best.aim(x, y)
            if best.button is not None:
                c = best.button.mapToGlobal(best.button.rect().center())
                best.point = (float(c.x()), float(c.y()))
        self.current = best
        return best
