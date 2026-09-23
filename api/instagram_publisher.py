"""Instagram Graph API publisher with temporary local media hosting."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from utils.media import MediaInfo, validate_media
from utils.media_server import PublicMediaServer

from .base import AuthenticationError, PostData, PublishError, SocialPlatform, require_success

INSTAGRAM_EXTENSIONS = {".jpg", ".jpeg", ".mp4"}


@dataclass(slots=True)
class InstagramPostData(PostData):
    def validate(self) -> None:
        if not self.media:
            raise ValueError("Instagram posts require at least one image or video")
        validate_media(
            self.media,
            max_files=10,
            allow_remote=True,
            allowed_extensions=INSTAGRAM_EXTENSIONS,
            max_image_bytes=8 * 1024 * 1024,
            max_video_bytes=100 * 1024 * 1024,
        )
        if len(self.text) > 2200:
            raise ValueError("Instagram captions are limited to 2200 characters")


class InstagramPublisher(SocialPlatform):
    def __init__(
        self,
        access_token: str,
        account_id: str | int,
        *,
        graph_version: str = "v21.0",
        public_media_base_url: str | None = None,
        media_host: str = "0.0.0.0",
        media_port: int = 8080,
        status_poll_interval: float = 2.0,
        status_poll_timeout: float = 120.0,
        timeout: float = 30.0,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        if not access_token:
            raise ValueError("Instagram access_token is required")
        if str(account_id).strip() == "":
            raise ValueError("Instagram account_id is required")
        if not graph_version.startswith("v"):
            raise ValueError("graph_version must look like 'v21.0'")
        if status_poll_interval <= 0 or status_poll_timeout <= 0:
            raise ValueError("Instagram polling intervals must be positive")
        super().__init__(timeout=timeout, proxy=proxy, trust_env=trust_env)
        self._access_token = access_token
        self.account_id = str(account_id)
        self._base_url = f"https://graph.facebook.com/{graph_version}"
        self.public_media_base_url = public_media_base_url
        self.media_host = media_host
        self.media_port = media_port
        self.status_poll_interval = status_poll_interval
        self.status_poll_timeout = status_poll_timeout

    async def authenticate(self) -> Mapping[str, Any]:
        try:
            payload = await self._graph_get(
                self.account_id,
                {"fields": "id,username"},
                "Instagram authentication",
            )
            if not payload.get("username"):
                raise AuthenticationError("Instagram account username was not returned")
            return {"id": payload.get("id", self.account_id), "username": payload["username"]}
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Instagram authentication failed: {exc}") from exc

    async def publish(self, post: PostData) -> Mapping[str, Any]:
        if not isinstance(post, InstagramPostData):
            raise TypeError("InstagramPublisher requires InstagramPostData")
        post.validate()
        media = validate_media(
            post.media,
            max_files=10,
            allow_remote=True,
            allowed_extensions=INSTAGRAM_EXTENSIONS,
            max_image_bytes=8 * 1024 * 1024,
            max_video_bytes=100 * 1024 * 1024,
        )
        local = [item.path for item in media if item.path is not None]
        if local and not self.public_media_base_url:
            raise ValueError(
                "public_media_base_url is required for local Instagram media"
            )

        server: PublicMediaServer | None = None
        try:
            if local:
                server = PublicMediaServer(
                    local,
                    public_base_url=self.public_media_base_url or "",
                    host=self.media_host,
                    port=self.media_port,
                ).start()
            urls = [self._media_url(item, server) for item in media]
            creation_id = await self._create_post(media, urls, post.text)
            await self._wait_for_container(creation_id)
            published = await self._graph_post(
                f"{self.account_id}/media_publish",
                {"creation_id": creation_id},
                "Instagram media publish",
            )
            if not published.get("id"):
                raise PublishError("Instagram did not return a published media ID")
            return published
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(f"Instagram publishing failed: {exc}") from exc
        finally:
            if server is not None:
                server.stop()

    async def _create_post(
        self, media: list[MediaInfo], urls: list[str], caption: str
    ) -> str:
        if len(media) == 1:
            params = self._container_params(media[0], urls[0], carousel_item=False)
            params["caption"] = caption
            container_id = await self._create_container(params)
            return container_id

        children: list[str] = []
        for item, url in zip(media, urls, strict=True):
            child_id = await self._create_container(
                self._container_params(item, url, carousel_item=True)
            )
            await self._wait_for_container(child_id)
            children.append(child_id)
        parent = await self._create_container(
            {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}
        )
        return parent

    @staticmethod
    def _container_params(
        media: MediaInfo, url: str, *, carousel_item: bool
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"is_carousel_item": str(carousel_item).lower()}
        if media.kind == "video":
            params.update(media_type="VIDEO" if carousel_item else "REELS", video_url=url)
        else:
            params["image_url"] = url
        return params

    async def _create_container(self, params: dict[str, Any]) -> str:
        payload = await self._graph_post(
            f"{self.account_id}/media", params, "Instagram container creation"
        )
        container_id = payload.get("id")
        if not container_id:
            raise PublishError("Instagram did not return a container ID")
        return str(container_id)

    async def _wait_for_container(self, container_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.status_poll_timeout
        while True:
            payload = await self._graph_get(
                container_id,
                {"fields": "status_code,status"},
                "Instagram container status",
            )
            status = str(payload.get("status_code", "")).upper()
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                detail = payload.get("status") or status
                raise PublishError(f"Instagram container processing failed: {detail}")
            if loop.time() >= deadline:
                raise PublishError(
                    f"Instagram container was not ready within {self.status_poll_timeout:g} seconds"
                )
            await asyncio.sleep(self.status_poll_interval)

    def _media_url(self, media: MediaInfo, server: PublicMediaServer | None) -> str:
        if media.path is None:
            return str(media.source)
        if server is None:
            raise PublishError("Local Instagram media server is not running")
        return server.url_for(media.path)

    async def _graph_get(
        self, path: str, params: dict[str, Any], operation: str
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"{self._base_url}/{path}",
            operation=operation,
            params={**params, "access_token": self._access_token},
        )
        return require_success(response, operation)

    async def _graph_post(
        self, path: str, data: dict[str, Any], operation: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{self._base_url}/{path}",
            operation=operation,
            data={**data, "access_token": self._access_token},
        )
        return require_success(response, operation)


__all__ = ["InstagramPostData", "InstagramPublisher"]
