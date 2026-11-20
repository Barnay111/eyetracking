"""Gaze engine: камера → MediaPipe Face Landmarker → признаки → Ridge → One Euro.

Извлечение признаков портировано из EyeTrax (MIT, (c) 2024 ck-zhang):
3D-ориентиры глаз/радужки нормализуются в систему координат головы
(центр между уголками глаз, ось X — межглазная линия, масштаб — межглазное
расстояние), к ним добавляются углы поворота головы (yaw/pitch/roll) и
положение/масштаб лица в кадре. Благодаря этому смещение и поворот головы
после калибровки сбивают прицел значительно меньше, чем «сырые» координаты
радужки.

Весь захват и инференс выполняются в отдельном QThread; наружу уходят только
Qt-сигналы, которые доставляются в GUI-поток через очередь событий.
"""
from __future__ import annotations

import math
import pickle
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

from core.dataset import GazeDataset

# ---------------------------------------------------------------------------
# Индексы ориентиров MediaPipe (из EyeTrax)
# fmt: off
LEFT_EYE_INDICES = [
    107, 66, 105, 63, 70, 55, 65, 52, 53, 46,
    468, 469, 470, 471, 472,
    133, 33,
    173, 157, 158, 159, 160, 161, 246,
    155, 154, 153, 145, 144, 163, 7,
    243, 190, 56, 28, 27, 29, 30, 247,
    130, 25, 110, 24, 23, 22, 26, 112,
    244, 189, 221, 222, 223, 224, 225, 113,
    226, 31, 228, 229, 230, 231, 232, 233,
    193, 245, 128, 121, 120, 119, 118, 117,
    111, 35, 124, 143, 156,
]
RIGHT_EYE_INDICES = [
    336, 296, 334, 293, 300, 285, 295, 282, 283, 276,
    473, 476, 475, 474, 477,
    362, 263,
    398, 384, 385, 386, 387, 388, 466,
    382, 381, 380, 374, 373, 390, 249,
    463, 414, 286, 258, 257, 259, 260, 467,
    359, 255, 339, 254, 253, 252, 256, 341,
    464, 413, 441, 442, 443, 444, 445, 342,
    446, 261, 448, 449, 450, 451, 452, 453,
    417, 465, 357, 350, 349, 348, 347, 346,
    340, 265, 353, 372, 383,
]
MUTUAL_INDICES = [4, 10, 151, 9, 152, 234, 454, 58, 288]
# fmt: on
SUBSET_INDICES = LEFT_EYE_INDICES + RIGHT_EYE_INDICES + MUTUAL_INDICES

PROFILE_VERSION = 3


