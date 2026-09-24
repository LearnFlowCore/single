"""Platform-neutral publishing orchestration."""

from __future__ import annotations

import asyncio
from typing import Any

from api.instagram_publisher import InstagramPostData, InstagramPublisher
from api.max_publisher import MaxPostData, MaxPublisher
from api.telegram_publisher import TelegramPostData, TelegramPublisher
from api.vk_publisher import VKPostData, VKPublisher
from config.settings import Settings
from utils.media import prepare_media_for_publish
from utils.network import resolve_network


async def publish_post(
    text: str,
    media_paths: list[str],
    platforms: list[str],
    secrets: dict[str, dict[str, str]],
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    with prepare_media_for_publish(
        media_paths, for_instagram="instagram" in platforms
    ) as prepared_media:
        pairs = await asyncio.gather(
            *(_publish_one(name, text, prepared_media, secrets, settings) for name in platforms)
        )
    return dict(pairs)


async def authenticate_platform(
    platform: str, secrets: dict[str, dict[str, str]], settings: Settings
) -> dict[str, Any]:
    publisher, _ = _build(platform, "", [], secrets, settings)
    try:
        return dict(await publisher.authenticate())
    finally:
        await publisher.aclose()


async def _publish_one(
    platform: str,
    text: str,
    media_paths: list[str],
    secrets: dict[str, dict[str, str]],
    settings: Settings,
) -> tuple[str, dict[str, Any]]:
    publisher = None
    try:
        publisher, post = _build(platform, text, media_paths, secrets, settings)
        response = await publisher.publish(post)
        return platform, {"success": True, "url": _result_url(platform, response), "error": ""}
    except Exception as exc:
        return platform, {"success": False, "url": "", "error": str(exc)}
    finally:
        if publisher is not None:
            await publisher.aclose()


def _build(
    platform: str,
    text: str,
    media_paths: list[str],
    secrets: dict[str, dict[str, str]],
    settings: Settings,
):  # type: ignore[no-untyped-def]
    network = resolve_network(settings.network_mode, settings.proxy_url)
    common = {"proxy": network.proxy, "trust_env": network.trust_env}
    if platform == "vk":
        values = secrets["vk"]
        group_id = int(values["group_id"]) if values["group_id"].strip() else None
        return VKPublisher(values["access_token"], group_id=group_id, **common), VKPostData(
            text=text, media=media_paths
        )
    if platform == "instagram":
        values = secrets["instagram"]
        return InstagramPublisher(
            values["access_token"],
            values["account_id"],
            graph_version=settings.graph_version,
            public_media_base_url=settings.public_media_base_url or None,
            media_port=settings.media_port,
            **common,
        ), InstagramPostData(text=text, media=media_paths)
    if platform == "telegram":
        values = secrets["telegram"]
        return TelegramPublisher(values["bot_token"], values["chat_id"], **common), TelegramPostData(
            text=text, media=media_paths
        )
    if platform == "max":
        values = secrets["max"]
        return MaxPublisher(values["bot_token"], values["chat_id"], **common), MaxPostData(
            text=text, media=media_paths
        )
    raise ValueError(f"Неизвестная платформа: {platform}")


def _result_url(platform: str, response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    if platform == "max" and isinstance(response.get("message"), dict):
        return str(response["message"].get("link") or "")
    return str(response.get("url") or "")
