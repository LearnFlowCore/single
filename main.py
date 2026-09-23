"""AutoPoster application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from utils.logging_config import configure_logging


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative


def main() -> int:
    configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("AutoPoster")
    app.setOrganizationName("AutoPoster")
    style_path = resource_path("ui/styles/wp_style.qss")
    if style_path.exists():
        app.setStyleSheet(style_path.read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
