"""Drag-and-drop media selector with thumbnails."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from utils.media import SUPPORTED_EXTENSIONS, validate_media


class MediaUploader(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paths: list[str] = []
        self.setAcceptDrops(True)
        self.setObjectName("mediaUploader")
        self.setMinimumHeight(170)

        self.hint = QLabel("Перетащите JPG, PNG, GIF или MP4 сюда\nили выберите файлы")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        choose = QPushButton("Выбрать файлы")
        choose.clicked.connect(self._choose_files)
        self.grid = QGridLayout()

        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(choose, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(self.grid)

    @property
    def paths(self) -> list[str]:
        return self._paths.copy()

    def clear(self) -> None:
        self._paths.clear()
        self._refresh()
        self.changed.emit()

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Выберите медиа", "", "Медиа (*.jpg *.jpeg *.png *.gif *.mp4)"
        )
        self._add_paths(paths)

    def _add_paths(self, paths: list[str]) -> None:
        candidates = self._paths + [str(Path(path).resolve()) for path in paths]
        candidates = list(dict.fromkeys(candidates))
        try:
            validate_media(candidates, max_files=10)
        except ValueError as exc:
            QMessageBox.warning(self, "Не удалось добавить файл", str(exc))
            return
        self._paths = candidates
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.hint.setVisible(not self._paths)
        for index, path_text in enumerate(self._paths):
            path = Path(path_text)
            card = QWidget()
            card.setObjectName("mediaCard")
            card_layout = QVBoxLayout(card)
            preview = QLabel()
            preview.setFixedSize(100, 72)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if path.suffix.lower() in SUPPORTED_EXTENSIONS - {".mp4"}:
                pixmap = QPixmap(str(path))
                preview.setPixmap(
                    pixmap.scaled(100, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
            else:
                preview.setText("VIDEO")
            name = QLabel(path.name)
            name.setToolTip(str(path))
            name.setMaximumWidth(110)
            remove = QPushButton("Удалить")
            remove.setProperty("mediaIndex", index)
            remove.clicked.connect(self._remove_clicked)
            card_layout.addWidget(preview)
            card_layout.addWidget(name)
            card_layout.addWidget(remove)
            self.grid.addWidget(card, index // 5, index % 5)

    def _remove_clicked(self) -> None:
        button = self.sender()
        index = int(button.property("mediaIndex"))
        self._paths.pop(index)
        self._refresh()
        self.changed.emit()
