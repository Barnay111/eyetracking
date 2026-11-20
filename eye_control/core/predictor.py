"""Подсказка слов для экранной клавиатуры.

Словари — 30 000 самых частых слов RU/EN с частотой (Zipf) из проекта wordfreq
(данные: CC BY-SA 4.0). Набранный текст никуда не сохраняется.
"""
from __future__ import annotations

import bisect
import heapq
from pathlib import Path

from config import PROJECT_DIR

WORDS_DIR = PROJECT_DIR / "assets"


class WordPredictor:
    def __init__(self, words_dir: Path = WORDS_DIR):
        self._dir = words_dir
        self._cache: dict[str, tuple[list[str], dict[str, float]]] = {}

    def _load(self, lang: str):
        if lang not in self._cache:
            freq: dict[str, float] = {}
            try:
                with open(self._dir / f"words_{lang}.txt", encoding="utf-8") as f:
                    for line in f:
                        w, _, z = line.rstrip("\n").partition("\t")
                        if w and w not in freq:
                            freq[w] = float(z or 0)
            except FileNotFoundError:
                print(f"[predictor] нет словаря words_{lang}.txt — подсказки выключены")
            self._cache[lang] = (sorted(freq), freq)
        return self._cache[lang]

    def suggest(self, prefix: str, lang: str, n: int = 4) -> list[str]:
        """До n самых частых слов, начинающихся с prefix (без учёта регистра).
        Регистр первой буквы берётся из prefix (Привет → Привет..., не привет)."""
        if not prefix:
            return []
        words, freq = self._load(lang)
        p = prefix.lower().replace("ё", "е") if lang == "ru" else prefix.lower()
        cands = []
        for key in {p, prefix.lower()}:
            lo = bisect.bisect_left(words, key)
            hi = bisect.bisect_left(words, key + "￿")
            cands.extend(words[lo:hi])
        if lang == "ru" and "е" in p:  # «еще» должно находить «ещё»
            alt = p.replace("е", "ё")
            lo = bisect.bisect_left(words, alt)
            cands.extend(words[lo:bisect.bisect_left(words, alt + "￿")])
        # само набранное слово целиком не предлагаем — его и так можно закончить пробелом
        cands = {w for w in cands if len(w) > len(prefix)}
        best, seen = [], set()
        for w in heapq.nlargest(n * 2, cands, key=lambda w: freq[w]):
            k = w.replace("ё", "е")  # «еще» и «ещё» — одна подсказка
            if k not in seen:
                seen.add(k)
                best.append(w)
        best = best[:n]
        if prefix[:1].isupper():
            best = [w[:1].upper() + w[1:] for w in best]
        return best


def current_word(text: str) -> str:
    """Незаконченное слово в конце текста."""
    i = len(text)
    while i > 0 and (text[i - 1].isalpha() or text[i - 1] in "-'"):
        i -= 1
    return text[i:]
