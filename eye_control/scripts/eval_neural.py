"""Помогает ли нейросеть? Сравнение на сессиях калибровки, где записаны её ответы (N).

    .venv/bin/python scripts/eval_neural.py

1) Внутри сессии: GroupKFold по точкам сетки, Ridge на обычных признаках против
   обычных + нейросеть. 2) Между сессиями (если таких сессий ≥ 2): обучение на всех
   остальных сессиях с N, проверка на отложенной. Ошибка — медиана по точкам (px).
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402
from sklearn.model_selection import GroupKFold  # noqa: E402

from core.gaze_engine import GazeModel, filter_outliers  # noqa: E402

ALPHA = 300.0
SCALE_N = 1.0


def load():
    out = {}
    for f in sorted(glob.glob(os.path.expanduser("~/.eye_control/data/sessions/*calib.npz"))):
        with np.load(f, allow_pickle=False) as z:
            if "N" not in z.files:
                continue
            X, y, g, k, N = z["X"], z["y"], z["groups"], z["kind"], z["N"]
        m = (k == "grid") & np.isfinite(N).all(1)
        if len(np.unique(g[m])) >= 9:
            out[os.path.basename(f)] = (X[m], y[m], g[m], N[m])
    return out


def point_err(model, X, y, g):
    p = model.predict(X)
    return [float(np.hypot(*(np.median(p[g == gg], 0) - y[g == gg][0]))) for gg in np.unique(g)]


def feats(X, N, mode):
    if mode == "base":
        return X
    if mode == "+nn":
        return np.hstack([X, N * SCALE_N])
    if mode == "nn+head":  # только нейросеть + поза/положение головы
        return np.hstack([N, X[:, -6:]])


def main():
    S = load()
    if not S:
        print("Нет сессий с записью нейросети — проведите калибровку.")
        return
    modes = ("base", "+nn", "nn+head")
    print("1) Внутри сессии (медиана ошибки по точкам, px):")
    for name, (X, y, g, N) in S.items():
        keep = filter_outliers(X, g)
        X, y, g, N = X[keep], y[keep], g[keep], N[keep]
        row = []
        for mode in modes:
            F = feats(X, N, mode)
            errs = []
            for tr, te in GroupKFold(5).split(F, y, g):
                errs += point_err(GazeModel(ALPHA).fit(F[tr], y[tr]), F[te], y[te], g[te])
            row.append(f"{mode} {np.median(errs):5.0f}")
        print(f"   {name}: " + " | ".join(row))
    if len(S) >= 2:
        print("2) Между сессиями (обучение на остальных, проверка на отложенной):")
        for test in S:
            row = []
            for mode in modes:
                Xtr = np.vstack([feats(S[k][0], S[k][3], mode) for k in S if k != test])
                ytr = np.vstack([S[k][1] for k in S if k != test])
                X, y, g, N = S[test]
                errs = point_err(GazeModel(ALPHA).fit(Xtr, ytr), feats(X, N, mode), y, g)
                row.append(f"{mode} {np.median(errs):5.0f}")
            print(f"   {test}: " + " | ".join(row))
    else:
        print("2) Между сессиями — нужно ещё минимум одна калибровка с записью нейросети.")


if __name__ == "__main__":
    main()
