"""Нейросетевая оценка направления взгляда по изображению лица (экспериментально).

Модель MobileOne-S0 в стиле L2CS-Net, обученная на Gaze360
(github.com/yakhyo/gaze-estimation, код — MIT; веса обучены на Gaze360 —
исследовательский датасет, для личного использования).
Возвращает (yaw, pitch) в радианах; ~5 мс на кадр на CPU.

Пока ответы только записываются в сессии калибровки рядом с обычными признаками.
В рабочую модель они попадут, если на накопленных данных это даст выигрыш
(см. scripts/eval_neural.py).
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from config import PROJECT_DIR

NEURAL_MODEL_PATH = PROJECT_DIR / "assets" / "gaze_mobileone_s0.onnx"
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_BINS = np.arange(90, dtype=np.float32)
CROP_MARGIN = 0.12   # расширение bbox по ориентирам ≈ bbox детектора лица RetinaFace


class NeuralGaze:
    def __init__(self, model_path: Path = NEURAL_MODEL_PATH):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # не отнимать CPU у MediaPipe и GUI
        self._sess = ort.InferenceSession(str(model_path), sess_options=opts,
                                          providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        self._size = tuple(self._sess.get_inputs()[0].shape[2:][::-1])  # (W, H)
        names = [o.name for o in self._sess.get_outputs()]
        self._order = (names.index("yaw"), names.index("pitch")) if {"yaw", "pitch"} <= set(names) else (0, 1)

    def estimate(self, frame_bgr: np.ndarray, pts_norm: np.ndarray) -> np.ndarray | None:
        """pts_norm — ориентиры MediaPipe (N×3, x/y в долях кадра). → [yaw, pitch] (рад)."""
        h, w = frame_bgr.shape[:2]
        xs, ys = pts_norm[:, 0] * w, pts_norm[:, 1] * h
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        mx, my = (x1 - x0) * CROP_MARGIN, (y1 - y0) * CROP_MARGIN
        x0, x1 = int(max(x0 - mx, 0)), int(min(x1 + mx, w))
        y0, y1 = int(max(y0 - my, 0)), int(min(y1 + my, h))
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None
        crop = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2RGB)
        crop = cv2.resize(crop, self._size).astype(np.float32) / 255.0
        crop = ((crop - _MEAN) / _STD).transpose(2, 0, 1)[None]
        out = self._sess.run(None, {self._input: np.ascontiguousarray(crop)})

        def decode(logits):
            z = logits[0] - logits[0].max()
            p = np.exp(z) / np.exp(z).sum()
            return float(np.radians((p * _BINS).sum() * 4.0 - 180.0))

        return np.array([decode(out[self._order[0]]), decode(out[self._order[1]])], dtype=np.float32)
