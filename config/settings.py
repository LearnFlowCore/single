"""Paths and non-secret application settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "AutoPoster"
CONFIG_PATH = APP_DIR / "settings.json"
SECRETS_PATH = APP_DIR / "secrets.json"
DATABASE_PATH = APP_DIR / "autoposter.db"
LOG_PATH = APP_DIR / "logs" / "autoposter.log"


@dataclass(slots=True)
class Settings:
    network_mode: str = "system"
    proxy_url: str = ""
    public_media_base_url: str = ""
    public_site_url: str = ""
    media_port: int = 8080
    graph_version: str = "v21.0"

    @classmethod
    def load(cls) -> "Settings":
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if data.get("proxy_url") and "network_mode" not in data:
                data["network_mode"] = "proxy"
            known = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
            return cls(**known)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        _write_json(CONFIG_PATH, asdict(self))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
