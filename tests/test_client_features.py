import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import Api, ApiError


class MemoryStorage:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


def mock_client(response):
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


class ClientFeatureTests(unittest.TestCase):
    def setUp(self):
        self.storage = MemoryStorage()
        self.api = Api(self.storage)
        self.api.base = ""

    def test_local_draft_can_be_updated_and_deleted(self):
        async def run():
            created = await self.api._create_post({
                "title": "Draft title",
                "text": "Draft body",
                "mode": "draft",
            })
            edited = await self.api._update_draft(created["id"], {
                "title": "Edited title",
                "text": "Edited body",
            })
            posts = await self.api.get("/posts")
            self.assertEqual(edited["status"], "draft")
            self.assertEqual(posts[0]["title"], "Edited title")
            self.assertEqual(posts[0]["text"], "Edited body")
            await self.api.delete(f"/posts/{created['id']}")
            self.assertEqual(await self.api.get("/posts"), [])

        asyncio.run(run())

    def test_published_local_post_cannot_be_deleted_as_a_draft(self):
        async def run():
            await self.api._save("posts", [{
                "id": 1, "status": "published", "title": "Published",
                "text": "Preserved",
            }])
            with self.assertRaisesRegex(ApiError, "فقط پیش‌نویس"):
                await self.api.delete("/posts/1")
            self.assertEqual((await self.api.get("/posts"))[0]["text"],
                             "Preserved")

        asyncio.run(run())

    def test_local_publish_forwards_supported_message_options(self):
        async def run():
            self.storage.values["token"] = "test-token"
            await self.api._save("channels", [{
                "id": 1, "title": "test", "chat_id": "test-channel",
            }])
            response = SimpleNamespace(
                status_code=200,
                json=lambda: {"ok": True, "result": {"message_id": 1}},
            )
            client = mock_client(response)
            with (
                patch("api.config.EITAA_TOKEN", ""),
                patch("api.httpx.AsyncClient", return_value=client),
            ):
                result = await self.api._create_post({
                    "title": "title",
                    "text": "body",
                    "signature": "footer",
                    "channel_ids": [1],
                    "mode": "now",
                    "view_to_delete": 50,
                    "reply_to_message_id": 7,
                })
            self.assertEqual(result["status"], "published")
            _, kwargs = client.post.await_args
            self.assertEqual(
                kwargs["data"],
                {
                    "chat_id": "test-channel",
                    "text": "body\n\nfooter",
                    "title": "title",
                    "view_to_delete": 50,
                    "reply_to_message_id": 7,
                },
            )

        asyncio.run(run())

    def test_local_publish_does_not_claim_success_without_ok_flag(self):
        async def run():
            self.storage.values["token"] = "test-token"
            await self.api._save("channels", [{
                "id": 1, "title": "test", "chat_id": "test-channel",
            }])
            response = SimpleNamespace(
                status_code=200,
                json=lambda: {"result": {"message_id": 1}},
            )
            client = mock_client(response)
            with (
                patch("api.config.EITAA_TOKEN", ""),
                patch("api.httpx.AsyncClient", return_value=client),
            ):
                result = await self.api._create_post({
                    "text": "body", "channel_ids": [1], "mode": "now",
                })
            self.assertEqual(result["status"], "failed")
            posts = await self.api.get("/posts")
            self.assertEqual(posts[0]["deliveries"][0]["status"], "failed")

        asyncio.run(run())

    def test_ai_settings_store_key_in_provider_specific_slot(self):
        async def run():
            await self.api.save_ai_settings(
                provider="anthropic",
                model="claude-test-model",
                key="test-key",
            )
            settings = await self.api.ai_settings()
            self.assertEqual(settings["provider"], "anthropic")
            self.assertTrue(settings["has_key"])
            self.assertEqual(self.storage.values["ai_key_anthropic"], "test-key")
            self.assertNotIn("ai_key_openai", self.storage.values)

        asyncio.run(run())

    def test_ai_settings_does_not_replace_an_existing_key(self):
        async def run():
            self.storage.values["ai_key_openai"] = "existing-test-key"
            with self.assertRaisesRegex(ApiError, "قابل جایگزینی نیست"):
                await self.api.save_ai_settings(
                    provider="openai",
                    model="gpt-test-model",
                    key="replacement-test-key",
                )
            self.assertEqual(
                self.storage.values["ai_key_openai"], "existing-test-key"
            )
            self.assertNotIn("ai_provider", self.storage.values)

        asyncio.run(run())

    def test_ai_settings_preserves_configured_groq_key(self):
        async def run():
            with patch("api.config.GROQ_API_KEY", "configured-test-key"):
                with self.assertRaisesRegex(ApiError, "قابل جایگزینی نیست"):
                    await self.api.save_ai_settings(
                        provider="groq",
                        model="test-model",
                        key="replacement-test-key",
                    )
            self.assertNotIn("groq_key", self.storage.values)
            self.assertNotIn("ai_provider", self.storage.values)

        asyncio.run(run())

    def test_login_keeps_session_token_in_memory(self):
        async def run():
            self.api.base = "https://backend.example"
            self.api._http = AsyncMock(return_value={
                "token": "session-test-token",
                "user": {"username": "owner"},
            })
            user = await self.api.login("owner", "test-password")
            self.assertEqual(user["username"], "owner")
            self.assertEqual(self.api.token, "session-test-token")
            self.assertEqual(self.storage.values, {})

        asyncio.run(run())

    def test_local_connection_stores_new_token_only_after_api_verifies_it(self):
        async def run():
            response = SimpleNamespace(
                status_code=200,
                json=lambda: {"ok": True, "result": {"username": "test-bot"}},
            )
            client = mock_client(response)
            with (
                patch("api.config.EITAA_TOKEN", ""),
                patch("api.httpx.AsyncClient", return_value=client),
            ):
                connection = await self.api.post("/connections", {
                    "name": "test",
                    "token": "new-test-token",
                })
            self.assertEqual(connection["bot"], "test-bot")
            self.assertEqual(self.storage.values["token"], "new-test-token")

        asyncio.run(run())

    def test_local_connection_does_not_replace_configured_token(self):
        async def run():
            self.storage.values["token"] = "existing-test-token"
            with patch("api.config.EITAA_TOKEN", ""):
                with self.assertRaisesRegex(ApiError, "قابل جایگزینی"):
                    await self.api.post("/connections", {
                        "token": "replacement-test-token",
                    })
            self.assertEqual(self.storage.values["token"], "existing-test-token")

        asyncio.run(run())

    def test_local_channel_creation_requires_verified_eitaa_token(self):
        async def run():
            with patch("api.config.EITAA_TOKEN", ""):
                with self.assertRaisesRegex(ApiError, "اتصال ایتایار"):
                    await self.api.post("/channels", {
                        "value": "channel-name",
                    })

        asyncio.run(run())

    def test_gemini_uses_openai_compatible_endpoint_and_provider_key(self):
        async def run():
            self.storage.values.update({
                "ai_provider": "gemini",
                "ai_model": "gemini-test-model",
                "ai_key_gemini": "test-key",
            })
            response = SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{"message": {"content": "خروجی"}}]},
            )
            client = mock_client(response)
            with patch("api.httpx.AsyncClient", return_value=client):
                result = await self.api._ai({
                    "action": "proofread", "text": "متن آزمایشی",
                })
            self.assertEqual(result["text"], "خروجی")
            args, kwargs = client.post.await_args
            self.assertEqual(
                args[0],
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            )
            self.assertEqual(kwargs["json"]["model"], "gemini-test-model")
            self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))

        asyncio.run(run())

    def test_anthropic_response_is_parsed(self):
        async def run():
            self.storage.values.update({
                "ai_provider": "anthropic",
                "ai_model": "claude-test-model",
                "ai_key_anthropic": "test-key",
            })
            response = SimpleNamespace(
                status_code=200,
                json=lambda: {"content": [
                    {"type": "text", "text": "پاسخ"},
                ]},
            )
            client = mock_client(response)
            with patch("api.httpx.AsyncClient", return_value=client):
                result = await self.api._ai({
                    "action": "proofread", "text": "متن آزمایشی",
                })
            self.assertEqual(result["text"], "پاسخ")
            args, kwargs = client.post.await_args
            self.assertEqual(args[0], "https://api.anthropic.com/v1/messages")
            self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
