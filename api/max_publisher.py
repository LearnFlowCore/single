"""MAX Bot API publisher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from utils.media import MediaInfo, validate_media

from .base import AuthenticationError, PostData, PublishError, SocialPlatform, require_success

MAX_API_URL = "https://platform-api2.max.ru"
MAX_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".mp4"}
TRUSTED_UPLOAD_SUFFIXES = (".max.ru", ".oneme.ru", ".okcdn.ru")


@dataclass(slots=True)
class MaxPostData(PostData):
    def validate(self) -> None:
        if not self.text.strip() and not self.media:
            raise ValueError("Публикация MAX должна содержать текст или медиа")
        if len(self.text) > 4000:
            raise ValueError("Текст MAX ограничен 4000 символами")
        validate_media(
            self.media,
            max_files=10,
            allowed_extensions=MAX_EXTENSIONS,
            max_image_bytes=50 * 1024 * 1024,
            max_video_bytes=250 * 1024 * 1024,
        )


class MaxPublisher(SocialPlatform):
    def __init__(
        self,
        access_token: str,
        chat_id: str | int,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        if not access_token.strip():
            raise ValueError("Токен бота MAX обязателен")
        if not str(chat_id).strip():
            raise ValueError("chat_id MAX обязателен")
        try:
            self.chat_id = int(str(chat_id).strip())
        except ValueError as exc:
            raise ValueError("chat_id MAX должен быть числом") from exc
        super().__init__(timeout=timeout, proxy=proxy, trust_env=trust_env)
        self._headers = {"Authorization": access_token.strip()}

    async def authenticate(self) -> Mapping[str, Any]:
        try:
            response = await self._request(
                "GET", f"{MAX_API_URL}/me", operation="Авторизация MAX", headers=self._headers
            )
            bot = require_success(response, "Авторизация MAX")
            if not bot.get("is_bot"):
                raise AuthenticationError("Токен MAX не принадлежит боту")
            chat_response = await self._request(
                "GET",
                f"{MAX_API_URL}/chats/{self.chat_id}/members/me",
                operation="Проверка доступа к чату MAX",
                headers=self._headers,
            )
            require_success(chat_response, "Проверка доступа к чату MAX")
            return {
                "id": bot.get("user_id"),
                "username": bot.get("username") or "",
                "first_name": bot.get("first_name") or bot.get("name") or "",
            }
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Авторизация MAX не выполнена: {exc}") from exc

    async def publish(self, post: PostData) -> Mapping[str, Any]:
        if not isinstance(post, MaxPostData):
            raise TypeError("MaxPublisher requires MaxPostData")
        post.validate()
        media = validate_media(
            post.media,
            max_files=10,
            allowed_extensions=MAX_EXTENSIONS,
            max_image_bytes=50 * 1024 * 1024,
            max_video_bytes=250 * 1024 * 1024,
        )
        try:
            attachments = [await self._upload(item) for item in media]
            if attachments:
                await asyncio.sleep(1)
            body: dict[str, Any] = {"text": post.text}
            if attachments:
                body["attachments"] = attachments
            return await self._send_message(body)
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"Публикация в MAX не выполнена: {exc}") from exc

    async def _upload(self, media: MediaInfo) -> dict[str, Any]:
        assert media.path is not None
        media_type = "video" if media.kind == "video" else "image"
        setup_response = await self._request(
            "POST",
            f"{MAX_API_URL}/uploads",
            operation="Подготовка загрузки MAX",
            params={"type": media_type},
            headers=self._headers,
        )
        setup = require_success(setup_response, "Подготовка загрузки MAX")
        upload_url = str(setup.get("url") or "")
        if not upload_url:
            raise PublishError("MAX не вернул URL загрузки медиа")
        upload_headers = self._headers if _trusted_upload_url(upload_url) else {}
        with media.path.open("rb") as source:
            upload_response = await self._request(
                "POST",
                upload_url,
                operation="Загрузка медиа в MAX",
                headers=upload_headers,
                files={"data": (media.path.name, source, media.mime_type)},
            )
        token = str(setup.get("token") or "")
        if not token:
            try:
                token = _find_token(upload_response.json())
            except ValueError as exc:
                raise PublishError("MAX не вернул токен загруженного медиа") from exc
        if not upload_response.is_success:
            require_success(upload_response, "Загрузка медиа в MAX")
        if not token:
            raise PublishError("MAX не вернул токен загруженного медиа")
        return {"type": media_type, "payload": {"token": token}}

    async def _send_message(self, body: dict[str, Any]) -> Mapping[str, Any]:
        for attempt in range(3):
            response = await self._request(
                "POST",
                f"{MAX_API_URL}/messages",
                operation="Отправка сообщения MAX",
                params={"chat_id": self.chat_id},
                headers=self._headers,
                json=body,
            )
            try:
                payload = response.json()
            except ValueError:
                return require_success(response, "Отправка сообщения MAX")
            if response.is_success:
                if not isinstance(payload, dict):
                    raise PublishError("MAX вернул неожиданный ответ")
                return payload
            if isinstance(payload, dict) and payload.get("code") == "attachment.not.ready" and attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            require_success(response, "Отправка сообщения MAX")
        raise PublishError("MAX не завершил обработку вложений")


def _find_token(value: Any) -> str:
    if isinstance(value, dict):
        token = value.get("token")
        if isinstance(token, str) and token:
            return token
        for child in value.values():
            found = _find_token(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_token(child)
            if found:
                return found
    return ""


def _trusted_upload_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in TRUSTED_UPLOAD_SUFFIXES)


__all__ = ["MaxPostData", "MaxPublisher"]
