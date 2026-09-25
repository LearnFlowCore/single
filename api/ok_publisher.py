"""Odnoklassniki REST API group publisher."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from utils.media import validate_media

from .base import APIError, AuthenticationError, PostData, PublishError, SocialPlatform

OK_API_URL = "https://api.ok.ru/fb.do"
OK_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
OK_UPLOAD_HOST_SUFFIXES = (".ok.ru", ".okcdn.ru", ".mycdn.me")


@dataclass(slots=True)
class OkPostData(PostData):
    def validate(self) -> None:
        if not self.text.strip() and not self.media:
            raise ValueError("Публикация в Одноклассниках должна содержать текст или фото")
        validate_media(
            self.media,
            max_files=10,
            allowed_extensions=OK_PHOTO_EXTENSIONS,
            max_image_bytes=50 * 1024 * 1024,
            max_video_bytes=0,
        )


class OkPublisher(SocialPlatform):
    def __init__(
        self,
        application_id: str,
        application_key: str,
        application_secret: str,
        access_token: str,
        session_secret_key: str,
        group_id: str | int,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        values = {
            "Application ID": application_id,
            "публичный ключ": application_key,
            "access_token": access_token,
            "ID группы": str(group_id),
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise ValueError(f"Для Одноклассников не заполнено: {', '.join(missing)}")
        if not session_secret_key.strip() and not application_secret.strip():
            raise ValueError("Укажите session_secret_key или секретный ключ приложения OK")
        if not str(group_id).strip().isdigit() or int(str(group_id).strip()) <= 0:
            raise ValueError("ID группы OK должен быть положительным числом")
        super().__init__(timeout=timeout, proxy=proxy, trust_env=trust_env)
        self.application_id = application_id.strip()
        self.application_key = application_key.strip()
        self.application_secret = application_secret.strip()
        self.access_token = access_token.strip()
        self.session_secret_key = session_secret_key.strip()
        self.group_id = str(group_id).strip()

    async def authenticate(self) -> Mapping[str, Any]:
        try:
            payload = await self._ok_call(
                "users.getCurrentUser",
                {"fields": "uid,name"},
                "Авторизация в Одноклассниках",
            )
            if not isinstance(payload, dict) or not payload.get("uid"):
                raise AuthenticationError("OK API не вернул данные текущего пользователя")
            return {
                "id": str(payload["uid"]),
                "first_name": str(payload.get("name") or ""),
            }
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Авторизация в Одноклассниках не выполнена: {exc}") from exc

    async def publish(self, post: PostData) -> Mapping[str, Any]:
        if not isinstance(post, OkPostData):
            raise TypeError("OkPublisher requires OkPostData")
        post.validate()
        try:
            media: list[dict[str, Any]] = []
            if post.text.strip():
                media.append({"type": "text", "text": post.text})
            if post.media:
                tokens = await self._upload_photos([Path(item) for item in post.media])
                media.append({"type": "photo", "list": [{"id": token} for token in tokens]})
            topic_id = await self._ok_call(
                "mediatopic.post",
                {
                    "type": "GROUP_THEME",
                    "gid": self.group_id,
                    "attachment": json.dumps(
                        {"media": media}, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                "Публикация в Одноклассниках",
            )
            if not isinstance(topic_id, (str, int)) or not str(topic_id):
                raise PublishError("OK API не вернул ID опубликованной темы")
            return {
                "topic_id": str(topic_id),
                "url": f"https://ok.ru/group/{self.group_id}/topic/{topic_id}",
            }
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"Публикация в Одноклассниках не выполнена: {exc}") from exc

    async def _upload_photos(self, paths: list[Path]) -> list[str]:
        setup = await self._ok_call(
            "photosV2.getUploadUrl",
            {
                "gid": self.group_id,
                "count": str(len(paths)),
                "sizes": ",".join(str(path.stat().st_size) for path in paths),
            },
            "Подготовка фото для Одноклассников",
        )
        if not isinstance(setup, dict):
            raise PublishError("OK API не вернул параметры загрузки фото")
        upload_url = str(setup.get("upload_url") or "")
        if not _trusted_upload_url(upload_url):
            raise PublishError("OK API вернул недопустимый адрес загрузки фото")
        with ExitStack() as stack:
            files = {
                f"pic{index}": (
                    path.name,
                    stack.enter_context(path.open("rb")),
                    "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else None,
                )
                for index, path in enumerate(paths, start=1)
            }
            response = await self._request(
                "POST",
                upload_url,
                operation="Загрузка фото в Одноклассники",
                files=files,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublishError("Сервер OK вернул некорректный ответ при загрузке фото") from exc
        if not response.is_success or not isinstance(payload, dict):
            raise PublishError(f"Загрузка фото в OK завершилась с HTTP {response.status_code}")
        photos = payload.get("photos")
        if not isinstance(photos, dict):
            if payload.get("error_code"):
                raise PublishError(
                    "Загрузка фото в OK отклонена: "
                    f"{payload.get('error_msg') or payload.get('error_code')}"
                )
            raise PublishError("OK API не вернул токены загруженных фото")
        photo_ids = setup.get("photo_ids")
        if isinstance(photo_ids, list) and len(photo_ids) == len(paths):
            photo_results = [photos.get(str(photo_id)) for photo_id in photo_ids]
        else:
            photo_results = list(photos.values())
        tokens = [
            str(item.get("token") or "")
            for item in photo_results
            if isinstance(item, dict)
        ]
        if len(tokens) != len(paths) or any(not token for token in tokens):
            raise PublishError("OK API вернул не все токены загруженных фото")
        return tokens

    async def _ok_call(self, method: str, params: dict[str, str], operation: str) -> Any:
        signed = {
            "application_key": self.application_key,
            "format": "json",
            "method": method,
            **params,
        }
        signature_source = "".join(
            f"{key}={value}" for key, value in sorted(signed.items())
        ) + self._signing_secret()
        data = {
            **signed,
            "access_token": self.access_token,
            "sig": hashlib.md5(signature_source.encode("utf-8")).hexdigest(),
        }
        response = await self._request("POST", OK_API_URL, operation=operation, data=data)
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError(f"{operation}: OK API вернул некорректный JSON") from exc
        if not response.is_success:
            raise APIError(f"{operation}: HTTP {response.status_code}")
        if isinstance(payload, dict) and payload.get("error_code"):
            code = payload.get("error_code")
            message = payload.get("error_msg") or "неизвестная ошибка OK API"
            if code in {102, 103, 104, 401}:
                raise AuthenticationError(f"OK отклонил данные авторизации: {message}")
            if "permission" in str(message).lower() or code == 10:
                raise PublishError(
                    f"OK не выдал необходимое право GROUP_CONTENT/PHOTO_CONTENT: {message}"
                )
            raise APIError(f"OK API error {code}: {message}")
        return payload

    def _signing_secret(self) -> str:
        if self.session_secret_key:
            return self.session_secret_key
        return hashlib.md5(
            f"{self.access_token}{self.application_secret}".encode("utf-8")
        ).hexdigest()


def _trusted_upload_url(url: str) -> bool:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in OK_UPLOAD_HOST_SUFFIXES
    )


__all__ = ["OkPostData", "OkPublisher"]