# ---------------------------------------------------------------------------
# Сглаживание
class OneEuroFilter:
    """One Euro Filter (Casiez et al., 2012) для 2D-точки.

    min_cutoff — частота среза в покое (меньше → меньше тремора при фиксации);
    beta — насколько частота среза растёт со скоростью (больше → меньше лага
    при быстром переводе взгляда).
    """

    def __init__(self, min_cutoff: float = 0.8, beta: float = 0.004, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self) -> None:
        self._x: np.ndarray | None = None
        self._dx = np.zeros(2)
        self._t: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: np.ndarray, t: float) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if self._x is None or self._t is None:
            self._x, self._t = x.copy(), t
            return x
        dt = max(t - self._t, 1e-3)
        self._t = t
        dx = (x - self._x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self._dx = a_d * dx + (1 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * float(np.linalg.norm(self._dx))
        a = self._alpha(cutoff, dt)
        self._x = a * x + (1 - a) * self._x
        return self._x.copy()


# ---------------------------------------------------------------------------
# Извлечение признаков
class FeatureExtractor:
    def __init__(self, model_path: Path, blink_ratio: float = 0.75, ear_history: int = 60):
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import vision
            from mediapipe.tasks.python.core.base_options import BaseOptions
        except Exception as e:  # pragma: no cover
            raise ImportError(
                "Не удалось импортировать mediapipe. Установите зависимости: "
                "pip install -r requirements.txt (нужен numpy<2)."
            ) from e
        if not Path(model_path).exists():
            raise FileNotFoundError(f"Не найдена модель FaceLandmarker: {model_path}")

        self._mp = mp
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        self._lm = vision.FaceLandmarker.create_from_options(options)
        self._last_ts = 0
        self._ear_hist: deque[float] = deque(maxlen=ear_history)
        self._blink_ratio = blink_ratio

    def close(self) -> None:
        if self._lm is not None:
            self._lm.close()
            self._lm = None

    def extract(self, frame_bgr: np.ndarray) -> tuple[np.ndarray | None, bool]:
        """Возвращает (features | None, blink)."""
        rgb = np.ascontiguousarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        ts = int(time.monotonic() * 1000)
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts

        result = self._lm.detect_for_video(image, ts)
        if not result.face_landmarks:
            return None, False
        lms = result.face_landmarks[0]
        pts = np.array([(p.x, p.y, p.z) for p in lms], dtype=np.float32)
        self.last_pts = pts
        if pts.shape[0] <= max(SUBSET_INDICES):
            raise RuntimeError("Модель FaceLandmarker не возвращает ориентиры радужки (нужно 478 точек).")

        left_corner, right_corner, top = pts[33], pts[263], pts[10]
        center = (left_corner + right_corner) / 2.0

        # Базис головы
        x_axis = right_corner - left_corner
        x_axis /= np.linalg.norm(x_axis) + 1e-9
        y_approx = top - center
        y_approx /= np.linalg.norm(y_approx) + 1e-9
        y_axis = y_approx - np.dot(y_approx, x_axis) * x_axis
        y_axis /= np.linalg.norm(y_axis) + 1e-9
        z_axis = np.cross(x_axis, y_axis)
        z_axis /= np.linalg.norm(z_axis) + 1e-9
        R = np.column_stack((x_axis, y_axis, z_axis))

        local = (R.T @ (pts - center).T).T
        inter_eye = float(np.linalg.norm(R.T @ (right_corner - left_corner)))
        if inter_eye > 1e-7:
            local /= inter_eye

        eye_feats = local[SUBSET_INDICES].flatten()

        # Поза головы (углы) + положение/масштаб лица в кадре — компенсация сдвига головы
        yaw = math.atan2(R[1, 0], R[0, 0])
        pitch = math.atan2(-R[2, 0], math.sqrt(R[2, 1] ** 2 + R[2, 2] ** 2))
        roll = math.atan2(R[2, 1], R[2, 2])
        img_eye_dist = float(np.linalg.norm(right_corner[:2] - left_corner[:2]))
        head = np.array([yaw, pitch, roll, center[0], center[1], img_eye_dist], dtype=np.float32)

        features = np.concatenate([eye_feats, head]).astype(np.float32)

        # Моргание по EAR с адаптивным порогом
        def ear(inner, outer, upper, lower):
            w = np.linalg.norm(pts[outer, :2] - pts[inner, :2])
            h = np.linalg.norm(pts[upper, :2] - pts[lower, :2])
            return h / (w + 1e-9)

        e = (ear(133, 33, 159, 145) + ear(362, 263, 386, 374)) / 2.0
        self._ear_hist.append(float(e))
        thr = float(np.median(self._ear_hist)) * self._blink_ratio if len(self._ear_hist) >= 15 else 0.18
        return features, bool(e < thr)


# ---------------------------------------------------------------------------
# Регрессионная модель
ALPHA_GRID = (3.0, 10.0, 30.0, 100.0, 300.0, 1000.0)
ALPHA_TOLERANCE = 1.2  # см. select_alpha


def bad_points(errs: dict, min_px: float = 250.0, factor: float = 3.0) -> set:
    """Точки, на которые человек, скорее всего, не смотрел (моргнул/отвлёкся/не успел перевести взгляд):
    ошибка > factor·медианы и > min_px."""
    if len(errs) < 4:
        return set()
    med = float(np.median(list(errs.values())))
    return {p for p, e in errs.items() if e > max(factor * med, min_px)}
W_CURRENT = 3.0        # вес текущей калибровки (сегодняшняя поза/свет важнее)
W_CORRECTION = 2.0     # вес дообучения по кликам из прошлых запусков
W_LIVE_CORRECTION = 10.0  # клики этого запуска: сегодняшняя поза
W_ADJUST = 100.0       # точки подстройки этого запуска (проверено на данных: 142 → 77 px)
W_EXERCISE = 30.0      # упражнение по реальным целям (Dock, меню) в этом запуске
W_HISTORY = 1.0        # вес прошлых сессий
HISTORY_CAP = 40_000   # максимум сэмплов истории в обучении (самые свежие)
CV_HISTORY_CAP = 10_000
MAX_CORRECTION_SAMPLES = 3_000

# Поправка сдвига: общий сдвиг прогноза по последним «якорям» (фиксации с известной
# целью — клики по своим кнопкам и точки подстройки). Ловит смену позы за 3–5 кликов,
# не дожидаясь переобучения модели.
DRIFT_ANCHORS = 8          # сколько последних якорей учитывать
DRIFT_HALF_LIFE = 300.0    # сек: вес якоря падает вдвое за 5 минут
DRIFT_PRIOR = 1.0          # «вес» нулевого сдвига — один клик не дёргает поправку целиком
DRIFT_MAX = 200.0          # px: больше — это уже не сдвиг, нужна калибровка
PURSUIT_KIND = "pursuit"


class GazeModel:
    """StandardScaler + Ridge: признаки → (x, y) в логических координатах экрана."""

    def __init__(self, alpha: float = 1.0):
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        self.alpha = alpha
        self.pipeline = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
        self.feature_dim: int | None = None

    def fit(self, X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> "GazeModel":
        if w is None:
            self.pipeline.fit(X, y)
        else:
            self.pipeline.fit(X, y, ridge__sample_weight=w)
        self.feature_dim = X.shape[1]
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.pipeline.predict(X)


def filter_outliers(X: np.ndarray, groups: np.ndarray, keep: float = 0.8) -> np.ndarray:
    """Маска: внутри каждой точки оставляем keep·100% сэмплов, ближайших к медиане.

    Отсекает кадры с полуприкрытыми глазами, остаточные саккады, рывки головы.
    """
    mask = np.ones(len(X), dtype=bool)
    for g in np.unique(groups):
        idx = np.flatnonzero(groups == g)
        if len(idx) < 8:
            continue
        Xg = X[idx]
        scale = Xg.std(axis=0) + 1e-6
        d = np.linalg.norm((Xg - np.median(Xg, axis=0)) / scale, axis=1)
        mask[idx[d > np.quantile(d, keep)]] = False
    return mask


def point_errors(model: GazeModel, X: np.ndarray, y: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """Ошибка по точкам: расстояние от медианы предсказаний точки до её цели."""
    pred = model.predict(X)
    errs = []
    for g in np.unique(groups):
        m = groups == g
        errs.append(float(np.linalg.norm(np.median(pred[m], axis=0) - y[m][0])))
    return np.array(errs)


def _subsample(X, y, cap, rng):
    if len(X) <= cap:
        return X, y
    idx = rng.choice(len(X), cap, replace=False)
    return X[idx], y[idx]


def select_alpha(Xc, yc, gc, test_mask, Xh, yh, rng) -> tuple[float, float, dict]:
    """Подбор alpha: GroupKFold по точкам текущей сессии.

    История всегда в обучении, из текущей сессии по очереди убирается ~1/5 точек,
    модель угадывает их. Тестируем только на точках сетки (не на «слежении»).
    Возвращает (alpha, ошибка_px, {точка: ошибка_px} для выбранного alpha).
    """
    Xh_cv, yh_cv = _subsample(Xh, yh, CV_HISTORY_CAP, rng)
    test_groups = np.unique(gc[test_mask])
    n_folds = int(min(5, len(test_groups)))
    folds = np.array_split(rng.permutation(test_groups), n_folds)
    scores: list[tuple[float, float]] = []
    per_point: dict[float, dict] = {}
    for a in ALPHA_GRID:
        errs = []
        pp = {}
        for fold in folds:
            held = np.isin(gc, fold)
            Xtr = np.vstack([Xh_cv, Xc[~held]]) if len(Xh_cv) else Xc[~held]
            ytr = np.vstack([yh_cv, yc[~held]]) if len(yh_cv) else yc[~held]
            wtr = np.concatenate([np.full(len(Xh_cv), W_HISTORY), np.full(int((~held).sum()), W_CURRENT)])
            m = GazeModel(a).fit(Xtr, ytr, wtr)
            te = held & test_mask
            pe = point_errors(m, Xc[te], yc[te], gc[te])
            errs.extend(pe.tolist())
            pp.update(zip(np.unique(gc[te]).tolist(), pe.tolist()))
        per_point[a] = pp
        # медиана устойчива к единичным «недосмотренным» точкам
        scores.append((a, float(np.median(errs))))
    # Кросс-валидация внутри одной сессии не видит дрейфа позы головы и поощряет слабую
    # регуляризацию. На реальных данных устойчивее самая сильная регуляризация, чья ошибка
    # не более чем на ALPHA_TOLERANCE хуже лучшей (аналог правила «одной стандартной ошибки»).
    best_err = min(e for _, e in scores)
    a, e = max((a, e) for a, e in scores if e <= best_err * ALPHA_TOLERANCE)
    return a, e, per_point[a]


@dataclass
class CalibrationProfile:
    """Обученная модель + метаданные. Сами данные живут в датасете (core/dataset.py)."""

    model: GazeModel
    screen_geometry: tuple[int, int, int, int]  # x, y, w, h (логические)
    session_id: str = ""
    alpha: float = 1.0
    created: float = field(default_factory=time.time)
    cv_error_px: float = 0.0                    # кросс-валидация по точкам калибровки
    validation_error_px: float | None = None    # ошибка на проверочных точках
    n_corrections: int = 0
    n_train_samples: int = 0
    n_history_sessions: int = 0
    version: int = PROFILE_VERSION

    @property
    def mean_error_px(self) -> float:
        return self.validation_error_px if self.validation_error_px is not None else self.cv_error_px

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(self, fh)
        tmp.replace(p)

    @staticmethod
    def load(path: str | Path) -> "CalibrationProfile":
        with Path(path).open("rb") as fh:
            prof = pickle.load(fh)
        if not isinstance(prof, CalibrationProfile) or getattr(prof, "version", 0) != PROFILE_VERSION:
            raise ValueError("Профиль калибровки от старой версии программы — нужна новая калибровка")
        return prof


# ---------------------------------------------------------------------------
class GazeEngine(QThread):
    """Фоновый поток: камера + трекинг. Обучение — на всей накопленной истории.

    Сигналы:
        gaze(x, y)          — сглаженная точка взгляда (логические глобальные координаты)
        face_changed(bool)  — лицо/глаза в кадре (с гистерезисом face_lost_grace)
        fps_changed(float)
        error(str)
        camera_ready()
    """

    gaze = Signal(float, float)
    face_changed = Signal(bool)
    fps_changed = Signal(float)
    error = Signal(str)
    camera_ready = Signal()

    def __init__(self, settings, model_path: Path, dataset: GazeDataset | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.dataset = dataset
        self._model_path = model_path
        self._running = False
        self._lock = threading.Lock()
        self._rng = np.random.default_rng()

        self._profile: CalibrationProfile | None = None
        self._filter = OneEuroFilter(settings.smoothing_min_cutoff(), settings.one_euro_beta)
        self._camera_index = settings.camera_index
        self._camera_changed = False

        # Текущая сессия калибровки
        self._session_id = ""
        self._session_geometry: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._collect_target: tuple[float, float] | None = None
        self._collect_kind = "grid"
        self._cur_X: list[np.ndarray] = []
        self._cur_y: list[tuple[float, float]] = []
        self._cur_g: list[int] = []
        self._cur_k: list[str] = []
        self._cur_N: list[np.ndarray] = []  # ответы нейросети (эксперимент)
        self._point_id = 0

        # База для переобучения (история + текущая калибровка), без дообучения
        self._base_X: np.ndarray | None = None
        self._base_y: np.ndarray | None = None
        self._base_w: np.ndarray | None = None
        # Дообучение по кликам (этот запуск программы)
        self._corr_X: list[np.ndarray] = []
        self._corr_y: list[tuple[float, float]] = []
        self._corr_session_id = GazeDataset.new_session_id("corr")
        # Подстройка при запуске (этот запуск программы)
        self._adj_X: list[np.ndarray] = []
        self._adj_y: list[tuple[float, float]] = []
        self._adj_pending: list = []  # точки текущей подстройки до проверки согласованности
        # Упражнение «Тренировка» по реальным целям (этот запуск)
        self._ex_X: list[np.ndarray] = []
        self._ex_y: list[tuple[float, float]] = []
        self._refit_pending = False
        self._refit_thread: threading.Thread | None = None

        # Последние кадры (t, features) — для дообучения по кликам и подстройки
        self._recent: deque[tuple[float, np.ndarray]] = deque(maxlen=150)
        # Якоря для поправки сдвига: (время, кадры, цель)
        self._anchors: deque[tuple[float, np.ndarray, np.ndarray]] = deque(maxlen=DRIFT_ANCHORS)
        self._bias = np.zeros(2)

        self._face_present = False
        self._bounds: tuple[float, float, float, float] | None = None  # x0, y0, x1, y1

    # ---------------- публичный API (вызывается из GUI-потока) -------------
    @property
    def is_calibrated(self) -> bool:
        return self._profile is not None

    @property
    def face_present(self) -> bool:
        return self._face_present

    @property
    def profile(self) -> CalibrationProfile | None:
        return self._profile

    def set_desktop_bounds(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """Границы виртуального рабочего стола — для ограничения предсказаний."""
        self._bounds = (x0, y0, x1, y1)

    def set_smoothing(self, min_cutoff: float, beta: float) -> None:
        with self._lock:
            self._filter.min_cutoff = min_cutoff
            self._filter.beta = beta

    def set_camera(self, index: int) -> None:
        with self._lock:
            if index != self._camera_index:
                self._camera_index = index
                self._camera_changed = True

    def stop(self) -> None:
        self._running = False
        self.wait(3000)
        if self._refit_thread is not None:
            self._refit_thread.join(timeout=5)

    # ---------------- сессия калибровки ----------------
    def start_session(self, screen_geometry: tuple[int, int, int, int]) -> None:
        with self._lock:
            self._session_id = GazeDataset.new_session_id("calib")
            self._session_geometry = tuple(screen_geometry)
            self._cur_X.clear()
            self._cur_N.clear()
            self._cur_y.clear()
            self._cur_g.clear()
            self._cur_k.clear()
            self._point_id = 0
            self._collect_target = None

    # совместимость со старым API
    def clear_samples(self) -> None:
        self.start_session(self._session_geometry)

    def begin_collect(self, x: float, y: float, kind: str = "grid") -> None:
        """Новая точка (новая группа) с целью (x, y)."""
        with self._lock:
            self._point_id += 1
            self._collect_kind = kind
            self._collect_target = (float(x), float(y))

    def update_target(self, x: float, y: float) -> None:
        """Сдвинуть цель без смены группы (для «слежения» за движущейся точкой)."""
        with self._lock:
            if self._collect_target is not None:
                self._collect_target = (float(x), float(y))

    def end_collect(self) -> None:
        with self._lock:
            self._collect_target = None

    def sample_count(self) -> int:
        with self._lock:
            return len(self._cur_X)

    def _current_arrays(self):
        with self._lock:
            X = np.array(self._cur_X, dtype=np.float32)
            y = np.array(self._cur_y, dtype=np.float32).reshape(-1, 2)
            g = np.array(self._cur_g, dtype=np.int32)
            k = np.array(self._cur_k, dtype="U12")
        return X, y, g, k

    def save_current_session(self) -> int:
        """Сохранить все сэмплы текущей сессии в датасет (в т.ч. прерванной). Возвращает их число."""
        if self.dataset is None or not self._session_id:
            return 0
        X, y, g, k = self._current_arrays()
        if len(X) == 0:
            return 0
        with self._lock:
            N = np.array(self._cur_N, dtype=np.float32).reshape(-1, 2)
        self.dataset.save_session(self._session_id, X, y, g, k, self._session_geometry,
                                  {"camera_index": self._camera_index,
                                   "camera_size": [self.settings.camera_width, self.settings.camera_height]},
                                  neural=N if len(N) == len(X) else None)
        return len(X)

    def _load_history(self, feature_dim: int, exclude: set[str]) -> tuple[np.ndarray, np.ndarray, int]:
        """Прошлые сессии с той же геометрией экрана — самые свежие, не больше HISTORY_CAP сэмплов."""
        if self.dataset is None:
            return np.zeros((0, feature_dim), np.float32), np.zeros((0, 2), np.float32), 0
        sessions = self.dataset.load_sessions(feature_dim, self._session_geometry, exclude)
        sessions.sort(key=lambda s: s.created, reverse=True)
        Xs, ys, total = [], [], 0
        for s in sessions:
            Xs.append(s.X)
            ys.append(s.y)
            total += len(s.X)
            if total >= HISTORY_CAP:
                break
        if not Xs:
            return np.zeros((0, feature_dim), np.float32), np.zeros((0, 2), np.float32), 0
        X, y = np.vstack(Xs)[:HISTORY_CAP], np.vstack(ys)[:HISTORY_CAP]
        return X, y, len(Xs)

    def train(self, screen_geometry: tuple[int, int, int, int] | None = None) -> tuple[bool, str, float]:
        """Обучение: текущая сессия (сетка + слежение) + вся совместимая история.

        Можно вызывать из рабочего потока. Возвращает (ok, msg, cv_err_px).
        """
        if screen_geometry is not None:
            self._session_geometry = tuple(screen_geometry)
        X, y, g, k = self._current_arrays()
        train_mask = k != "val"
        X, y, g, k = X[train_mask], y[train_mask], g[train_mask], k[train_mask]
        grid_points = len(np.unique(g[k == "grid"])) if len(g) else 0
        if len(X) < 30 or grid_points < 6:
            return False, (f"Недостаточно данных ({len(X)} сэмплов, {grid_points} точек). "
                           "Проверьте освещение и положение лица."), 0.0
        keep = filter_outliers(X, g)
        X, y, g, k = X[keep], y[keep], g[keep], k[keep]

        Xh, yh, n_hist = self._load_history(X.shape[1], {self._session_id})
        alpha, cv_err, pt_err = select_alpha(X, y, g, k == "grid", Xh, yh, self._rng)
        bad = bad_points(pt_err)
        if bad:
            keep = ~np.isin(g, list(bad))
            X, y, g, k = X[keep], y[keep], g[keep], k[keep]
            print(f"[engine] выброшено точек сетки (вероятно, не смотрели): {len(bad)} — "
                  + ", ".join(f"{pt_err[p]:.0f}px" for p in sorted(bad)), flush=True)

        bX = np.vstack([Xh, X]) if len(Xh) else X
        by = np.vstack([yh, y]) if len(yh) else y
        bw = np.concatenate([np.full(len(Xh), W_HISTORY), np.full(len(X), W_CURRENT)])
        model = GazeModel(alpha).fit(bX, by, bw)
        profile = CalibrationProfile(model=model, screen_geometry=self._session_geometry,
                                     session_id=self._session_id, alpha=alpha, cv_error_px=cv_err,
                                     n_train_samples=len(bX), n_history_sessions=n_hist)
        with self._lock:
            self._base_X, self._base_y, self._base_w = bX, by, bw
            self._corr_X.clear()
            self._corr_y.clear()
            self._profile = profile
            self._filter.reset()
            self._reset_drift()
        print(f"[engine] обучено: сессия {len(X)} сэмплов ({grid_points} точек) + история "
              f"{len(Xh)} сэмплов из {n_hist} сессий; alpha={alpha:g}; CV-ошибка ≈ {cv_err:.0f} px", flush=True)
        return True, f"Модель обучена, ошибка ≈ {cv_err:.0f} px", cv_err

    def finish_validation(self) -> tuple[bool, str, float]:
        """Ошибка на проверочных точках (модель их не видела); затем они идут в обучение,
        а вся сессия сохраняется в датасет."""
        with self._lock:
            prof = self._profile
        X, y, g, k = self._current_arrays()
        vm = k == "val"
        Xv, yv, gv = X[vm], y[vm], g[vm]
        if prof is None:
            return False, "Нет обученной модели", 0.0
        n_saved = self.save_current_session()
        if len(Xv) < 10:
            return True, f"Готово. Ошибка ≈ {prof.cv_error_px:.0f} px", prof.cv_error_px
        keep = filter_outliers(Xv, gv)
        Xv, yv, gv = Xv[keep], yv[keep], gv[keep]
        pe = point_errors(prof.model, Xv, yv, gv)
        errs = dict(zip(np.unique(gv).tolist(), pe.tolist()))
        bad = bad_points(errs, factor=2.5)
        if bad:
            print(f"[engine] проверочные точки отброшены (вероятно, не смотрели): "
                  + ", ".join(f"{errs[p]:.0f}px" for p in sorted(bad)), flush=True)
            keep = ~np.isin(gv, list(bad))
            Xv, yv, gv = Xv[keep], yv[keep], gv[keep]
        val_err = float(np.mean([e for p, e in errs.items() if p not in bad]))

        with self._lock:
            bX = np.vstack([self._base_X, Xv])
            by = np.vstack([self._base_y, yv])
            bw = np.concatenate([self._base_w, np.full(len(Xv), W_CURRENT)])
        model = GazeModel(prof.alpha).fit(bX, by, bw)
        new = CalibrationProfile(model=model, screen_geometry=prof.screen_geometry,
                                 session_id=prof.session_id, alpha=prof.alpha, created=prof.created,
                                 cv_error_px=prof.cv_error_px, validation_error_px=val_err,
                                 n_train_samples=len(bX), n_history_sessions=prof.n_history_sessions)
        with self._lock:
            self._base_X, self._base_y, self._base_w = bX, by, bw
            self._profile = new
            self._filter.reset()
            self._reset_drift()
        print(f"[engine] проверка: ошибка на новых точках ≈ {val_err:.0f} px; модель на {len(bX)} сэмплах; "
              f"сессия сохранена ({n_saved} сэмплов)", flush=True)
        msg = f"Точность ≈ {val_err:.0f} px"
        if self.dataset is not None:
            n_s, n_x = self.dataset.stats()
            msg += f"   ·   в памяти: {n_s} сессий, {n_x} сэмплов"
        return True, msg, val_err

    # ---------------- дообучение по кликам ----------------
    def add_correction(self, tx: float, ty: float, max_dist: float = 350.0) -> bool:
        """Пользователь только что выбрал взглядом цель (tx, ty).

        Берём кадры фиксации перед кликом (без последних 150 мс). Если модель и так
        смотрела рядом с целью (иначе это, вероятно, промах), добавляем их с меткой
        (tx, ty), сохраняем в датасет и переобучаем модель в фоне.
        """
        now = time.monotonic()
        with self._lock:
            prof = self._profile
            feats = [f for (t, f) in self._recent if now - 1.0 <= t <= now - 0.15]
        if prof is None or len(feats) < 6:
            return False
        F = np.array(feats, dtype=np.float32)
        pred = np.median(prof.model.predict(F), axis=0) + self._bias
        miss = float(np.hypot(pred[0] - tx, pred[1] - ty))
        if miss > max_dist:
            return False
        self._add_anchor(F, tx, ty, prof)
        with self._lock:
            self._corr_X.extend(F)
            self._corr_y.extend([(float(tx), float(ty))] * len(F))
            if len(self._corr_X) > MAX_CORRECTION_SAMPLES:
                del self._corr_X[:-MAX_CORRECTION_SAMPLES]
                del self._corr_y[:-MAX_CORRECTION_SAMPLES]
            prof.n_corrections += 1
            n = prof.n_corrections
        bx, by = self._bias
        print(f"[engine] дообучение #{n}: промах {miss:.0f} px, +{len(F)} сэмплов; "
              f"сдвиг ({bx:+.0f}, {by:+.0f}) px", flush=True)
        if n % 5 == 0:
            self.save_corrections()
        self._refit_async()
        return True

    # ---------------- поправка сдвига ----------------
    def _reset_drift(self) -> None:
        """Вызывается под self._lock при смене модели."""
        self._anchors.clear()
        self._bias = np.zeros(2)

    def _add_anchor(self, F: np.ndarray, tx: float, ty: float, prof) -> None:
        with self._lock:
            self._anchors.append((time.monotonic(), F, np.array([tx, ty], dtype=np.float64)))
        self._recompute_bias(prof)

    def _recompute_bias(self, prof) -> None:
        with self._lock:
            anchors = list(self._anchors)
        if not anchors or prof is None:
            return
        now = time.monotonic()
        rs, ws = [], []
        for t, F, target in anchors:
            pred = np.median(prof.model.predict(F), axis=0)
            rs.append(target - pred)
            ws.append(0.5 ** ((now - t) / DRIFT_HALF_LIFE))
        rs, ws = np.array(rs), np.array(ws)
        # Устойчивая оценка: отбрасываем якоря, сильно отличающиеся от медианы
        med = np.median(rs, axis=0)
        ok = np.hypot(*(rs - med).T) <= max(80.0, 2.5 * float(np.median(np.hypot(*(rs - med).T))))
        bias = (rs[ok] * ws[ok, None]).sum(axis=0) / (ws[ok].sum() + DRIFT_PRIOR)
        n = float(np.hypot(*bias))
        if n > DRIFT_MAX:
            bias *= DRIFT_MAX / n
        with self._lock:
            if self._profile is prof:
                self._bias = bias

    @property
    def drift(self) -> tuple[float, float]:
        return float(self._bias[0]), float(self._bias[1])

    def add_adjust_point(self, tx: float, ty: float, t0: float, t1: float, max_dist: float = 800.0,
                         kind: str = "adjust"):
        """Точка быстрой подстройки: кадры с t0 по t1 (time.monotonic) — взгляд на (tx, ty).
        Возвращает промах ДО подстройки (px) или None, если кадров мало.

        Абсолютный порог мягкий: если человек сел иначе, промах на ВСЕХ точках может быть
        300–500 px — именно эти точки и нужны. Точки, куда явно не смотрели, отсеиваются
        в finish_adjust по несогласованности с остальными."""
        with self._lock:
            prof = self._profile
            feats = [f for (t, f) in self._recent if t0 <= t <= t1]
        if prof is None or len(feats) < 8:
            return None
        F = np.array(feats, dtype=np.float32)
        keep = filter_outliers(F, np.zeros(len(F), dtype=np.int32))
        F = F[keep]
        pred = np.median(prof.model.predict(F), axis=0) + self._bias
        miss = float(np.hypot(pred[0] - tx, pred[1] - ty))
        if miss > max_dist:
            return None
        with self._lock:
            self._adj_pending.append((F, np.array([tx, ty], dtype=np.float64), pred - self._bias, kind))
        return miss

    def clear_adjust(self) -> None:
        """Новая подстройка заменяет предыдущую (поза могла снова поменяться)."""
        with self._lock:
            self._adj_X.clear()
            self._adj_y.clear()
            self._adj_pending = []
            self._reset_drift()

    def clear_pending(self) -> None:
        """Сбросить точки начатой, но не законченной подстройки/тренировки."""
        with self._lock:
            self._adj_pending = []

    def finish_adjust(self, tolerance: float = 150.0) -> tuple[float, float]:
        """После всех точек подстройки: пересчитать сдвиг и дообучить модель в фоне."""
        with self._lock:
            prof = self._profile
            pending, self._adj_pending = self._adj_pending, []
        if pending:
            # Согласованность: у всех точек сдвиг (цель − прогноз) должен быть похож.
            # Точка, чей сдвиг сильно отличается от медианного, — туда не смотрели.
            r = np.array([t - p for (_, t, p, _) in pending])
            dev = np.hypot(*(r - np.median(r, axis=0)).T)
            ok = dev <= max(tolerance, 2.5 * float(np.median(dev)))
            dropped = int((~ok).sum())
            if dropped:
                print(f"[engine] подстройка: отброшено точек (не похоже, что смотрели): {dropped}", flush=True)
            now = time.monotonic()
            ex_X, ex_y, ex_g = [], [], []
            with self._lock:
                for i, ((F, t, _, kind), good) in enumerate(zip(pending, ok)):
                    if not good:
                        continue
                    self._anchors.append((now, F, t))
                    lab = [(float(t[0]), float(t[1]))] * len(F)
                    if kind == "exercise":
                        self._ex_X.extend(F)
                        self._ex_y.extend(lab)
                        ex_X.append(F)
                        ex_y.extend(lab)
                        ex_g.extend([i] * len(F))
                    else:
                        self._adj_X.extend(F)
                        self._adj_y.extend(lab)
                geom = self._profile.screen_geometry if self._profile else self._session_geometry
            if ex_X and self.dataset is not None:
                X = np.vstack(ex_X)
                self.dataset.save_session(GazeDataset.new_session_id("exer"), X, np.array(ex_y, np.float32),
                                          np.array(ex_g, np.int32), np.full(len(X), "exercise"), geom)
        self._recompute_bias(prof)
        self._refit_async()
        return self.drift

    def save_corrections(self) -> None:
        if self.dataset is None:
            return
        with self._lock:
            X = np.array(self._corr_X + self._adj_X, dtype=np.float32)
            y = np.array(self._corr_y + self._adj_y, dtype=np.float32).reshape(-1, 2)
            kinds = np.array(["correction"] * len(self._corr_X) + ["adjust"] * len(self._adj_X))
            geom = self._profile.screen_geometry if self._profile else self._session_geometry
        if len(X):
            g = np.arange(len(X), dtype=np.int32) // 20
            self.dataset.save_session(self._corr_session_id, X, y, g, kinds, geom)

    def _refit_async(self) -> None:
        with self._lock:
            self._refit_pending = True
            if self._refit_thread is not None and self._refit_thread.is_alive():
                return
            self._refit_thread = threading.Thread(target=self._refit_loop, daemon=True, name="refit")
            self._refit_thread.start()

    def _refit_loop(self) -> None:
        while True:
            with self._lock:
                if not self._refit_pending:
                    return
                self._refit_pending = False
                prof = self._profile
                if prof is None or self._base_X is None:
                    return
                dim = self._base_X.shape[1]
                cX = np.array(self._corr_X, dtype=np.float32).reshape(-1, dim)
                cy = np.array(self._corr_y, dtype=np.float32).reshape(-1, 2)
                aX = np.array(self._adj_X, dtype=np.float32).reshape(-1, dim)
                ay = np.array(self._adj_y, dtype=np.float32).reshape(-1, 2)
                eX = np.array(self._ex_X, dtype=np.float32).reshape(-1, dim)
                ey = np.array(self._ex_y, dtype=np.float32).reshape(-1, 2)
                bX, by, bw = self._base_X, self._base_y, self._base_w
            X = np.vstack([bX, cX, aX, eX])
            y = np.vstack([by, cy, ay, ey])
            w = np.concatenate([bw, np.full(len(cX), W_LIVE_CORRECTION), np.full(len(aX), W_ADJUST),
                                np.full(len(eX), W_EXERCISE)])
            try:
                model = GazeModel(prof.alpha).fit(X, y, w)
            except Exception as e:
                print(f"[engine] ошибка переобучения: {e}", flush=True)
                return
            with self._lock:
                if self._profile is prof:
                    prof.model = model
                    prof.n_train_samples = len(X)
            # Модель уже выучила часть сдвига — поправка = то, что она ещё не догоняет
            self._recompute_bias(prof)
            if len(aX) or len(eX):
                bx, by = self._bias
                print(f"[engine] модель подстроена: {len(aX)} кадров подстройки, {len(eX)} кадров тренировки, "
                      f"{len(cX)} кадров кликов; "
                      f"остаточный сдвиг ({bx:+.0f}, {by:+.0f}) px", flush=True)

    # ---------------- профили ----------------
    def save_profile(self, path: str | Path) -> None:
        with self._lock:
            prof = self._profile
        if prof is None:
            raise RuntimeError("Нет обученной модели")
        prof.save(path)

    def load_profile(self, path: str | Path) -> CalibrationProfile:
        prof = CalibrationProfile.load(path)
        with self._lock:
            self._profile = prof
            self._session_geometry = tuple(prof.screen_geometry)
            self._filter.reset()
            self._reset_drift()
        # База для дообучения — из датасета, в фоне (чтобы не тормозить запуск)
        threading.Thread(target=self._rebuild_base, args=(prof,), daemon=True, name="load-base").start()
        return prof

    def _rebuild_base(self, prof: CalibrationProfile) -> None:
        if self.dataset is None or prof.model.feature_dim is None:
            return
        sessions = self.dataset.load_sessions(prof.model.feature_dim, tuple(prof.screen_geometry))
        sessions.sort(key=lambda s: s.created, reverse=True)
        Xs, ys, ws, total = [], [], [], 0
        for s in sessions:
            w = W_CURRENT if s.path.stem == prof.session_id else (
                W_CORRECTION if s.path.stem.endswith(("corr", "exer")) else W_HISTORY)
            Xs.append(s.X)
            ys.append(s.y)
            ws.append(np.full(len(s.X), w))
            total += len(s.X)
            if total >= HISTORY_CAP:
                break
        if not Xs:
            return
        with self._lock:
            if self._profile is prof:
                self._base_X = np.vstack(Xs)
                self._base_y = np.vstack(ys)
                self._base_w = np.concatenate(ws)
        print(f"[engine] база для дообучения: {total} сэмплов из {len(Xs)} сессий", flush=True)
        with self._lock:
            pending = bool(self._adj_X or self._corr_X or self._ex_X)
        if pending:  # подстройка успела пройти раньше, чем загрузилась база
            self._refit_async()

    # ---------------- поток --------------------------------------------------
    def _open_camera(self, index: int) -> cv2.VideoCapture | None:
        if sys.platform == "darwin":
            backends = [cv2.CAP_AVFOUNDATION, cv2.CAP_ANY]
        elif sys.platform.startswith("win"):
            backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]
        for be in backends:
            cap = cv2.VideoCapture(index, be)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.settings.camera_width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.settings.camera_height)
                cap.set(cv2.CAP_PROP_FPS, 30)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, _ = cap.read()
                if ok:
                    return cap
            cap.release()
        return None

    def _set_face(self, present: bool) -> None:
        if present != self._face_present:
            self._face_present = present
            if not present:
                with self._lock:
                    self._filter.reset()
            self.face_changed.emit(present)

    def run(self) -> None:  # noqa: C901
        self._running = True
        try:
            extractor = FeatureExtractor(self._model_path)
        except Exception as e:
            self.error.emit(f"Ошибка инициализации трекера: {e}")
            return
        neural = None
        try:
            from core.neural_gaze import NEURAL_MODEL_PATH, NeuralGaze

            if NEURAL_MODEL_PATH.exists():
                neural = NeuralGaze()
        except Exception as e:  # onnxruntime не установлен — просто не пишем эти данные
            print(f"[engine] нейросеть недоступна: {e}", flush=True)

        cap = None
        last_valid = time.monotonic()
        frames, fps_t0 = 0, time.monotonic()
        try:
            while self._running:
                if cap is None or self._camera_changed:
                    if cap is not None:
                        cap.release()
                    with self._lock:
                        self._camera_changed = False
                        idx = self._camera_index
                    cap = self._open_camera(idx)
                    if cap is None:
                        self.error.emit(
                            f"Не удалось открыть камеру #{idx}. Проверьте подключение и разрешение "
                            "на доступ к камере (macOS: Системные настройки → Конфиденциальность → Камера)."
                        )
                        self._set_face(False)
                        # повторяем попытку раз в 2 сек
                        for _ in range(20):
                            if not self._running or self._camera_changed:
                                break
                            self.msleep(100)
                        continue
                    self.camera_ready.emit()

                ok, frame = cap.read()
                if not ok or frame is None:
                    self.msleep(10)
                    if time.monotonic() - last_valid > 2.0:
                        cap.release()
                        cap = None  # переоткрыть
                    continue

                now = time.monotonic()
                try:
                    feats, blink = extractor.extract(frame)
                except Exception as e:
                    self.error.emit(f"Ошибка трекинга: {e}")
                    feats, blink = None, False

                frames += 1
                if now - fps_t0 >= 1.0:
                    self.fps_changed.emit(frames / (now - fps_t0))
                    frames, fps_t0 = 0, now

                if feats is None or blink:
                    # Нет лица или моргание: короткие пропуски игнорируем,
                    # длинные — сигнал «лицо потеряно» (курсор встаёт на паузу).
                    if now - last_valid > self.settings.face_lost_grace:
                        self._set_face(False)
                    continue

                last_valid = now
                self._set_face(True)

                # Нейросеть считаем только во время калибровки — для записи в датасет
                nn = None
                if neural is not None and self._collect_target is not None:
                    try:
                        nn = neural.estimate(frame, extractor.last_pts)
                    except Exception as e:
                        print(f"[engine] нейросеть отключена: {e}", flush=True)
                        neural = None

                with self._lock:
                    target = self._collect_target
                    if target is not None:
                        self._cur_N.append(nn if nn is not None else np.full(2, np.nan, np.float32))
                        self._cur_X.append(feats)
                        self._cur_y.append(target)
                        self._cur_g.append(self._point_id)
                        self._cur_k.append(self._collect_kind)
                    else:
                        self._recent.append((now, feats))
                    profile = self._profile

                if profile is None or target is not None:
                    continue

                try:
                    xy = profile.model.predict(feats[None, :])[0] + self._bias
                except Exception as e:
                    self.error.emit(f"Профиль калибровки несовместим: {e}. Выполните калибровку заново.")
                    with self._lock:
                        self._profile = None
                    continue

                if self._bounds is not None:
                    x0, y0, x1, y1 = self._bounds
                    xy = np.array([min(max(xy[0], x0), x1 - 1), min(max(xy[1], y0), y1 - 1)])

                with self._lock:
                    sx, sy = self._filter(xy, now)
                self.gaze.emit(float(sx), float(sy))
        finally:
            if cap is not None:
                cap.release()
            extractor.close()
