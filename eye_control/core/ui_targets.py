"""Реальные цели на экране пользователя для упражнения «Тренировка».

macOS (Accessibility API): значки Dock, пункты меню активного приложения, значки
строки меню справа (Wi-Fi, батарея, часы…). Плюс кнопки собственной панели —
их добавляет main.py. Это те места, куда человек смотрит чаще всего, и при этом
края экрана, где трекинг по веб-камере ошибается сильнее всего.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import IS_MAC


@dataclass
class UITarget:
    x: float
    y: float
    w: float
    h: float
    label: str
    group: str   # "dock" | "menu" | "extras" | "toolbar"

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


def _mac_targets(screen_rect) -> list[UITarget]:
    import AppKit
    import ApplicationServices as AS

    def attr(el, name):
        err, v = AS.AXUIElementCopyAttributeValue(el, name, None)
        return v if err == 0 else None

    def frame(el):
        p, s = attr(el, "AXPosition"), attr(el, "AXSize")
        if p is None or s is None:
            return None
        ok1, pt = AS.AXValueGetValue(p, AS.kAXValueCGPointType, None)
        ok2, sz = AS.AXValueGetValue(s, AS.kAXValueCGSizeType, None)
        if not (ok1 and ok2) or sz.width < 8 or sz.height < 8:
            return None
        sx, sy, sw, sh = screen_rect
        cx, cy = pt.x + sz.width / 2, pt.y + sz.height / 2
        if not (sx <= cx < sx + sw and sy <= cy < sy + sh):
            return None  # Dock скрыт / элемент за пределами экрана
        return float(pt.x), float(pt.y), float(sz.width), float(sz.height)

    ws = AppKit.NSWorkspace.sharedWorkspace()
    apps = {str(a.bundleIdentifier()): a for a in ws.runningApplications()}
    out: list[UITarget] = []

    # Dock
    if "com.apple.dock" in apps:
        dock = AS.AXUIElementCreateApplication(apps["com.apple.dock"].processIdentifier())
        for lst in attr(dock, "AXChildren") or []:
            for it in attr(lst, "AXChildren") or []:
                sub = str(attr(it, "AXSubrole") or "")
                if "Separator" in sub:
                    continue
                f = frame(it)
                if f is None:
                    continue
                title = str(attr(it, "AXTitle") or "")
                if "MinimizedWindow" in sub:
                    title = "свёрнутое окно"  # заголовки окон могут быть личными
                out.append(UITarget(*f, label=title or "значок", group="dock"))

    # Меню активного приложения
    front = ws.frontmostApplication()
    if front is not None:
        fe = AS.AXUIElementCreateApplication(front.processIdentifier())
        mb = attr(fe, "AXMenuBar")
        for it in (attr(mb, "AXChildren") or []) if mb is not None else []:
            f = frame(it)
            if f is not None:
                out.append(UITarget(*f, label=str(attr(it, "AXTitle") or "меню"), group="menu"))

    # Значки строки меню справа
    for bid in ("com.apple.controlcenter", "com.apple.systemuiserver"):
        if bid not in apps:
            continue
        el = AS.AXUIElementCreateApplication(apps[bid].processIdentifier())
        ex = attr(el, "AXExtrasMenuBar")
        for it in (attr(ex, "AXChildren") or []) if ex is not None else []:
            f = frame(it)
            if f is not None:
                label = str(attr(it, "AXDescription") or attr(it, "AXTitle") or "значок")
                out.append(UITarget(*f, label=label, group="extras"))
    return out


def collect_targets(screen_rect) -> list[UITarget]:
    """screen_rect = (x, y, w, h) основного экрана в логических координатах."""
    if not IS_MAC:
        return []  # TODO: Windows — панель задач через UI Automation
    try:
        return _mac_targets(screen_rect)
    except Exception as e:
        print(f"[ui_targets] не удалось получить цели: {e}")
        return []


def order_targets(targets: list[UITarget]) -> list[UITarget]:
    """Порядок обхода: меню слева направо → значки справа → панель сверху вниз →
    Dock справа налево (короткие перелёты взгляда, без прыжков через весь экран)."""
    menu = sorted([t for t in targets if t.group == "menu"], key=lambda t: t.x)
    extras = sorted([t for t in targets if t.group == "extras"], key=lambda t: t.x)
    toolbar = sorted([t for t in targets if t.group == "toolbar"], key=lambda t: (t.y, t.x))
    dock = sorted([t for t in targets if t.group == "dock"], key=lambda t: -t.x)
    return menu + extras + toolbar + dock
