"""Primary publish action."""

from PySide6.QtWidgets import QPushButton, QWidget


class PublishButton(QPushButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Опубликовать", parent)
        self.setObjectName("primaryButton")
        self.setMinimumHeight(44)
        self.setMinimumWidth(180)
