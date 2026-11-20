"""Стабилизатор курсора: «мёртвая зона» вокруг точки фиксации.

Даже после One Euro фильтра точка взгляда дрожит на 10–40 px: это шум веб-камеры,
а не движение глаз. Пока новые точки остаются в радиусе R от якоря, курсор стоит
на месте (якорь лишь медленно подтягивается к среднему, чтобы не копилась ошибка).
Резкий перевод взгляда (дальше 3R) переносит курсор сразу; умеренный выход за R
подтверждается несколькими кадрами подряд — одиночный выброс не дёргает курсор.
"""
from __future__ import annotations

import math


class GazeStabilizer:
    def __init__(self, radius: float = 35.0, confirm_frames: int = 3, pull: float = 0.04):
        self.radius = radius
        self.confirm_frames = confirm_frames
        self.pull = pull            # доля, на которую якорь тянется к взгляду за кадр
        self._anchor: tuple[float, float] | None = None
        self._out = 0
        self._out_sum = [0.0, 0.0]

    def reset(self):
        self._anchor = None
        self._out = 0

    def update(self, x: float, y: float) -> tuple[float, float]:
        r = self.radius
        if r <= 0:
            return x, y
        if self._anchor is None:
            self._anchor = (x, y)
            return x, y
        ax, ay = self._anchor
        d = math.hypot(x - ax, y - ay)
        if d <= r:
            self._out = 0
            self._anchor = (ax + (x - ax) * self.pull, ay + (y - ay) * self.pull)
        elif d > 3 * r:  # саккада — сразу
            self._anchor = (x, y)
            self._out = 0
        else:
            if self._out == 0:
                self._out_sum = [0.0, 0.0]
            self._out += 1
            self._out_sum[0] += x
            self._out_sum[1] += y
            if self._out >= self.confirm_frames:  # взгляд действительно сместился
                self._anchor = (self._out_sum[0] / self._out, self._out_sum[1] / self._out)
                self._out = 0
        return self._anchor
