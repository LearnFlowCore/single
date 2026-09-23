"""Platform selection controls."""

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QWidget


class PlatformSelector(QWidget):
    LABELS = {
        "vk": "ВКонтакте",
        "instagram": "Instagram",
        "telegram": "Telegram",
        "max": "MAX",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.boxes: dict[str, QCheckBox] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for key, label in self.LABELS.items():
            box = QCheckBox(label)
            box.setObjectName("platformCheck")
            self.boxes[key] = box
            layout.addWidget(box)
        layout.addStretch()

    def selected(self) -> list[str]:
        return [key for key, box in self.boxes.items() if box.isChecked()]
