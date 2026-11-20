"""Настройки приложения Eye Control.

Хранятся в JSON в домашней директории пользователя (~/.eye_control/settings.json),
профили калибровки — в ~/.eye_control/profiles/.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "Eye Control"
APP_DIR = Path.home() / ".eye_control"
SETTINGS_PATH = APP_DIR / "settings.json"
PROFILES_DIR = APP_DIR / "profiles"
DEFAULT_PROFILE_PATH = PROFILES_DIR / "default.pkl"
DATA_DIR = APP_DIR / "data"          # накопленный датасет всех сессий

PROJECT_DIR = Path(__file__).resolve().parent
FACE_MODEL_PATH = PROJECT_DIR / "assets" / "face_landmarker.task"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

# Режимы клика
CLICK_LEFT = "left"
CLICK_DOUBLE = "double"
CLICK_RIGHT = "right"
CLICK_SCROLL = "scroll"
CLICK_MODES = [CLICK_LEFT, CLICK_DOUBLE, CLICK_RIGHT, CLICK_SCROLL]
CLICK_MODE_LABELS = {
    CLICK_LEFT: "Клик",
    CLICK_DOUBLE: "Двойной",
    CLICK_RIGHT: "Правый",
    CLICK_SCROLL: "Скролл",
}

# Пределы для UI
DWELL_TIME_RANGE = (0.8, 2.5)
KEY_DWELL_RANGE = (0.4, 2.5)
MAGNET_RADIUS_RANGE = (0, 160)
# Размер сетки калибровки: число точек → (столбцы, строки)
CALIB_GRIDS = {9: (3, 3), 16: (4, 4), 25: (5, 5), 35: (7, 5), 48: (8, 6), 63: (9, 7), 80: (10, 8), 99: (11, 9)}
DWELL_RADIUS_RANGE = (20, 120)
SMOOTHING_RANGE = (1, 10)


@dataclass
class Settings:
    # Камера
    camera_index: int = 0
    # Разрешение запрашивается у камеры; важно сохранять то же поле зрения, что у
    # прошлых сессий (признаки — в нормированных координатах кадра). У камер MacBook
    # 640x480 — это уменьшенный полный кадр 1760x1328, а 1280x720 — обрезанный.
    camera_width: int = 640
    camera_height: int = 480

    # Dwell-клик
    dwell_time: float = 1.2           # сек, 0.8 – 2.5
    key_dwell_time: float = 0.8       # сек, для клавиш набора и подсказок
    dwell_radius: int = 45            # px (логические точки экрана)
    click_cooldown: float = 1.0       # сек; пауза перед повторным кликом в той же точке
    click_mode: str = CLICK_LEFT

    # Пауза
    resume_dwell_time: float = 2.0    # удержание взгляда на «Возобновить»
    move_cursor_when_paused: bool = False
    global_hotkey_enabled: bool = not IS_MAC  # pynput-listener на новых macOS бывает нестабилен
    pause_hotkey: str = "<ctrl>+<alt>+p"

    # Скролл
    scroll_zone_ratio: float = 0.2    # верхние/нижние 20% экрана
    scroll_speed: int = 2             # «щелчков» колеса за шаг
    scroll_interval: float = 0.08     # сек между шагами

    # Трекинг / сглаживание
    smoothing: int = 5                # 1 (быстро, шумно) – 10 (плавно, с лагом)
    stabilize_radius: int = 35        # px: «мёртвая зона» курсора (0 — выкл.)
    one_euro_beta: float = 0.004      # насколько быстро фильтр «отпускает» при саккадах
    face_lost_grace: float = 0.3      # сек без лица до постановки на паузу
    ridge_alpha: float = 1.0

    # Калибровка
    calib_settle_time: float = 0.6    # сек: точка показана, сэмплы не собираются
    calib_sample_time: float = 1.2    # сек: сбор сэмплов на точку (1.2–1.5)
    calib_points: int = 35            # 9 / 16 / 25 / 35 / 48 / 63 / 80 / 99
    calib_pursuit_time: float = 20.0  # сек «слежения» за движущейся точкой (0 — выкл.)
    calib_rest_every: int = 12        # перерыв каждые N точек
    calib_rest_time: float = 15.0     # сек отдыха (Пробел — пропустить)
    profile_path: str = str(DEFAULT_PROFILE_PATH)
    quick_adjust_on_start: bool = True  # 5 точек ≈ 8 с при каждом запуске

    # Клавиатура / озвучка
    keyboard_layout: str = "ru"       # "ru" | "en"
    direct_typing: bool = True        # печатать сразу в активное окно
    tts_rate: int = 180               # слов/мин (приблизительно)

    # Внешний вид
    toolbar_button_size: int = 96
    key_size: int = 78
    show_gaze_indicator: bool = True

    # Магнит и лупа
    magnet_enabled: bool = True       # притягивать взгляд к ближайшей кнопке/ссылке
    magnet_radius: int = 70           # px: насколько далеко ищем цель
    magnifier_zoom: float = 3.0       # увеличение лупы

    # Панель команд: дополнительные приложения (пути к .app / .exe / .lnk)
    command_apps: list = field(default_factory=list)

    # ------------------------------------------------------------------
    def smoothing_min_cutoff(self) -> float:
        """Smoothing 1..10 → min_cutoff One Euro (Гц). Меньше = плавнее."""
        s = min(max(self.smoothing, SMOOTHING_RANGE[0]), SMOOTHING_RANGE[1])
        t = (s - 1) / 9.0
        # Логарифмическая интерполяция 3.0 Гц → 0.15 Гц
        return float(3.0 * (0.05 ** t))

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        s = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            known = {f.name: f for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, type(getattr(s, k))(v))
        except FileNotFoundError:
            pass
        except Exception as e:  # повреждённый файл — работаем с дефолтами
            print(f"[config] Не удалось прочитать настройки: {e}", file=sys.stderr)
        s.clamp()
        return s

    def clamp(self) -> None:
        self.dwell_time = min(max(self.dwell_time, DWELL_TIME_RANGE[0]), DWELL_TIME_RANGE[1])
        self.key_dwell_time = min(max(self.key_dwell_time, KEY_DWELL_RANGE[0]), KEY_DWELL_RANGE[1])
        self.command_apps = [str(p) for p in self.command_apps if isinstance(p, str)]
        self.dwell_radius = int(min(max(self.dwell_radius, DWELL_RADIUS_RANGE[0]), DWELL_RADIUS_RANGE[1]))
        self.smoothing = int(min(max(self.smoothing, SMOOTHING_RANGE[0]), SMOOTHING_RANGE[1]))
        self.stabilize_radius = int(min(max(self.stabilize_radius, 0), 100))
        if self.click_mode not in CLICK_MODES:
            self.click_mode = CLICK_LEFT
        if self.calib_points not in CALIB_GRIDS:
            self.calib_points = 35
        self.calib_pursuit_time = min(max(self.calib_pursuit_time, 0.0), 120.0)
        self.magnet_radius = int(min(max(self.magnet_radius, MAGNET_RADIUS_RANGE[0]), MAGNET_RADIUS_RANGE[1]))
        self.magnifier_zoom = min(max(self.magnifier_zoom, 2.0), 5.0)
        if self.keyboard_layout not in ("ru", "en"):
            self.keyboard_layout = "ru"

    def save(self, path: Path = SETTINGS_PATH) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


# Раскладки виртуальной клавиатуры
KEYBOARD_LAYOUTS = {
    "en": [
        list("1234567890"),
        list("qwertyuiop"),
        list("asdfghjkl"),
        list("zxcvbnm,.?"),
    ],
    "ru": [
        list("1234567890"),
        list("йцукенгшщзх"),
        list("фывапролджэ"),
        list("ячсмитьбю.?"),
    ],
}
