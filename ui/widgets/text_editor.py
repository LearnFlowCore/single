"""Rich text editor with a compact formatting toolbar."""

from __future__ import annotations

from PySide6.QtGui import QKeySequence, QTextCharFormat
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QPushButton, QTextEdit, QVBoxLayout, QWidget


class TextEditor(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = QTextEdit()
        self.editor.setAcceptRichText(True)
        self.editor.setMinimumHeight(260)
        self.editor.setPlaceholderText("Введите текст публикации...")

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 4)
        bold = QPushButton("B")
        bold.setObjectName("formatButton")
        bold.setCheckable(True)
        bold.setShortcut(QKeySequence.Bold)
        bold.toggled.connect(self._set_bold)
        italic = QPushButton("I")
        italic.setObjectName("formatButton")
        italic.setCheckable(True)
        italic.setShortcut(QKeySequence.Italic)
        italic.toggled.connect(self._set_italic)
        link = QPushButton("Ссылка")
        link.setObjectName("formatButton")
        link.clicked.connect(self._insert_link)
        toolbar.addWidget(bold)
        toolbar.addWidget(italic)
        toolbar.addWidget(link)
        toolbar.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(toolbar)
        layout.addWidget(self.editor)

    def plain_text(self) -> str:
        return self.editor.toPlainText().strip()

    def html_text(self) -> str:
        return self.editor.toHtml()

    def clear(self) -> None:
        self.editor.clear()

    def _set_bold(self, enabled: bool) -> None:
        self.editor.setFontWeight(700 if enabled else 400)
        self.editor.setFocus()

    def _set_italic(self, enabled: bool) -> None:
        self.editor.setFontItalic(enabled)
        self.editor.setFocus()

    def _insert_link(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            return
        url, accepted = QInputDialog.getText(self, "Добавить ссылку", "URL:")
        if accepted and url.strip():
            char_format = QTextCharFormat()
            char_format.setAnchor(True)
            char_format.setAnchorHref(url.strip())
            char_format.setFontUnderline(True)
            cursor.mergeCharFormat(char_format)
