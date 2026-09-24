"""VK API v5.131 wall publisher."""

from __future__ import annotations

import mimetypes
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from utils.media import validate_media
from utils.auth import extract_vk_access_token

from .base import AuthenticationError, PostData, PublishError, SocialPlatform, require_success

VK_API_URL = "https://api.vk.com/method"
VK_API_VERSION = "5.131"
VK_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
VK_SCOPE_PHOTOS = 4
VK_SCOPE_WALL = 8192
VK_UPLOAD_ATTEMPTS = 3


@dataclass(slots=True)
class VKPostData(PostData):
    def validate(self) -> None:
        if not self.text.strip() and not self.media:
            raise ValueError("A VK post must contain text or photos")
        validate_media(
            self.media,
            max_files=10,
            allowed_extensions=VK_PHOTO_EXTENSIONS,
            max_image_bytes=50 * 1024 * 1024,
        )


class VKPublisher(SocialPlatform):
    def __init__(
        self,
        access_token: str,
        *,
        group_id: int | None = None,
        timeout: float = 30.0,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        access_token = extract_vk_access_token(access_token)
        if not access_token:
            raise ValueError("VK access_token is required")
        if group_id is not None and group_id <= 0:
            raise ValueError("VK group_id must be a positive integer")
        super().__init__(timeout=timeout, proxy=proxy, trust_env=trust_env)
        self._access_token = access_token
        self.group_id = group_id
        self._user_id: int | None = None

    async def authenticate(self) -> Mapping[str, Any]:
        try:
            payload = await self._vk_call("users.get", {}, "VK authentication")
            users = payload.get("response")
            if not isinstance(users, list) or not users:
                raise AuthenticationError("VK authentication returned no user")
            user = users[0]
            self._user_id = int(user["id"])
            permissions = await self._vk_call(
                "account.getAppPermissions", {}, "VK permissions check"
            )
            permission_mask = int(permissions.get("response", 0))
            missing = []
            if not permission_mask & VK_SCOPE_WALL:
                missing.append("wall")
            if not permission_mask & VK_SCOPE_PHOTOS:
                missing.append("photos")
            if missing:
                raise AuthenticationError(
                    "Токен VK не содержит прав: " + ", ".join(missing) + ". Получите токен заново."
                )
            group_name = ""
            if self.group_id is not None:
                groups = await self._vk_call(
                    "groups.getById", {"group_id": self.group_id}, "VK group access check"
                )
                group_data = groups.get("response")
                if isinstance(group_data, dict):
                    group_data = group_data.get("groups")
                if not isinstance(group_data, list) or not group_data:
                    raise AuthenticationError("У токена нет доступа к указанной группе VK")
                group_name = str(group_data[0].get("name", ""))
            return {
                "id": self._user_id,
                "first_name": user.get("first_name", ""),
                "last_name": user.get("last_name", ""),
                "group": group_name,
            }
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Авторизация VK не выполнена: {exc}") from exc

    async def publish(self, post: PostData) -> Mapping[str, Any]:
        if not isinstance(post, VKPostData):
            raise TypeError("VKPublisher requires VKPostData")
        post.validate()
        if self._user_id is None:
            await self.authenticate()

        try:
            attachments = [await self._upload_photo(Path(item)) for item in post.media]
            params: dict[str, Any] = {
                "message": post.text,
                "random_id": secrets.randbits(31) or 1,
            }
            if attachments:
                params["attachments"] = ",".join(attachments)
            if self.group_id is not None:
                params.update(owner_id=-self.group_id, from_group=1)
            payload = await self._vk_call("wall.post", params, "VK wall post")
            response = payload.get("response")
            if not isinstance(response, dict) or not response.get("post_id"):
                raise PublishError("VK wall post returned an unexpected response")
            return response
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"VK publishing failed: {exc}") from exc

    async def _upload_photo(self, path: Path) -> str:
        upload_params: dict[str, Any] = {}
        if self.group_id is not None:
            upload_params["group_id"] = self.group_id
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        for attempt in range(1, VK_UPLOAD_ATTEMPTS + 1):
            upload_server = await self._vk_call(
                "photos.getWallUploadServer", upload_params, "VK photo upload setup"
            )
            server_data = upload_server.get("response")
            if not isinstance(server_data, dict) or not server_data.get("upload_url"):
                raise PublishError("VK did not return a photo upload URL")

            with path.open("rb") as source:
                upload_response = await self._request(
                    "POST",
                    str(server_data["upload_url"]),
                    operation="VK photo upload",
                    files={"photo": (path.name, source, mime_type)},
                )
            uploaded = require_success(upload_response, "VK photo upload")
            if not _is_complete_upload(uploaded):
                self._logger.warning(
                    "VK upload server did not accept photo; requesting a new server (%d/%d)",
                    attempt,
                    VK_UPLOAD_ATTEMPTS,
                )
                continue

            save_params: dict[str, Any] = {
                "server": uploaded["server"],
                "photo": uploaded["photo"],
                "hash": uploaded["hash"],
            }
            if self.group_id is not None:
                save_params["group_id"] = self.group_id
            try:
                saved_payload = await self._vk_call(
                    "photos.saveWallPhoto", save_params, "VK photo save"
                )
            except PublishError as exc:
                if attempt < VK_UPLOAD_ATTEMPTS and "photo is undefined" in str(exc).lower():
                    self._logger.warning(
                        "VK rejected uploaded photo; retrying with a new server (%d/%d)",
                        attempt,
                        VK_UPLOAD_ATTEMPTS,
                    )
                    continue
                raise
            saved = saved_payload.get("response")
            if not isinstance(saved, list) or not saved:
                raise PublishError("VK did not return the saved photo")
            photo = saved[0]
            return f"photo{photo['owner_id']}_{photo['id']}"

        raise PublishError(
            "VK не принял изображение после трёх попыток. "
            "Попробуйте выбрать файл заново или сохранить его как JPG."
        )

    async def _vk_call(
        self, method: str, params: dict[str, Any], operation: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{VK_API_URL}/{method}",
            operation=operation,
            data={**params, "access_token": self._access_token, "v": VK_API_VERSION},
        )
        payload = require_success(response, operation)
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("error_msg") or "unknown VK API error"
            code = error.get("error_code")
            if code == 5:
                raise AuthenticationError("Токен VK недействителен или истёк. Получите новый токен.")
            if code == 7:
                raise AuthenticationError("Токен VK не имеет нужных прав wall/photos.")
            if code == 15:
                raise AuthenticationError("Доступ к группе VK запрещён. Проверьте ID и права администратора.")
            raise PublishError(f"{operation}: {message}")
        return payload


def _is_complete_upload(payload: dict[str, Any]) -> bool:
    photo = payload.get("photo")
    return (
        payload.get("server") is not None
        and isinstance(photo, str)
        and bool(photo.strip())
        and photo.strip() != "[]"
        and bool(payload.get("hash"))
    )


__all__ = ["VKPostData", "VKPublisher"]
