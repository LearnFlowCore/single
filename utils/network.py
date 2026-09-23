"""Network mode and Windows proxy discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    proxy: str | None
    trust_env: bool
    description: str


def resolve_network(mode: str, custom_proxy: str = "") -> NetworkConfig:
    if mode == "direct":
        return NetworkConfig(None, False, "Прямое подключение")
    if mode == "proxy":
        proxy = normalize_proxy(custom_proxy)
        if not proxy:
            raise ValueError("Для режима прокси укажите адрес прокси-сервера")
        return NetworkConfig(proxy, False, f"Прокси: {_safe_proxy(proxy)}")

    environment = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if environment:
        proxy = normalize_proxy(environment)
        return NetworkConfig(proxy, True, f"Прокси из HTTPS_PROXY: {_safe_proxy(proxy)}")
    windows_proxy = _windows_proxy()
    if windows_proxy:
        return NetworkConfig(windows_proxy, False, f"Системный прокси Windows: {_safe_proxy(windows_proxy)}")
    return NetworkConfig(None, True, "Системные настройки, прямое подключение")


def normalize_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("Некорректный прокси. Используйте http://host:port или socks5://host:port")
    return value


def _windows_proxy() -> str:
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]) if enabled else ""
    except (ImportError, OSError, ValueError):
        return ""
    if ";" in server or "=" in server:
        entries = {}
        for item in server.split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                entries[key.lower()] = value
        server = entries.get("https") or entries.get("http") or ""
    return normalize_proxy(server) if server else ""


def _safe_proxy(proxy: str) -> str:
    parsed = urlsplit(proxy)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"
