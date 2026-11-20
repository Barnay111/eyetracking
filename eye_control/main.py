"""Eye Control — управление компьютером взглядом через обычную веб-камеру.

Точка входа: инициализация Qt, проверка прав ОС, связывание модулей.

    python main.py
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

# Импорты проекта относительно папки eye_control
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QPoint, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QMessageBox  # noqa: E402

from config import (  # noqa: E402
    APP_NAME,
    CLICK_DOUBLE,
    CLICK_RIGHT,
    CLICK_MODE_LABELS,
    CLICK_SCROLL,
    DATA_DIR,
    FACE_MODEL_PATH,
    IS_MAC,
    Settings,
)
from core.dataset import GazeDataset  # noqa: E402
from core.gaze_engine import GazeEngine  # noqa: E402
from core.magnet import Magnet  # noqa: E402
from core.stabilizer import GazeStabilizer  # noqa: E402
from core.ui_targets import UITarget, collect_targets, order_targets  # noqa: E402
from core.os_controller import (  # noqa: E402
    CursorController,
    DwellDetector,
    GlobalHotkeys,
    KeyboardOutput,
    ScrollZoneController,
    check_accessibility_permission,
    open_accessibility_settings,
    launch_app,
    open_camera_settings,
)
from core.tts_engine import TTSEngine  # noqa: E402
from ui.calibration_ui import CalibrationWindow  # noqa: E402
from ui.commands_ui import CommandsWindow  # noqa: E402
from ui.exercise import ExerciseWindow  # noqa: E402
from ui.keyboard_ui import VirtualKeyboard  # noqa: E402
from ui.magnifier import Magnifier  # noqa: E402
from ui.overlay import GazeOverlay  # noqa: E402
from ui.quick_adjust import QuickAdjustWindow  # noqa: E402
from ui.settings_ui import SettingsWindow  # noqa: E402
from ui.toolbar import Toolbar  # noqa: E402


class EyeControlApp:
    def __init__(self, app: QApplication):
        self.app = app
        self.settings = Settings.load()
        self.paused = False
        self._resume_armed = False
        self.calibrating = False
        self._calib_window: CalibrationWindow | None = None
        self._auto_calib_done = False
        self._auto_adjust_done = False
        self._adjust_window = None
        self._camera_error_shown = False
        self._fps = 0.0
        self._fire_flash_until = 0.0
        self._once_mode: str | None = None  # одноразовый режим (двойной клик)
        self._zoom_armed = False
        self._last_external: tuple[float, float] | None = None  # куда смотрели вне своих окон
        self._corrections_unsaved = 0

        # --- Ядро ---
        self.cursor = CursorController()
        self.kb_out = KeyboardOutput()
        self.tts = TTSEngine(self.settings.tts_rate)
        self.dwell = DwellDetector(self.settings.dwell_time, self.settings.dwell_radius, self.settings.click_cooldown)
        self.resume_dwell = DwellDetector(self.settings.resume_dwell_time, 10_000, 1.0)
        self.scroll = ScrollZoneController(self.cursor, self.settings)

        self.dataset = GazeDataset(DATA_DIR)
        self.engine = GazeEngine(self.settings, FACE_MODEL_PATH, self.dataset)
        self._update_desktop_bounds()
        self.engine.gaze.connect(self.on_gaze)
        self.engine.face_changed.connect(self.on_face_changed)
        self.engine.fps_changed.connect(self.on_fps)
        self.engine.error.connect(self.on_engine_error)
        self.engine.camera_ready.connect(self.on_camera_ready)

        # --- UI ---
        self.toolbar = Toolbar(self.settings)
        self.keyboard = VirtualKeyboard(self.settings)
        self.settings_win = SettingsWindow(self.settings, self.dataset)
        self.commands = CommandsWindow(self.settings, self.dataset)
        self.overlay = GazeOverlay()
        self.overlay.radius = self.settings.dwell_radius
        self.magnifier = Magnifier(self.settings.magnifier_zoom)
        self._magnifier_timer = QTimer()
        self._magnifier_timer.setSingleShot(True)
        self._magnifier_timer.timeout.connect(self.magnifier.hide)
        self.magnet = Magnet(self.settings)
        self.stabilizer = GazeStabilizer(self.settings.stabilize_radius)
        self._exercise_window = None

        self.toolbar.calibrate_requested.connect(self.start_calibration)
        self.toolbar.pause_toggled.connect(self.toggle_pause)
        self.toolbar.keyboard_toggled.connect(self.toggle_keyboard)
        self.toolbar.click_mode_changed.connect(self.on_click_mode)
        self.toolbar.double_once_requested.connect(self.toggle_double_once)
        self.toolbar.magnifier_requested.connect(self.toggle_zoom_once)
        self.toolbar.commands_requested.connect(self.toggle_commands)
        self.toolbar.adjust_requested.connect(self.start_quick_adjust)
        self.toolbar.exercise_requested.connect(self.start_exercise)
        self.commands.shortcut.connect(self.on_shortcut)
        self.commands.scroll.connect(self.on_command_scroll)
        self.commands.launch.connect(self.on_launch_app)
        self.commands.open_keyboard.connect(
            lambda: None if self.keyboard.isVisible() else self.toggle_keyboard()
        )
        self.toolbar.settings_requested.connect(self.toggle_settings)
        self.toolbar.quit_requested.connect(self.quit)

        self.keyboard.type_text.connect(self.kb_out.type_text)
        self.keyboard.key_backspace.connect(self.kb_out.backspace)
        self.keyboard.key_enter.connect(self.kb_out.enter)
        self.keyboard.paste_text.connect(self.on_paste)
        self.keyboard.speak.connect(self.tts.speak)
        self.keyboard.closed.connect(lambda: self.toolbar.set_keyboard_visible(False))

        self.settings_win.settings_changed.connect(self.apply_settings)
        self.settings_win.camera_changed.connect(self.engine.set_camera)
        self.settings_win.save_profile.connect(self.save_profile)
        self.settings_win.load_profile.connect(self.load_profile)

        self.hotkeys = None
        if self.settings.global_hotkey_enabled:
            self.hotkeys = GlobalHotkeys(self.settings.pause_hotkey)
            self.hotkeys.pause_toggled.connect(self.toggle_pause)

        QGuiApplication.instance().screenAdded.connect(lambda *_: self._update_desktop_bounds())
        QGuiApplication.instance().screenRemoved.connect(lambda *_: self._update_desktop_bounds())

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(500)

    # ------------------------------------------------------------------
    def start(self):
        self.toolbar.show()
        self.overlay.raise_all()
        self.dataset.log_event("app_start")
        n_s, n_x = self.dataset.stats()
        print(f"[main] Датасет: {n_s} сессий, {n_x} сэмплов ({DATA_DIR})")
        self._load_default_profile()
        self.engine.start()
        self.refresh_status()

    def _save_profile_quiet(self):
        try:
            self.engine.save_profile(self.settings.profile_path)
            self._corrections_unsaved = 0
        except Exception as e:
            print(f"[main] Не удалось сохранить профиль: {e}", file=sys.stderr)

    def quit(self):
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        if self._calib_window is not None:  # ⌘Q посреди калибровки — сохранить собранное
            try:
                self.engine.save_current_session()
            except Exception as e:
                print(f"[main] Не удалось сохранить калибровку: {e}", file=sys.stderr)
        if self._corrections_unsaved and self.engine.is_calibrated:
            self._save_profile_quiet()
        self.engine.save_corrections()
        self.dataset.log_event("app_quit")
        self.settings.save()
        if self.hotkeys:
            self.hotkeys.stop()
        self.tts.stop()
        self.magnet.stop()
        self.engine.stop()
        self.app.quit()

    def _update_desktop_bounds(self):
        r = QRect()
        for s in QGuiApplication.screens():
            r = r.united(s.geometry())
        self.engine.set_desktop_bounds(r.left(), r.top(), r.right() + 1, r.bottom() + 1)

    # ---------------- профили ----------------
    def _load_default_profile(self):
        p = Path(self.settings.profile_path)
        if p.exists():
            try:
                prof = self.engine.load_profile(p)
                print(f"[main] Загружен профиль {p}: точность ≈ {prof.mean_error_px:.0f} px, "
                      f"обучен на {prof.n_train_samples} сэмплах, дообучений: {prof.n_corrections}")
            except Exception as e:
                print(f"[main] Профиль не загружен: {e}", file=sys.stderr)

    def save_profile(self, path: str):
        try:
            self.engine.save_profile(path)
            self.settings.profile_path = path
            self.settings.save()
        except Exception as e:
            QMessageBox.warning(None, APP_NAME, f"Не удалось сохранить профиль:\n{e}")

    def load_profile(self, path: str):
        try:
            self.engine.load_profile(path)
            self.settings.profile_path = path
            self.settings.save()
        except Exception as e:
            QMessageBox.warning(None, APP_NAME, f"Не удалось загрузить профиль:\n{e}")

    # ---------------- сигналы движка ----------------
    def on_camera_ready(self):
        self._camera_error_shown = False
        if (not self.engine.is_calibrated or "--calibrate" in sys.argv) and not self._auto_calib_done:
            # Первый запуск без профиля (или флаг --calibrate): калибровку запускаем сами —
            # без неё пользователь не сможет нажать кнопку взглядом.
            self._auto_calib_done = True
            QTimer.singleShot(800, self.start_calibration)
        elif (self.settings.quick_adjust_on_start and not self._auto_adjust_done
              and "--no-adjust" not in sys.argv):
            # Каждый запуск — 5 точек под сегодняшнюю позу (ошибка ~140 → ~77 px)
            self._auto_adjust_done = True
            QTimer.singleShot(1000, self.start_quick_adjust)

    def on_engine_error(self, msg: str):
        print(f"[engine] {msg}", file=sys.stderr)
        self.toolbar.set_status("⚠️ " + msg[:80])
        if "камеру" in msg and not self._camera_error_shown:
            self._camera_error_shown = True
            box = QMessageBox(QMessageBox.Warning, APP_NAME, msg)
            open_btn = box.addButton("Открыть настройки камеры", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                open_camera_settings()

    def on_fps(self, fps: float):
        self._fps = fps

    def on_face_changed(self, present: bool):
        if present:
            self.overlay.set_state(banner="")
        else:
            # Лицо пропало: курсор остаётся на месте, dwell сбрасывается
            self.dwell.reset()
            self.resume_dwell.reset()
            self.scroll.reset()
            self.overlay.hide_pointer()
            if not self.calibrating:
                self.overlay.set_state(banner="Лицо не найдено — управление на паузе")
        self.refresh_status()

    # ---------------- главный цикл обработки взгляда ----------------
    def _own_window_at(self, p: QPoint):
        for w in self._own_windows():
            if w.frameGeometry().contains(p):
                return w
        return None

    def on_gaze(self, x: float, y: float):
        if self.calibrating:
            return
        # Мёртвая зона: шум веб-камеры не дёргает курсор, саккады проходят сразу
        x, y = self.stabilizer.update(x, y)
        t = time.monotonic()
        p = QPoint(int(x), int(y))
        show = self.settings.show_gaze_indicator

        # --- Пауза: клики выключены, кроме удержания взгляда на «Возобновить»
        if self.paused:
            if self.settings.move_cursor_when_paused:
                self.cursor.move(x, y)
            on_resume = self.toolbar.isVisible() and self.toolbar.pause_button_rect().contains(p)
            if not on_resume:
                # Только что нажали «Пауза» взглядом — сначала нужно отвести взгляд,
                # иначе удержание сразу же снимет паузу.
                self._resume_armed = True
            if on_resume and self._resume_armed:
                st = self.resume_dwell.update(x, y, t, self.settings.resume_dwell_time)
                if st.fired:
                    self.resume_dwell.reset()
                    self.set_paused(False)
                    # Взгляд всё ещё на кнопке: не нажимать её снова, пока не уведут взгляд
                    self.dwell.lock_until_exit(x, y)
                    return
                self.overlay.set_state(pos=(x, y), progress=st.progress, color=GazeOverlay.COLOR_RESUME)
            else:
                self.resume_dwell.reset()
                self.overlay.set_state(pos=(x, y), progress=0.0, color=GazeOverlay.COLOR_PAUSED, scroll_dir=0)
            return

        own = self._own_window_at(p)
        mode = self._once_mode or self.settings.click_mode

        # --- Режим скролла (вне собственных окон)
        if mode == CLICK_SCROLL and own is None:
            self.cursor.move(x, y)
            self.magnet.reset()
            d, prog = self.scroll.update(x, y, t)
            self.dwell.reset()
            self.overlay.set_state(
                pos=(x, y), progress=prog if d else 0.0, color=GazeOverlay.COLOR_SCROLL, scroll_dir=d,
                label="", target_rect=None,
            )
            self.overlay.set_visible(show)
            return
        self.scroll.reset()

        # --- Магнит: фиксация считается по цели, а не по «дрожащему» взгляду.
        # В лупе картинка и так увеличена — там взгляд используется как есть.
        target = None
        if own is not self.magnifier:
            target = self.magnet.update(x, y, own, self._own_buttons(own))
        fx, fy = target.point if target is not None else (x, y)
        self.cursor.move(fx, fy)
        if own is None:
            self._last_external = (fx, fy)

        # Клавиши набора — с коротким dwell (магнит уже держит взгляд на клавише)
        key_btn = target.button if target is not None else (
            own.button_at(p) if own is self.keyboard else None)
        dwell_t = None
        if key_btn is not None and key_btn.window() is self.keyboard and self.keyboard.is_typing_key(key_btn):
            dwell_t = self.settings.key_dwell_time
        st = self.dwell.update(fx, fy, t, dwell_t)
        outside = own is None and (target is None or target.button is None)
        label = ""
        if outside and self._zoom_armed:
            label = "🔍"
        elif outside and mode == CLICK_DOUBLE:
            label = "×2"
        elif outside and mode == CLICK_RIGHT:
            label = "ПКМ"
        if t < self._fire_flash_until:
            color = GazeOverlay.COLOR_FIRE
        elif label:
            color = GazeOverlay.COLOR_DOUBLE
        else:
            color = GazeOverlay.COLOR_ACTIVE
        self.overlay.set_state(pos=(fx, fy), progress=st.progress, color=color, scroll_dir=0, label=label,
                               target_rect=target.rect if target is not None else None)
        self.overlay.set_visible(show)
        if st.fired:
            self._fire_flash_until = t + 0.35
            ax, ay = st.anchor if st.anchor else (fx, fy)
            q = QPoint(int(round(ax)), int(round(ay)))
            btn = target.button if target is not None and target.button is not None else (
                own.button_at(q) if own is not None else None)
            self._dispatch_gaze_click(q, own, mode, target)
            # Повторный клик — только после того, как взгляд ушёл с цели. Иначе магнит
            # держит точку фиксации неподвижной, и клик повторяется каждые ~2 с.
            if not self._is_repeatable(btn):
                self.dwell.lock_until_exit(ax, ay)

    def _is_repeatable(self, btn) -> bool:
        """Кнопки с автоповтором при удержании взгляда: ⌫, прокрутка, «−/+» в настройках."""
        if btn is None:
            return False
        if btn is self.keyboard.btn_bs or btn in self.commands.repeat_buttons:
            return True
        return btn.window() is self.settings_win and btn.text() in ("−", "+")

    def _own_windows(self):
        return [w for w in (self.magnifier, self.settings_win, self.commands, self.keyboard, self.toolbar)
                if w.isVisible()]

    def _own_buttons(self, own):
        wins = [own] if own is not None else self._own_windows()
        out = []
        for w in wins:
            for b in w.findChildren(QAbstractButton):
                if b.isVisible() and b.isEnabled():
                    out.append(b)
        return out

    def _dispatch_gaze_click(self, p: QPoint, own, mode: str, target=None):
        """Клик по точке фиксации.

        Свои окна (панель, клавиатура, настройки, лупа) нажимаем программно —
        это не активирует наше приложение, и фокус ввода остаётся у
        целевого окна (важно для печати с виртуальной клавиатуры).
        """
        btn = target.button if target is not None and target.button is not None else None
        if btn is None and own is not None:
            btn = own.button_at(p)
        if btn is not None:
            win = btn.window()
            # Текст клавиш клавиатуры не пишем в журнал (приватность)
            label = "key" if win is self.keyboard and self.keyboard.is_private(btn) else btn.text()
            self.dataset.log_event("own_button", window=type(win).__name__, button=label, x=p.x(), y=p.y())
            # Дообучение: человек смотрел на эту кнопку → её центр — точная метка
            c = btn.mapToGlobal(btn.rect().center())
            if self.engine.add_correction(c.x(), c.y()):
                self._corrections_unsaved += 1
                if self._corrections_unsaved >= 5:
                    self._save_profile_quiet()
            btn.animateClick()
            if btn is self.toolbar.btn_quit:
                # Для подтверждения выхода нужно отвести взгляд и вернуться
                self.dwell.lock_until_exit(p.x(), p.y())
            return
        if own is self.magnifier:
            self._click_from_magnifier(p, mode)
            return
        if own is not None:
            return  # фон своего окна — ничего не делаем
        if self._zoom_armed:
            self._open_magnifier(p)
            return
        self._system_click(p, mode, role=target.role if target is not None else None)

    def _system_click(self, p: QPoint, mode: str, role=None, via=None):
        self.cursor.move(p.x(), p.y())
        self.dataset.log_event("click", mode=mode, once=self._once_mode is not None, role=role, via=via,
                               x=p.x(), y=p.y())
        try:
            self.cursor.click(mode)
        except Exception as e:
            print(f"[main] click failed: {e}", file=sys.stderr)
        if self._once_mode is not None:
            self._set_once_mode(None)

    # ---------------- лупа ----------------
    def toggle_zoom_once(self):
        self._set_zoom_armed(not self._zoom_armed)

    def _set_zoom_armed(self, armed: bool):
        self._zoom_armed = armed
        self.toolbar.set_zoom_armed(armed)
        if armed:
            self.dataset.log_event("zoom_armed")

    def _open_magnifier(self, p: QPoint):
        self._set_zoom_armed(False)
        if IS_MAC:
            try:
                import Quartz

                if not Quartz.CGPreflightScreenCaptureAccess():
                    Quartz.CGRequestScreenCaptureAccess()
                    self.toolbar.set_status("⚠️ Для лупы нужно право «Запись экрана»")
                    return
            except Exception:
                pass
        src = Magnifier.source_rect(p.x(), p.y(), self.settings.magnifier_zoom)
        # Убираем кольцо взгляда, чтобы оно не попало в снимок
        self.overlay.set_hidden(True)
        QTimer.singleShot(90, lambda: self._grab_and_show(src))

    def _grab_and_show(self, src):
        pix = Magnifier.grab(src)
        self.overlay.set_hidden(False)
        if pix is None:
            self.toolbar.set_status("⚠️ Не удалось снять экран")
            return
        self.magnifier.zoom = self.settings.magnifier_zoom
        self.magnifier.open(src, pix)
        self.overlay.raise_all()
        self.dwell.reset()
        self.magnet.reset()
        self._magnifier_timer.start(15000)
        self.dataset.log_event("zoom_open", x=src.center().x(), y=src.center().y())

    def _click_from_magnifier(self, p: QPoint, mode: str):
        q = self.magnifier.map_to_source(p)
        self.magnifier.hide()
        self._magnifier_timer.stop()
        # Даём окну лупы исчезнуть, чтобы клик попал в приложение под ним
        QTimer.singleShot(80, lambda: self._system_click(q, mode, via="zoom"))

    def toggle_double_once(self):
        self._set_once_mode(None if self._once_mode == CLICK_DOUBLE else CLICK_DOUBLE)

    def _set_once_mode(self, mode):
        self._once_mode = mode
        self.toolbar.set_double_armed(mode == CLICK_DOUBLE)
        self.dataset.log_event("once_mode", mode=mode)
        self.refresh_status()

    # ---------------- действия панели ----------------
    def toggle_pause(self):
        self.set_paused(not self.paused)

    def set_paused(self, paused: bool):
        self.paused = paused
        self._resume_armed = False
        if paused and self._once_mode is not None:
            self._set_once_mode(None)
        if paused:
            if self._zoom_armed:
                self._set_zoom_armed(False)
            self.magnifier.hide()
            self.magnet.reset()
            self.overlay.set_state(target_rect=None)
        self.dataset.log_event("pause" if paused else "resume")
        self.dwell.reset()
        self.resume_dwell.reset()
        self.scroll.reset()
        self.toolbar.set_paused(paused)
        self.overlay.set_state(progress=0.0, color=GazeOverlay.COLOR_PAUSED if paused else GazeOverlay.COLOR_ACTIVE)
        self.refresh_status()

    def toggle_keyboard(self):
        if self.keyboard.isVisible():
            self.keyboard.hide()
        else:
            self.keyboard.show_at_bottom()
            self.overlay.raise_all()
        self.toolbar.set_keyboard_visible(self.keyboard.isVisible())

    def toggle_commands(self):
        if self.commands.isVisible():
            self.commands.hide()
        else:
            self.commands.show_centered()
            self.overlay.raise_all()

    def on_shortcut(self, keys: list):
        self.dataset.log_event("command", keys="+".join(keys))
        try:
            self.kb_out.shortcut(keys)
        except Exception as e:
            print(f"[main] shortcut failed: {e}", file=sys.stderr)

    def on_command_scroll(self, direction: int):
        """Прокрутка окна, на которое смотрели последним (колесо идёт в окно под курсором)."""
        if self._last_external is None:
            self.toolbar.set_status("Сначала посмотрите на окно, которое прокручивать")
            return
        self.dataset.log_event("command", keys="scroll_up" if direction > 0 else "scroll_down")
        self.cursor.move(*self._last_external)
        QTimer.singleShot(30, lambda: self.cursor.scroll(direction * 5))

    def on_launch_app(self, name: str, path: str):
        self.dataset.log_event("app_launch", name=name, path=path)
        try:
            launch_app(path)
        except Exception as e:
            print(f"[main] не удалось запустить {path}: {e}", file=sys.stderr)

    def toggle_settings(self):
        if self.settings_win.isVisible():
            self.settings_win.hide()
        else:
            self.settings_win.show_centered()
            self.overlay.raise_all()

    def on_click_mode(self, mode: str):
        self.settings.save()
        self.dataset.log_event("click_mode", mode=mode)
        self.dwell.reset()
        self.refresh_status()

    def on_paste(self, text: str):
        QGuiApplication.clipboard().setText(text)
        QTimer.singleShot(80, self.kb_out.paste)

    def apply_settings(self):
        s = self.settings
        self.dwell.dwell_time = s.dwell_time
        self.dwell.radius = s.dwell_radius
        self.dwell.cooldown = s.click_cooldown
        self.overlay.radius = s.dwell_radius
        self.stabilizer.radius = s.stabilize_radius
        self.engine.set_smoothing(s.smoothing_min_cutoff(), s.one_euro_beta)
        self.overlay.set_visible(s.show_gaze_indicator)

    # ---------------- калибровка ----------------
    def start_calibration(self):
        if self.calibrating:
            return
        self.calibrating = True
        self.dwell.reset()
        self._kb_was_visible = self.keyboard.isVisible()
        self._settings_was_visible = self.settings_win.isVisible()
        for w in (self.toolbar, self.keyboard, self.settings_win, self.magnifier, self.commands):
            w.hide()
        self.magnet.reset()
        self.overlay.hide_pointer()
        self.overlay.set_hidden(True)
        screen = QGuiApplication.primaryScreen()
        self._calib_window = CalibrationWindow(self.engine, self.settings, screen)
        self._calib_window.finished.connect(self.on_calibration_finished)
        self._calib_window.start()

    def on_calibration_finished(self, ok: bool, msg: str):
        self.calibrating = False
        self._calib_window = None
        self.overlay.set_hidden(False)
        self.toolbar.show()
        if self._kb_was_visible:
            self.keyboard.show_at_bottom()
        if self._settings_was_visible:
            self.settings_win.show_centered()
        self.overlay.raise_all()
        if ok:
            try:
                self.engine.save_profile(self.settings.profile_path)
                self.settings.save()
            except Exception as e:
                print(f"[main] Не удалось сохранить профиль: {e}", file=sys.stderr)
        print(f"[main] {msg}")
        prof = self.engine.profile
        self.dataset.log_event("calibration", ok=ok, points=self.settings.calib_points,
                               pursuit=self.settings.calib_pursuit_time,
                               error_px=round(prof.mean_error_px, 1) if (ok and prof) else None)
        self.settings_win.refresh_stats()
        self.toolbar.set_status(msg)
        QTimer.singleShot(4000, self.refresh_status)
        if ok and "--exercise" in sys.argv and not getattr(self, "_ex_auto_done", False):
            # --calibrate --exercise: после калибровки сразу тренировка по реальным целям
            self._ex_auto_done = True
            QTimer.singleShot(2500, self.start_exercise)

    # ---------------- тренировка по реальным целям ----------------
    def start_exercise(self):
        if self.calibrating:
            return
        if not self.engine.is_calibrated:
            self.start_calibration()
            return
        screen = QGuiApplication.primaryScreen()
        g = screen.geometry()
        targets = collect_targets((g.x(), g.y(), g.width(), g.height()))
        for b in (self.toolbar.btn_calib, self.toolbar.btn_adjust, self.toolbar.btn_keyboard,
                  self.toolbar.btn_commands, self.toolbar.btn_settings, self.toolbar.btn_pause):
            tl = b.mapToGlobal(b.rect().topLeft())
            targets.append(UITarget(tl.x(), tl.y(), b.width(), b.height(),
                                    b.text().replace("\n", " "), "toolbar"))
        targets = order_targets(targets)
        if len(targets) < 5:
            self.toolbar.set_status("Не нашёл целей для тренировки (нужен доступ Accessibility)")
            return
        self.calibrating = True
        self.dwell.reset()
        self.magnet.reset()
        self._ex_restore = [w for w in (self.keyboard, self.settings_win, self.commands) if w.isVisible()]
        for w in self._ex_restore + [self.magnifier]:
            w.hide()
        self.overlay.hide_pointer()
        self.overlay.set_hidden(True)
        self._exercise_window = ExerciseWindow(self.engine, screen, targets)
        self._exercise_window.finished.connect(self.on_exercise_finished)
        self.engine.gaze.connect(self._exercise_window.on_gaze)
        self.dataset.log_event("exercise_start", n_targets=len(targets))
        self._exercise_window.start()

    def on_exercise_finished(self, ok: bool, msg: str, summary: dict):
        try:
            self.engine.gaze.disconnect(self._exercise_window.on_gaze)
        except Exception:
            pass
        self.calibrating = False
        self._exercise_window = None
        self.overlay.set_hidden(False)
        for w in self._ex_restore:
            w.show_at_bottom() if w is self.keyboard else w.show()
        self.overlay.raise_all()
        print(f"[main] {msg}")
        self.dataset.log_event("exercise", ok=ok, **{f"miss_{k}": v for k, v in summary.items()})
        self.toolbar.set_status(msg[:120])
        if ok:
            QTimer.singleShot(5000, self._save_profile_quiet)
        QTimer.singleShot(8000, self.refresh_status)

    # ---------------- быстрая подстройка ----------------
    def start_quick_adjust(self):
        if self.calibrating:
            return
        if not self.engine.is_calibrated:
            self.start_calibration()
            return
        self.calibrating = True  # взгляд не управляет курсором, пока идёт подстройка
        self.dwell.reset()
        self.magnet.reset()
        self._adj_restore = [w for w in (self.toolbar, self.keyboard, self.settings_win, self.commands)
                             if w.isVisible()]
        for w in self._adj_restore + [self.magnifier]:
            w.hide()
        self.overlay.hide_pointer()
        self.overlay.set_hidden(True)
        self._adjust_window = QuickAdjustWindow(self.engine, QGuiApplication.primaryScreen())
        self._adjust_window.finished.connect(self.on_quick_adjust_finished)
        self._adjust_window.start()

    def on_quick_adjust_finished(self, ok: bool, msg: str):
        self.calibrating = False
        self._adjust_window = None
        self.overlay.set_hidden(False)
        for w in self._adj_restore:
            if w is self.keyboard:
                w.show_at_bottom()
            else:
                w.show()
        if self.toolbar not in self._adj_restore:
            self.toolbar.show()
        self.overlay.raise_all()
        print(f"[main] {msg}")
        bx, by = self.engine.drift
        self.dataset.log_event("quick_adjust", ok=ok, drift_x=round(bx), drift_y=round(by))
        self.toolbar.set_status(msg)
        if ok:  # модель дообучается в фоне (~1–2 с) — потом сохраняем профиль
            QTimer.singleShot(4000, self._save_profile_quiet)
        QTimer.singleShot(5000, self.refresh_status)
        if "--exercise" in sys.argv and not getattr(self, "_ex_auto_done", False):
            # Запуск с флагом --exercise: тренировка сразу после подстройки, без нажатий
            self._ex_auto_done = True
            QTimer.singleShot(2500, self.start_exercise)

    # ------------------------------------------------------------------
    def refresh_status(self):
        if self.calibrating:
            return
        parts = []
        if not self.engine.is_calibrated:
            parts.append("Нужна калибровка")
        elif not self.engine.face_present:
            parts.append("🔴 Лицо не найдено")
        else:
            parts.append("🟢 Трекинг")
        if self.paused:
            parts.append("⏸ Пауза")
        else:
            parts.append("×2 Двойной (1 раз)" if self._once_mode == CLICK_DOUBLE
                         else CLICK_MODE_LABELS[self.settings.click_mode])
        parts.append(f"{self._fps:.0f} fps")
        self.toolbar.set_status("\n".join(parts))


# ---------------------------------------------------------------------------
def check_permissions() -> None:
    if IS_MAC and not check_accessibility_permission():
        box = QMessageBox(
            QMessageBox.Warning,
            APP_NAME,
            "Для управления курсором и кликами нужно разрешение «Универсальный доступ».\n\n"
            "Откройте: Системные настройки → Конфиденциальность и безопасность → "
            "Универсальный доступ и включите приложение, из которого запущен Eye Control "
            "(Terminal / iTerm / PyCharm / Python). После этого перезапустите программу.",
        )
        open_btn = box.addButton("Открыть настройки", QMessageBox.ActionRole)
        box.addButton("Продолжить", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            open_accessibility_settings()


def main() -> int:
    if not FACE_MODEL_PATH.exists():
        print(f"Не найдена модель {FACE_MODEL_PATH}. См. README.", file=sys.stderr)
        return 1

    # Логи сразу в файл/терминал, без буферизации
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    check_permissions()
    ctrl = EyeControlApp(app)
    ctrl.start()
    # ⌘Q / закрытие из Dock идут мимо кнопки «Выход» — сохраняем так же
    app.aboutToQuit.connect(ctrl.quit)

    # SIGTERM/SIGINT (kill, Ctrl+C) → штатный выход с сохранением профиля и дообучений.
    # Python-обработчик сигнала выполняется, только когда интерпретатор получает
    # управление, поэтому раз в 200 мс «будим» его пустым таймером.
    def _graceful(*_):
        print("[main] Получен сигнал завершения — сохраняю и выхожу")
        QTimer.singleShot(0, ctrl.quit)

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(200)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
