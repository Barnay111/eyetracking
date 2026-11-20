"""Виртуальная экранная клавиатура (ЙЦУКЕН / QWERTY) + предпросмотр + TTS.

Окно не забирает фокус: нажатия по взгляду выполняются программно (см.
main.py → _dispatch_gaze_click), поэтому активным остаётся целевое окно и
символы могут печататься в него напрямую (режим «Прямой ввод»).
Если прямой ввод выключен — текст копится в поле, а кнопка «Вставить»
кладёт его в буфер обмена и вставляет Cmd/Ctrl+V.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import KEYBOARD_LAYOUTS
from core.predictor import WordPredictor, current_word
from ui.common import DraggablePanel, apply_non_activating, button_at, setup_floating_window


class VirtualKeyboard(DraggablePanel):
    # Сигналы для main: вывод в систему
    type_text = Signal(str)
    key_backspace = Signal()
    key_enter = Signal()
    paste_text = Signal(str)
    speak = Signal(str)
    closed = Signal()

    def __init__(self, settings):
        super().__init__(None)
        self.settings = settings
        self.text = ""
        self.shift = False
        setup_floating_window(self)
        self.setWindowTitle("Клавиатура")

        panel = QWidget(self)
        panel.setObjectName("Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        self._root = QVBoxLayout(panel)
        self._root.setContentsMargins(12, 12, 12, 12)
        self._root.setSpacing(8)

        self.preview = QLabel("")
        self.preview.setMinimumHeight(64)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "QLabel { background: #111318; color: white; font-size: 26px; border-radius: 10px; padding: 8px 14px; }"
        )
        self._root.addWidget(self.preview)

        # Подсказки слов: 4 крупные кнопки над клавишами
        self.predictor = WordPredictor()
        sugg = QHBoxLayout()
        sugg.setSpacing(6)
        self._sugg_buttons: list[QPushButton] = []
        for _ in range(4):
            b = QPushButton("")
            b.setMinimumHeight(settings.key_size)
            b.setFocusPolicy(Qt.NoFocus)
            b.setObjectName("Suggest")
            b.setStyleSheet(
                "QPushButton#Suggest { font-size: 22px; background: #24496B; border-color: #2D9CDB; }"
                "QPushButton#Suggest:disabled { background: #2A2D35; border-color: #3A3F4B; }"
            )
            b.clicked.connect(lambda _=False, btn=b: self._accept_suggestion(btn.text()))
            sugg.addWidget(b, 1)
            self._sugg_buttons.append(b)
        self._root.addLayout(sugg)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(6)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._root.addWidget(self._grid_host)

        # Нижний ряд функциональных клавиш
        k = settings.key_size
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.btn_shift = self._mk("⇧", self._toggle_shift, k)
        self.btn_lang = self._mk("", self._toggle_layout, k)
        self.btn_space = self._mk("Пробел", lambda: self._input(" "), k)
        self.btn_space.setMinimumWidth(k * 4)
        self.btn_bs = self._mk("⌫", lambda: self._backspace(), int(k * 1.3))
        self.btn_enter = self._mk("⏎ Enter", self._enter, int(k * 1.6))
        for b in (self.btn_shift, self.btn_lang, self.btn_space, self.btn_bs, self.btn_enter):
            bottom.addWidget(b)
        self._root.addLayout(bottom)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.btn_speak = self._mk("🔊 Озвучить", lambda: self.speak.emit(self.text), k, "Accent")
        self.btn_clear = self._mk("🗑 Очистить", self._clear, k)
        self.btn_direct = self._mk("", self._toggle_direct, k)
        self.btn_paste = self._mk("📋 Вставить", self._paste, k)
        self.btn_close = self._mk("✕ Скрыть", self._close, k, "Danger")
        for b in (self.btn_speak, self.btn_clear, self.btn_direct, self.btn_paste, self.btn_close):
            b.setMinimumWidth(int(k * 1.9))
            actions.addWidget(b)
        self._root.addLayout(actions)

        self._build_keys()
        self._update_labels()

    # ------------------------------------------------------------------
    def _mk(self, text, slot, size, obj_name=None) -> QPushButton:
        b = QPushButton(text)
        b.setMinimumSize(size, size)
        b.setFocusPolicy(Qt.NoFocus)
        if obj_name:
            b.setObjectName(obj_name)
        b.clicked.connect(slot)
        return b

    def _build_keys(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        k = self.settings.key_size
        self._letter_buttons: list[QPushButton] = []
        rows = KEYBOARD_LAYOUTS[self.settings.keyboard_layout]
        max_len = max(len(r) for r in rows)
        for r, row in enumerate(rows):
            offset = (max_len - len(row))  # полуклавишный сдвиг коротких рядов
            for c, ch in enumerate(row):
                b = QPushButton(ch)
                b.setFixedSize(k, k)
                b.setFocusPolicy(Qt.NoFocus)
                b.setProperty("ch", ch)
                b.clicked.connect(lambda _=False, btn=b: self._input(self._char(btn.property("ch"))))
                self._grid.addWidget(b, r, c * 2 + offset, 1, 2)
                self._letter_buttons.append(b)
        self._apply_shift()
        self.adjustSize()

    def _char(self, ch: str) -> str:
        return ch.upper() if self.shift else ch

    def _apply_shift(self):
        for b in self._letter_buttons:
            ch = b.property("ch")
            b.setText(ch.upper() if self.shift else ch)
        self.btn_shift.setCheckable(True)
        self.btn_shift.setChecked(self.shift)

    def _update_labels(self):
        self.btn_lang.setText("RU" if self.settings.keyboard_layout == "ru" else "EN")
        self.btn_direct.setText("Прямой ввод:\nВКЛ" if self.settings.direct_typing else "Прямой ввод:\nВЫКЛ")
        self.btn_paste.setEnabled(not self.settings.direct_typing)
        shown = self.text[-120:] if self.text else ""
        self.preview.setText(shown + "▏")
        self._update_suggestions()

    def _update_suggestions(self):
        words = self.predictor.suggest(current_word(self.text), self.settings.keyboard_layout, len(self._sugg_buttons))
        for i, b in enumerate(self._sugg_buttons):
            w = words[i] if i < len(words) else ""
            b.setText(w)
            b.setEnabled(bool(w))

    def _accept_suggestion(self, word: str):
        if not word:
            return
        prefix = current_word(self.text)
        if word.startswith(prefix):
            add = word[len(prefix):] + " "
        else:  # отличается регистром или е/ё — переписываем слово целиком
            for _ in prefix:
                self._backspace(update=False)
            add = word + " "
        self.text += add
        if self.settings.direct_typing:
            self.type_text.emit(add)
        if self.shift:
            self.shift = False
            self._apply_shift()
        self._update_labels()

    def is_private(self, btn) -> bool:
        """Кнопки, текст которых нельзя писать в журнал (буквы и слова)."""
        return btn in self._letter_buttons or btn in self._sugg_buttons

    def is_typing_key(self, btn) -> bool:
        """Клавиши набора — для них короткий dwell."""
        return self.is_private(btn) or btn in (self.btn_space, self.btn_bs, self.btn_shift)

    # ------------------------------------------------------------------
    def _input(self, s: str):
        self.text += s
        if self.settings.direct_typing:
            self.type_text.emit(s)
        if self.shift and s.strip():
            self.shift = False
            self._apply_shift()
        self._update_labels()

    def _backspace(self, update: bool = True):
        self.text = self.text[:-1]
        if self.settings.direct_typing:
            self.key_backspace.emit()
        if update:
            self._update_labels()

    def _enter(self):
        if self.settings.direct_typing:
            self.key_enter.emit()
        self.text += "\n"
        self._update_labels()

    def _clear(self):
        self.text = ""
        self._update_labels()

    def _toggle_shift(self):
        self.shift = not self.shift
        self._apply_shift()

    def _toggle_layout(self):
        self.settings.keyboard_layout = "en" if self.settings.keyboard_layout == "ru" else "ru"
        self._build_keys()
        self._update_labels()

    def _toggle_direct(self):
        self.settings.direct_typing = not self.settings.direct_typing
        self._update_labels()

    def _paste(self):
        if self.text:
            self.paste_text.emit(self.text)

    def _close(self):
        self.hide()
        self.closed.emit()

    # ------------------------------------------------------------------
    def showEvent(self, e):
        super().showEvent(e)
        apply_non_activating(self)

    def show_at_bottom(self):
        self.adjustSize()
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.left() + (g.width() - self.width()) // 2, g.bottom() - self.height() - 12)
        self.show()
        self.raise_()

    def button_at(self, global_pos: QPoint):
        return button_at(self, global_pos)
