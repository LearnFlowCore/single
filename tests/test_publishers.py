from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from api.base import AuthenticationError
from api.instagram_publisher import InstagramPublisher
from api.max_publisher import MaxPostData, MaxPublisher
from api.ok_publisher import OkPostData, OkPublisher
from api.telegram_publisher import TelegramPostData, TelegramPublisher
from api.vk_publisher import VKPostData, VKPublisher
from utils.auth import SecretStore, extract_vk_access_token
from utils.media import prepare_media_for_publish, validate_media
from utils.network import resolve_network
from web.app import app, _credential_view, _merge_browser_credentials, _update_credentials


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

        changed = _update_credentials(
            stored,
            {
                "vk_access_token": (
                    "https://oauth.vk.com/blank.html#access_token=personal-token&expires_in=0"
                )
            },
        )

        self.assertEqual(stored["vk"]["access_token"], "personal-token")
        self.assertIn(("vk", "access_token"), changed)

    def test_vk_oauth_callback_is_public_and_not_cached(self) -> None:
        with TestClient(app) as client:
            response = client.get("/vk/oauth/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("autoposter-vk-oauth", response.text)
        self.assertIn("http://158.160.237.113", response.text)
        self.assertIn("http://testserver", response.text)

    def test_vk_token_reset_requires_login(self) -> None:
        with TestClient(app) as client:
            response = client.post("/settings/vk/reset", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_browser_credentials_cannot_override_vk_token_or_client_id(self) -> None:
        stored = SecretStore(Path("missing-secrets.json")).load()
        stored["vk"]["access_token"] = "server-token"
        stored["vk"]["client_id"] = "server-client-id"

        _merge_browser_credentials(
            stored,
            json.dumps(
                {
                    "vk": {
                        "access_token": "browser-token",
                        "client_id": "browser-client-id",
                        "group_id": "123",
                    }
                }
            ),
        )

        self.assertEqual(stored["vk"]["access_token"], "server-token")
        self.assertEqual(stored["vk"]["client_id"], "server-client-id")
        self.assertEqual(stored["vk"]["group_id"], "123")

    def test_browser_credentials_cannot_override_ok_secrets(self) -> None:
        stored = SecretStore(Path("missing-secrets.json")).load()
        stored["ok"]["access_token"] = "server-token"

        _merge_browser_credentials(
            stored,
            json.dumps(
                {
                    "ok": {
                        "access_token": "browser-token",
                        "session_secret_key": "browser-secret",
                        "group_id": "999",
                    }
                }
            ),
        )

        self.assertEqual(stored["ok"]["access_token"], "server-token")
        self.assertEqual(stored["ok"]["session_secret_key"], "")
        self.assertEqual(stored["ok"]["group_id"], "")

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

    async def test_ok_auth_and_group_text_request_are_signed(self) -> None:
        calls: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            values = {key: items[0] for key, items in parse_qs(request.content.decode()).items()}
            calls.append(values)
            signature = values.pop("sig")
            values.pop("access_token")
            source = "".join(f"{key}={value}" for key, value in sorted(values.items()))
            self.assertEqual(signature, hashlib.md5(f"{source}session-secret".encode()).hexdigest())
            if values["method"] == "users.getCurrentUser":
                return httpx.Response(200, json={"uid": "42", "name": "Тест"})
            return httpx.Response(200, json="987654321")

        publisher = OkPublisher(
            "app-id", "public-key", "app-secret", "access-token", "session-secret", "123"
        )
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            account = await publisher.authenticate()
            result = await publisher.publish(OkPostData(text="Проверка OK"))
            self.assertEqual(account["id"], "42")
            self.assertEqual(result["topic_id"], "987654321")
            self.assertEqual(result["url"], "https://ok.ru/group/123/topic/987654321")
            attachment = json.loads(calls[1]["attachment"])
            self.assertEqual(attachment["media"][0]["text"], "Проверка OK")
            self.assertEqual(calls[1]["type"], "GROUP_THEME")
            self.assertEqual(calls[1]["gid"], "123")
        finally:
            await publisher.aclose()

    async def test_ok_group_photo_upload(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "upload.okcdn.ru":
                self.assertIn(b'name="pic1"', request.content)
                return httpx.Response(200, json={"photos": {"photo-id": {"token": "photo-token"}}})
            values = {key: items[0] for key, items in parse_qs(request.content.decode()).items()}
            methods.append(values["method"])
            if values["method"] == "photosV2.getUploadUrl":
                return httpx.Response(
                    200,
                    json={"upload_url": "https://upload.okcdn.ru/photos", "photo_ids": ["photo-id"]},
                )
            attachment = json.loads(values["attachment"])
            self.assertEqual(attachment["media"][0]["list"][0]["id"], "photo-token")
            return httpx.Response(200, json="topic-id")

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "photo.jpg"
            Image.new("RGB", (640, 480), "orange").save(image, "JPEG")
            publisher = OkPublisher(
                "app-id", "public-key", "app-secret", "access-token", "session-secret", "123"
            )
            await publisher._client.aclose()
            publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                result = await publisher.publish(OkPostData(media=[str(image)]))
                self.assertEqual(result["topic_id"], "topic-id")
                self.assertEqual(methods, ["photosV2.getUploadUrl", "mediatopic.post"])
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

    async def test_telegram_media_group_uses_photo_type(self) -> None:
        captured: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"ok": True, "result": [{"message_id": 3}]})

        with tempfile.TemporaryDirectory() as directory:
            paths: list[str] = []
            for index in range(2):
                source = Path(directory) / f"photo{index}.jpg"
                Image.new("RGB", (320, 240), "yellow").save(source, "JPEG")
                paths.append(str(source))
            publisher = TelegramPublisher("token", "@channel")
            await publisher._client.aclose()
            publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await publisher.publish(TelegramPostData(text="Альбом", media=paths))
                self.assertIn(b'\"type\": \"photo\"', captured["body"])
                self.assertNotIn(b'\"type\": \"image\"', captured["body"])
            finally:
                await publisher.aclose()

    async def test_vk_text_request(self) -> None:
        calls: list[str] = []
        wall_body = b""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal wall_body
            calls.append(request.url.path)
            if request.url.path.endswith("users.get"):
                return httpx.Response(200, json={"response": [{"id": 42}]})
            if request.url.path.endswith("account.getAppPermissions"):
                return httpx.Response(200, json={"response": 8196})
            wall_body = request.content
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
            self.assertIn(b"owner_id=42", wall_body)
            self.assertIn(b"v=5.199", wall_body)
        finally:
            await publisher.aclose()

    async def test_vk_rejects_token_for_another_account(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response": [{"id": 42}]})

        publisher = VKPublisher("token", expected_user_id=200001271797)
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(AuthenticationError, "id200001271797"):
                await publisher.authenticate()
        finally:
            await publisher.aclose()

    async def test_vk_authentication_reports_api_reason(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 5,
                        "error_subcode": 1130,
                        "error_msg": "User authorization failed: access_token was given to another ip address.",
                    }
                },
            )

        publisher = VKPublisher("token")
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(AuthenticationError, "Запросы идут с сервера") as caught:
                await publisher.authenticate()
            self.assertEqual(getattr(caught.exception, "vk_code", None), 5)
            self.assertEqual(getattr(caught.exception, "vk_subcode", None), 1130)
        finally:
            await publisher.aclose()

    async def test_vk_retries_connection_reset_with_bounded_timeout(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ReadError("ECONNRESET", request=request)
            return httpx.Response(200, json={"response": [{"id": 42}]})

        publisher = VKPublisher("token")
        await publisher._client.aclose()
        publisher._client = httpx.AsyncClient(
            timeout=publisher._client.timeout,
            transport=httpx.MockTransport(handler),
        )
        try:
            with patch("api.base.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await publisher._vk_call("users.get", {}, "VK authentication")
            self.assertEqual(result["response"][0]["id"], 42)
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.await_count, 2)
            self.assertEqual(publisher._client.timeout.read, 10.0)
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
