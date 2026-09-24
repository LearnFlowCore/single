from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image

from api.instagram_publisher import InstagramPublisher
from api.max_publisher import MaxPostData, MaxPublisher
from api.telegram_publisher import TelegramPostData, TelegramPublisher
from api.vk_publisher import VKPostData, VKPublisher
from utils.auth import SecretStore, extract_vk_access_token
from utils.media import prepare_media_for_publish, validate_media
from utils.network import resolve_network
from web.app import _credential_view, _update_credentials


class CoreTests(unittest.TestCase):
    def test_media_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "photo.jpg"
            image.write_bytes(b"image")
            info = validate_media([image])
            self.assertEqual(info[0].kind, "image")

    def test_webp_is_normalized_to_rgb_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transparent.webp"
            Image.new("RGBA", (2000, 1000), (255, 0, 0, 128)).save(source, "WEBP")
            with prepare_media_for_publish([source]) as prepared:
                result = Path(prepared[0])
                self.assertEqual(result.suffix, ".jpg")
                with Image.open(result) as image:
                    self.assertEqual(image.mode, "RGB")
                    self.assertLessEqual(max(image.size), 1440)

    def test_instagram_image_ratio_is_padded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "portrait.png"
            Image.new("RGB", (400, 1200), "blue").save(source)
            with prepare_media_for_publish([source], for_instagram=True) as prepared:
                with Image.open(prepared[0]) as image:
                    self.assertGreaterEqual(image.width / image.height, 0.8)

    def test_secret_store_utf8_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            store = SecretStore(path)
            values = store.load()
            values["telegram"]["chat_id"] = "@канал"
            store.save(values)
            self.assertEqual(store.load()["telegram"]["chat_id"], "@канал")

    def test_vk_token_from_redirect_url(self) -> None:
        url = "https://oauth.vk.com/blank.html#access_token=user-token&expires_in=0"
        self.assertEqual(extract_vk_access_token(url), "user-token")

    def test_vk_token_with_label(self) -> None:
        self.assertEqual(extract_vk_access_token("token=vk1.a.value"), "vk1.a.value")

    def test_web_credential_view_does_not_expose_saved_credentials(self) -> None:
        stored = SecretStore(Path("missing-secrets.json")).load()
        stored["vk"]["access_token"] = "private-vk-token"
        stored["vk"]["group_id"] = "123"

        view = _credential_view(stored)

        token = next(field for field in view["vk"] if field["key"] == "access_token")
        group = next(field for field in view["vk"] if field["key"] == "group_id")
        self.assertTrue(token["saved"])
        self.assertEqual(token["value"], "")
        self.assertTrue(group["saved"])
        self.assertEqual(group["value"], "")

    def test_web_settings_save_entered_personal_token(self) -> None:
        stored = SecretStore(Path("missing-secrets.json")).load()

        _update_credentials(
            stored,
            {
                "vk_access_token": (
                    "https://oauth.vk.com/blank.html#access_token=personal-token&expires_in=0"
                )
            },
        )

        self.assertEqual(stored["vk"]["access_token"], "personal-token")

    def test_direct_network_ignores_environment(self) -> None:
        network = resolve_network("direct")
        self.assertIsNone(network.proxy)
        self.assertFalse(network.trust_env)


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_auth_and_text_request(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/me":
                return httpx.Response(
                    200, json={"user_id": 10, "first_name": "Bot", "is_bot": True}
                )
            if request.url.path.endswith("/members/me"):
                return httpx.Response(200, json={"user_id": 10})
            return httpx.Response(200, json={"message": {"body": {"text": "Тест"}}})

        publisher = MaxPublisher("token", "123")
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            account = await publisher.authenticate()
            result = await publisher.publish(MaxPostData(text="Тест"))
            self.assertEqual(account["id"], 10)
            self.assertIn("message", result)
            self.assertEqual(calls, ["/me", "/chats/123/members/me", "/messages"])
        finally:
            await publisher.aclose()

    async def test_telegram_text_request(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        publisher = TelegramPublisher("token", "@channel")
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await publisher.publish(TelegramPostData(text="<тест>"))
            self.assertIn("message_id", json.dumps(result))
            self.assertIn("parse_mode=HTML", captured["body"])
            self.assertIn("%26lt%3B", captured["body"])
        finally:
            await publisher.aclose()

    async def test_telegram_image_and_text_request(self) -> None:
        captured: dict[str, bytes | str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo.webp"
            Image.new("RGB", (640, 480), "yellow").save(source, "WEBP")
            with prepare_media_for_publish([source]) as prepared:
                publisher = TelegramPublisher("token", "@channel")
                await publisher._client.aclose()
                publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                try:
                    result = await publisher.publish(
                        TelegramPostData(text="Текст с картинкой", media=prepared)
                    )
                    self.assertIn("message_id", json.dumps(result))
                    self.assertIn("multipart/form-data", str(captured["content_type"]))
                    body = bytes(captured["body"])
                    self.assertIn(b'image/jpeg', body)
                    self.assertIn("Текст с картинкой".encode(), body)
                finally:
                    await publisher.aclose()

    async def test_vk_text_request(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("users.get"):
                return httpx.Response(200, json={"response": [{"id": 42}]})
            if request.url.path.endswith("account.getAppPermissions"):
                return httpx.Response(200, json={"response": 8196})
            return httpx.Response(200, json={"response": {"post_id": 7}})

        publisher = VKPublisher("token")
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await publisher.publish(VKPostData(text="Тест"))
            self.assertEqual(result["post_id"], 7)
            self.assertEqual(
                calls,
                ["/method/users.get", "/method/account.getAppPermissions", "/method/wall.post"],
            )
        finally:
            await publisher.aclose()

    async def test_vk_retries_when_upload_server_does_not_accept_photo(self) -> None:
        calls: list[str] = []
        upload_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal upload_count
            calls.append(request.url.path)
            if request.url.path.endswith("users.get"):
                return httpx.Response(200, json={"response": [{"id": 42}]})
            if request.url.path.endswith("account.getAppPermissions"):
                return httpx.Response(200, json={"response": 8196})
            if request.url.path.endswith("photos.getWallUploadServer"):
                return httpx.Response(200, json={"response": {"upload_url": "https://upload.vk.test/photo"}})
            if request.url.host == "upload.vk.test":
                upload_count += 1
                photo = "[]" if upload_count == 1 else '[{"sizes": []}]'
                return httpx.Response(
                    200, json={"server": 1, "photo": photo, "hash": "upload-hash"}
                )
            if request.url.path.endswith("photos.saveWallPhoto"):
                return httpx.Response(
                    200, json={"response": [{"owner_id": 42, "id": 99}]}
                )
            return httpx.Response(200, json={"response": {"post_id": 7}})

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "photo.jpg"
            Image.new("RGB", (640, 480), "yellow").save(image, "JPEG")
            publisher = VKPublisher("token")
            await publisher._client.aclose()
            publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                result = await publisher.publish(VKPostData(text="Тест", media=[image]))
                self.assertEqual(result["post_id"], 7)
                self.assertEqual(upload_count, 2)
                self.assertEqual(calls.count("/method/photos.getWallUploadServer"), 2)
                self.assertEqual(calls.count("/method/photos.saveWallPhoto"), 1)
            finally:
                await publisher.aclose()

    async def test_instagram_auth_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "123", "username": "business"})

        publisher = InstagramPublisher("token", "123")
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            account = await publisher.authenticate()
            self.assertEqual(account["username"], "business")
        finally:
            await publisher.aclose()


if __name__ == "__main__":
    unittest.main()
