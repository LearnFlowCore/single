"""Password-protected FastAPI interface and public download page."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as random_secrets
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config.settings import APP_DIR, Settings
from models.post import PostRepository
from services.publishing import authenticate_platform, publish_post
from utils.auth import SecretStore, extract_vk_access_token
from utils.logging_config import configure_logging
from utils.media import SUPPORTED_EXTENSIONS
from utils.network import resolve_network

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
UPLOAD_DIR = APP_DIR / "web_uploads"
PLATFORMS = {"vk": "ВКонтакте", "instagram": "Instagram", "telegram": "Telegram", "max": "MAX"}
CREDENTIAL_FIELDS = {
    "vk": [
        ("client_id", "ID приложения VK", "ID standalone-приложения, если получаете токен через OAuth."),
        ("access_token", "Личный токен VK", "Вставьте access token или полный URL после авторизации VK."),
        ("group_id", "ID группы", "Числовой ID без минуса; оставьте пустым для своей страницы."),
    ],
    "instagram": [
        ("app_id", "Meta App ID", "ID приложения Meta."),
        ("app_secret", "Meta App Secret", "Секрет приложения Meta."),
        ("access_token", "Долгосрочный токен Instagram", "Access token профессионального аккаунта Instagram."),
        ("account_id", "Instagram Account ID", "ID профессионального аккаунта Instagram."),
    ],
    "telegram": [
        ("bot_token", "Токен Telegram-бота", "Токен от BotFather. Telegram не разрешает публикацию через личный токен."),
        ("chat_id", "Канал или чат", "Например, @channel или числовой chat_id. Бот должен быть администратором."),
    ],
    "max": [
        ("bot_token", "Токен бота MAX", "Токен бота из платформы MAX для партнёров."),
        ("chat_id", "Канал или чат MAX", "ID канала или чата, куда добавлен бот."),
    ],
}
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
TEMPORARY_PASSWORD_SHA256 = "2aae8a7eb08409459c32c4a9a74a6059ffa3023e3df041f111dce59b72bcd065"

configure_logging()
app = FastAPI(title="Окно в другой мир", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
repository = PostRepository()
secret_store = SecretStore()


def _password() -> str:
    return os.getenv("AUTOPOSTER_WEB_PASSWORD", "")


def _username() -> str:
    return os.getenv("AUTOPOSTER_WEB_USERNAME", "Admin")


def _valid_password(value: str) -> bool:
    password = _password()
    if password:
        return hmac.compare_digest(value, password)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, TEMPORARY_PASSWORD_SHA256)


def _session_value() -> str:
    password = _password()
    key = os.getenv("AUTOPOSTER_WEB_SECRET", password or TEMPORARY_PASSWORD_SHA256).encode("utf-8")
    message = f"autoposter-admin:{_username()}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest() if key else ""


def _authenticated(request: Request) -> bool:
    expected = _session_value()
    return bool(expected) and hmac.compare_digest(request.cookies.get("autoposter_session", ""), expected)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):  # type: ignore[no-untyped-def]
    exe_exists = (PROJECT_ROOT / "dist" / "Okno-v-drugoi-mir-safe.zip").is_file()
    return templates.TemplateResponse(
        request, "landing.html", {"authenticated": _authenticated(request), "exe_exists": exe_exists}
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):  # type: ignore[no-untyped-def]
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "", "configured": True},
    )


@app.post("/login", response_class=HTMLResponse)
async def login(  # type: ignore[no-untyped-def]
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    valid_username = hmac.compare_digest(username, _username())
    valid_password = _valid_password(password)
    if not valid_username or not valid_password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль", "configured": True},
            status_code=401,
        )
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        "autoposter_session",
        _session_value(),
        httponly=True,
        secure=os.getenv("AUTOPOSTER_COOKIE_SECURE", "0") == "1",
        samesite="strict",
        max_age=8 * 60 * 60,
    )
    return response


@app.post("/logout")
async def logout():  # type: ignore[no-untyped-def]
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("autoposter_session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, message: str = ""):  # type: ignore[no-untyped-def]
    if not _authenticated(request):
        return _login_redirect()
    return _dashboard_response(request, message=message)


@app.post("/publish", response_class=HTMLResponse)
async def publish(
    request: Request,
    text: Annotated[str, Form()] = "",
    platforms: Annotated[list[str], Form()] = [],
    media: Annotated[list[UploadFile], File()] = [],
    browser_credentials: Annotated[str, Form()] = "",
):  # type: ignore[no-untyped-def]
    if not _authenticated(request):
        return _login_redirect()
    selected = [name for name in platforms if name in PLATFORMS]
    if not selected or (not text.strip() and not any(item.filename for item in media)):
        return _dashboard_response(
            request,
            error="Добавьте текст или медиа и выберите платформу.",
            error_target="publish",
        )

    paths: list[str] = []
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for upload in media:
            if not upload.filename:
                continue
            suffix = Path(upload.filename).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                raise ValueError(f"Формат {suffix or 'без расширения'} не поддерживается")
            path = UPLOAD_DIR / f"{random_secrets.token_hex(16)}{suffix}"
            size = 0
            with path.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("Файл превышает 250 МБ")
                    target.write(chunk)
            paths.append(str(path))

        post_id = repository.create(
            text=text.strip(), media_paths=paths, platforms=selected, status="publishing"
        )
        credentials = secret_store.load()
        _merge_browser_credentials(credentials, browser_credentials)
        results = await publish_post(text.strip(), paths, selected, credentials, Settings.load())
        all_success = bool(results) and all(item.get("success") for item in results.values())
        status = "success" if all_success else "error"
        repository.update_result(post_id, status, results)
        details = "; ".join(
            f"{PLATFORMS[name]}: {'успешно' if value.get('success') else value.get('error')}"
            for name, value in results.items()
        )
        if all_success:
            return _dashboard_response(request, message=details)
        return _dashboard_response(request, error=details, error_target="publish")
    except Exception as exc:
        return _dashboard_response(request, error=str(exc), error_target="publish")
    finally:
        for path_text in paths:
            Path(path_text).unlink(missing_ok=True)


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request):  # type: ignore[no-untyped-def]
    if not _authenticated(request):
        return _login_redirect()
    form = await request.form()
    stored = secret_store.load()
    try:
        changed_credentials = _update_credentials(stored, form)
    except ValueError as exc:
        return _dashboard_response(request, error=str(exc), error_target="settings")
    settings = Settings.load()
    settings.network_mode = str(form.get("network_mode", "system"))
    settings.proxy_url = str(form.get("proxy_url", "")).strip()
    settings.public_media_base_url = str(form.get("public_media_base_url", "")).strip()
    settings.public_site_url = str(form.get("public_site_url", "")).strip()
    try:
        resolve_network(settings.network_mode, settings.proxy_url)
    except ValueError as exc:
        return _dashboard_response(request, error=str(exc), error_target="settings")
    if ("vk", "access_token") in changed_credentials:
        try:
            await authenticate_platform("vk", stored, settings)
        except Exception as exc:
            return _dashboard_response(
                request,
                error=f"Токен VK не сохранён: {exc}",
                error_target="settings",
            )
    try:
        secret_store.save(stored)
        settings.save()
    except OSError as exc:
        return _dashboard_response(
            request,
            error=f"Не удалось сохранить настройки на сервере: {exc}",
            error_target="settings",
        )
    return _dashboard_response(request, message="Настройки сохранены.", settings_saved=True)


@app.post("/posts/{post_id}/delete")
async def delete_post(request: Request, post_id: int):  # type: ignore[no-untyped-def]
    if not _authenticated(request):
        return _login_redirect()
    repository.delete(post_id)
    return RedirectResponse("/dashboard?message=Запись+удалена", status_code=303)


@app.get("/download")
async def download():  # type: ignore[no-untyped-def]
    path = PROJECT_ROOT / "dist" / "Okno-v-drugoi-mir-safe.zip"
    if not path.is_file():
        return HTMLResponse("Сборка «Окно в другой мир» пока не опубликована", status_code=404)
    return FileResponse(
        path,
        filename="Okno-v-drugoi-mir-safe.zip",
        media_type="application/zip",
    )


def _dashboard_response(
    request: Request,
    *,
    message: str = "",
    error: str = "",
    error_target: str = "",
    settings_saved: bool = False,
):
    records = repository.recent(50)
    settings = Settings.load()
    credentials = _credential_view(secret_store.load())
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "message": message,
            "error": error,
            "error_target": error_target,
            "settings_saved": settings_saved,
            "platforms": PLATFORMS,
            "records": records,
            "settings": settings,
            "credentials": credentials,
            "render_url": str(request.base_url).rstrip("/"),
            "now": datetime.now(),
        },
    )


def _credential_view(stored: Mapping[str, Mapping[str, str]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for platform, definitions in CREDENTIAL_FIELDS.items():
        result[platform] = []
        for key, label, hint in definitions:
            value = stored.get(platform, {}).get(key, "")
            secret = "token" in key or "secret" in key
            result[platform].append(
                {
                    "key": key,
                    "label": label,
                    "hint": hint,
                    "secret": secret,
                    "saved": bool(value),
                    "value": "",
                }
            )
    return result


def _update_credentials(
    stored: dict[str, dict[str, str]], form: Mapping[str, Any]
) -> set[tuple[str, str]]:
    changed: set[tuple[str, str]] = set()
    for platform, fields in stored.items():
        for key in fields:
            value = str(form.get(f"{platform}_{key}", "")).strip()
            if platform == "vk" and key == "access_token" and value:
                value = extract_vk_access_token(value)
            if value:
                fields[key] = value
                changed.add((platform, key))
    return changed


def _merge_browser_credentials(
    stored: dict[str, dict[str, str]], raw: str
) -> None:
    if not raw:
        return
    if len(raw) > 32_768:
        raise ValueError("Данные подключений из браузера слишком велики")
    try:
        browser_values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Браузер передал повреждённые данные подключений") from exc
    if not isinstance(browser_values, dict):
        raise ValueError("Браузер передал неверный формат подключений")
    for platform, fields in stored.items():
        values = browser_values.get(platform, {})
        if not isinstance(values, dict):
            continue
        for key in fields:
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                fields[key] = value.strip()
