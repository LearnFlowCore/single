"""Telegram Bot API publisher."""

from __future__ import annotations

import json
import html
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from utils.media import MediaInfo, validate_media

from .base import AuthenticationError, PostData, PublishError, SocialPlatform, require_success


@dataclass(slots=True)
class TelegramPostData(PostData):
    def validate(self) -> None:
        if not self.text.strip() and not self.media:
            raise ValueError("A Telegram post must contain text or media")
        if not self.media and len(self.text) > 4096:
            raise ValueError("Telegram text messages are limited to 4096 characters")
        if self.media and len(self.text) > 1024:
            raise ValueError("Telegram media captions are limited to 1024 characters")
        media = validate_media(
            self.media,
            max_files=10,
            max_image_bytes=10 * 1024 * 1024,
            max_video_bytes=50 * 1024 * 1024,
        )
        if len(media) > 1 and any(item.extension == ".gif" for item in media):
            raise ValueError("Telegram не поддерживает GIF внутри медиагруппы")


class TelegramPublisher(SocialPlatform):
    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        if not bot_token:
            raise ValueError("Telegram bot_token is required")
        if str(chat_id).strip() == "":
            raise ValueError("Telegram chat_id is required")
        super().__init__(timeout=timeout, proxy=proxy, trust_env=trust_env)
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = str(chat_id)

    async def authenticate(self) -> Mapping[str, Any]:
        try:
            payload = await self._call("getMe", operation="Telegram authentication")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AuthenticationError("Telegram authentication returned no bot")
            return {
                "id": result.get("id"),
                "username": result.get("username", ""),
                "first_name": result.get("first_name", ""),
            }
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Telegram authentication failed: {exc}") from exc

    async def publish(self, post: PostData) -> Mapping[str, Any]:
        if not isinstance(post, TelegramPostData):
            raise TypeError("TelegramPublisher requires TelegramPostData")
        post.validate()
        media = validate_media(
            post.media,
            max_files=10,
            max_image_bytes=10 * 1024 * 1024,
            max_video_bytes=50 * 1024 * 1024,
        )
        try:
            if not media:
                return await self._result(
                    "sendMessage",
                    data={"chat_id": self.chat_id, "text": html.escape(post.text), "parse_mode": "HTML"},
                )
            if len(media) == 1:
                return await self._send_single(media[0], post.text)
            return await self._send_group(media, post.text)
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"Telegram publishing failed: {exc}") from exc

    async def _send_single(self, media: MediaInfo, caption: str) -> Mapping[str, Any]:
        assert media.path is not None
        if media.extension == ".gif":
            method, field = "sendAnimation", "animation"
        else:
            method = "sendVideo" if media.kind == "video" else "sendPhoto"
            field = "video" if media.kind == "video" else "photo"
        with media.path.open("rb") as source:
            return await self._result(
                method,
                data={"chat_id": self.chat_id, "caption": caption},
                files={field: (media.path.name, source, media.mime_type)},
            )

    async def _send_group(self, media: list[MediaInfo], caption: str) -> Mapping[str, Any]:
        if len(media) < 2:
            raise ValueError("Telegram media groups require at least two items")
        descriptors: list[dict[str, str]] = []
        with ExitStack() as stack:
            files: dict[str, tuple[str, Any, str]] = {}
            for index, item in enumerate(media):
                assert item.path is not None
                field = f"media{index}"
                media_type = "photo" if item.kind == "image" else item.kind
                descriptor = {"type": media_type, "media": f"attach://{field}"}
                if index == 0 and caption:
                    descriptor["caption"] = caption
                descriptors.append(descriptor)
                source = stack.enter_context(item.path.open("rb"))
                files[field] = (item.path.name, source, item.mime_type)
            return await self._result(
                "sendMediaGroup",
                data={"chat_id": self.chat_id, "media": json.dumps(descriptors)},
                files=files,
            )

    async def _result(self, method: str, **kwargs: Any) -> Mapping[str, Any]:
        payload = await self._call(method, operation=f"Telegram {method}", **kwargs)
        result = payload.get("result")
        if isinstance(result, (dict, list)):
            return {"result": result}
        raise PublishError(f"Telegram {method} returned an unexpected response")

    async def _call(self, method: str, *, operation: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(
            "POST", f"{self._base_url}/{method}", operation=operation, **kwargs
        )
        payload = require_success(response, operation)
        if payload.get("ok") is not True:
            raise PublishError(
                f"{operation} failed: {payload.get('description') or 'unknown Telegram API error'}"
            )
        return payload


__all__ = ["TelegramPostData", "TelegramPublisher"]
