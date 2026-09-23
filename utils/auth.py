"""Credential storage and OAuth URL helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from config.settings import APP_DIR, SECRETS_PATH, _write_json


EMPTY_SECRETS: dict[str, dict[str, str]] = {
    "vk": {"client_id": "", "access_token": "", "group_id": ""},
    "instagram": {
        "app_id": "",
        "app_secret": "",
        "access_token": "",
        "account_id": "",
    },
    "telegram": {"bot_token": "", "chat_id": ""},
    "max": {"bot_token": "", "chat_id": ""},
}


class SecretStore:
    def __init__(self, path: Path = SECRETS_PATH) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, str]]:
        result = {name: values.copy() for name, values in EMPTY_SECRETS.items()}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return result
        if not isinstance(stored, dict):
            return result
        for platform, defaults in result.items():
            values = stored.get(platform, {})
            if isinstance(values, dict):
                defaults.update({key: str(value) for key, value in values.items() if key in defaults})
        return result

    def save(self, secrets: dict[str, dict[str, str]]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        _write_json(self.path, secrets)


def vk_oauth_url(client_id: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "scope": "wall,photos,offline",
            "redirect_uri": "https://oauth.vk.com/blank.html",
            "display": "page",
            "response_type": "token",
            "v": "5.131",
        }
    )
    return f"https://oauth.vk.com/authorize?{query}"


def extract_vk_access_token(value: str) -> str:
    """Extract a VK token from raw text or the OAuth redirect URL."""

    candidate = value.strip()
    if not candidate:
        raise ValueError("Токен VK не указан")
    for prefix in ("token=", "access_token=", "bearer "):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix) :].strip()
    if "://" not in candidate and "access_token=" not in candidate:
        return candidate
    parsed = urlsplit(candidate if "://" in candidate else f"https://local/#{candidate}")
    values = parse_qs(parsed.fragment or parsed.query)
    token = values.get("access_token", [""])[0].strip()
    if not token:
        error = values.get("error_description", values.get("error", [""]))[0]
        raise ValueError(f"VK не вернул access_token{': ' + error if error else ''}")
    return token
