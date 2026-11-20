"""Накопительный датасет взгляда: все сессии калибровки и дообучения на диске.

Каждая сессия — отдельный файл ``~/.eye_control/data/sessions/<id>.npz``:
    X       float32 [N, D]  — признаки кадра (глаза + поза головы)
    y       float32 [N, 2]  — куда человек смотрел (логические координаты экрана)
    groups  int32   [N]     — номер точки внутри сессии (для честной кросс-валидации)
    kind    str     [N]     — "grid" | "val" | "pursuit" | "correction"
    meta    json            — время, геометрия экрана, версия признаков, камера

Журнал использования (клики, режимы) — ``~/.eye_control/data/usage.jsonl``.
Он пригодится позже, когда будем обучать сценарии: печать, открытие/закрытие приложений.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FEATURE_VERSION = 1


@dataclass
class Session:
    path: Path
    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    kind: np.ndarray
    meta: dict

    @property
    def created(self) -> float:
        return float(self.meta.get("created", 0.0))


class GazeDataset:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.usage_path = self.root / "usage.jsonl"

    # ------------------------------------------------------------------
    @staticmethod
    def new_session_id(prefix: str = "calib") -> str:
        return time.strftime(f"%Y%m%d-%H%M%S-{prefix}")

    def save_session(
        self,
        session_id: str,
        X: np.ndarray,
        y: np.ndarray,
        groups: np.ndarray,
        kind: np.ndarray,
        screen_geometry: tuple[int, int, int, int],
        extra_meta: dict | None = None,
        neural: np.ndarray | None = None,
    ) -> Path | None:
        if len(X) == 0:
            return None
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "created": time.time(),
            "screen_geometry": list(screen_geometry),
            "feature_version": FEATURE_VERSION,
            "feature_dim": int(X.shape[1]),
            "n_samples": int(len(X)),
        }
        meta.update(extra_meta or {})
        path = self.sessions_dir / f"{session_id}.npz"
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(
            tmp,
            X=np.asarray(X, dtype=np.float32),
            y=np.asarray(y, dtype=np.float32),
            groups=np.asarray(groups, dtype=np.int32),
            kind=np.asarray(kind, dtype="U12"),
            meta=np.array(json.dumps(meta)),
            **({"N": np.asarray(neural, dtype=np.float32)} if neural is not None else {}),
        )
        tmp.replace(path)
        return path

    def load_sessions(
        self,
        feature_dim: int | None = None,
        screen_geometry: tuple[int, int, int, int] | None = None,
        exclude: set[str] | None = None,
    ) -> list[Session]:
        """Совместимые сессии (та же версия признаков, размерность и геометрия экрана)."""
        out: list[Session] = []
        if not self.sessions_dir.exists():
            return out
        for p in sorted(self.sessions_dir.glob("*.npz")):
            if p.name.endswith(".tmp.npz") or (exclude and p.stem in exclude):
                continue
            try:
                with np.load(p, allow_pickle=False) as z:
                    meta = json.loads(str(z["meta"]))
                    if meta.get("feature_version") != FEATURE_VERSION:
                        continue
                    if feature_dim is not None and meta.get("feature_dim") != feature_dim:
                        continue
                    if screen_geometry is not None and list(meta.get("screen_geometry", [])) != list(screen_geometry):
                        continue
                    out.append(Session(p, z["X"], z["y"], z["groups"], z["kind"], meta))
            except Exception as e:
                print(f"[dataset] пропускаю повреждённый файл {p.name}: {e}", flush=True)
        return out

    def stats(self) -> tuple[int, int]:
        """(число сессий, число сэмплов) — по метаданным, без загрузки массивов."""
        n_s = n_x = 0
        if not self.sessions_dir.exists():
            return 0, 0
        for p in self.sessions_dir.glob("*.npz"):
            if p.name.endswith(".tmp.npz"):
                continue
            try:
                with np.load(p, allow_pickle=False) as z:
                    n_x += int(json.loads(str(z["meta"])).get("n_samples", 0))
                n_s += 1
            except Exception:
                pass
        return n_s, n_x

    # ------------------------------------------------------------------
    def log_event(self, event: str, **fields) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            rec = {"t": round(time.time(), 3), "event": event, **fields}
            with self.usage_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[dataset] не удалось записать событие: {e}", flush=True)

    def count_events(self, event: str, field: str) -> dict[str, int]:
        """Сколько раз встречалось событие event с каждым значением поля field."""
        out: dict[str, int] = {}
        try:
            with self.usage_path.open(encoding="utf-8") as fh:
                for line in fh:
                    if f'"{event}"' not in line:
                        continue
                    rec = json.loads(line)
                    if rec.get("event") == event and field in rec:
                        out[str(rec[field])] = out.get(str(rec[field]), 0) + 1
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[dataset] не удалось прочитать журнал: {e}", flush=True)
        return out
