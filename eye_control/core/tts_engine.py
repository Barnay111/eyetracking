"""Офлайн-озвучка текста.

* macOS — системная утилита `say` (без задержки на инициализацию, голос
  подбирается по языку текста: для кириллицы — русский голос, например Milena).
* Windows / прочие — pyttsx3 (SAPI5 на Windows) в отдельном рабочем потоке.
"""
from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading

IS_MAC = sys.platform == "darwin"
_CYR = re.compile(r"[а-яА-ЯёЁ]")


def _is_russian(text: str) -> bool:
    return bool(_CYR.search(text))


class TTSEngine:
    def __init__(self, rate: int = 180) -> None:
        self.rate = rate
        self._proc: subprocess.Popen | None = None
        self._voices_mac: dict[str, str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        if not IS_MAC:
            self._worker = threading.Thread(target=self._pyttsx3_loop, daemon=True, name="tts")
            self._worker.start()

    # ------------------------------------------------------------------
    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if IS_MAC:
            self._speak_mac(text)
        else:
            # очищаем очередь — озвучиваем только последнюю фразу
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put(text)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._worker is not None:
            self._queue.put(None)

    # ---------------- macOS ----------------
    def _mac_voice_for(self, text: str) -> str | None:
        if self._voices_mac is None:
            self._voices_mac = {}
            try:
                out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5).stdout
                for line in out.splitlines():
                    m = re.match(r"^(.+?)\s{2,}([a-z]{2}[_-][A-Z]{2})", line)
                    if m:
                        name, lang = m.group(1).strip(), m.group(2)[:2]
                        self._voices_mac.setdefault(lang, name)
                        if name.startswith("Milena"):
                            self._voices_mac["ru"] = name
            except Exception:
                pass
        lang = "ru" if _is_russian(text) else "en"
        return self._voices_mac.get(lang)

    def _speak_mac(self, text: str) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        cmd = ["say", "-r", str(self.rate)]
        voice = self._mac_voice_for(text)
        if voice:
            cmd += ["-v", voice]
        cmd.append(text)
        try:
            self._proc = subprocess.Popen(cmd)
        except Exception as e:
            print(f"[tts] say failed: {e}", file=sys.stderr)

    # ---------------- pyttsx3 ----------------
    def _pyttsx3_loop(self) -> None:
        try:
            import pyttsx3
        except Exception as e:
            print(f"[tts] pyttsx3 недоступен: {e}", file=sys.stderr)
            return
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                # Новый engine на каждую фразу — обходит известное зависание
                # runAndWait() при повторном использовании на SAPI5.
                engine = pyttsx3.init()
                engine.setProperty("rate", self.rate)
                want_ru = _is_russian(text)
                for v in engine.getProperty("voices"):
                    desc = f"{v.name} {v.id} {getattr(v, 'languages', '')}".lower()
                    is_ru = "russian" in desc or "ru-ru" in desc or "ru_ru" in desc or "irina" in desc
                    if is_ru == want_ru:
                        engine.setProperty("voice", v.id)
                        break
                engine.say(text)
                engine.runAndWait()
                engine.stop()
                del engine
            except Exception as e:
                print(f"[tts] ошибка озвучки: {e}", file=sys.stderr)
