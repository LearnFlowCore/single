"""Application logging setup."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config.settings import LOG_PATH


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
