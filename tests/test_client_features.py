import asyncio
import datetime as dt
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import Api, ApiError, normalize_channel


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

    def test_channel_normalization_rejects_malformed_identifiers(self):
        self.assertEqual(
            normalize_channel("https://eitaa.com/sample_channel"),
            "sample_channel",
        )
        self.assertEqual(normalize_channel("@sample_channel"), "sample_channel")
        self.assertEqual(normalize_channel("-10012345"), "-10012345")
        self.assertEqual(normalize_channel("../invalid"), "")
        self.assertEqual(normalize_channel(""), "")

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

    def test_local_schedule_is_saved_with_validated_utc_time(self):
        async def run():
            await self.api._save("channels", [{
                "id": 1, "title": "test", "chat_id": "test-channel",
            }])
            scheduled_at = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
            ).isoformat()
            result = await self.api._create_post({
                "text": "scheduled body",
                "channel_ids": [1],
                "mode": "schedule",
                "scheduled_at": scheduled_at,
            })
            posts = await self.api.get("/posts")
            self.assertEqual(result["status"], "scheduled")
            self.assertEqual(posts[0]["status"], "scheduled")
            self.assertEqual(posts[0]["deliveries"][0]["status"], "scheduled")
            self.assertIsNotNone(posts[0]["scheduled_at"])

        asyncio.run(run())

    def test_local_scheduler_publishes_due_posts_once(self):
        async def run():
            await self.api._save("posts", [{
                "id": 4,
                "title": "due post",
                "text": "body",
                "status": "scheduled",
                "scheduled_at": "2000-01-01T00:00:00",
                "deliveries": [{"channel_id": 1, "status": "scheduled"}],
            }])

            async def publish(post):
                post["status"] = "published"
                post["deliveries"][0]["status"] = "published"

            self.api._publish = AsyncMock(side_effect=publish)
            self.assertEqual(await self.api.process_due_scheduled(), 1)
            self.assertEqual(await self.api.process_due_scheduled(), 0)
            posts = await self.api.get("/posts")
            self.assertEqual(posts[0]["status"], "published")
            self.api._publish.assert_awaited_once()

        asyncio.run(run())

    def test_local_scheduler_records_unexpected_publish_failure(self):
        async def run():
            await self.api._save("posts", [{
                "id": 5,
                "title": "due post",
                "text": "body",
                "status": "scheduled",
                "scheduled_at": "2000-01-01T00:00:00",
                "deliveries": [{"channel_id": 1, "status": "scheduled"}],
            }])
            self.api._publish = AsyncMock(
                side_effect=RuntimeError("simulated unexpected failure")
            )

            self.assertEqual(await self.api.process_due_scheduled(), 1)
            posts = await self.api.get("/posts")
            self.assertEqual(posts[0]["status"], "failed")
            self.assertEqual(posts[0]["deliveries"][0]["status"], "failed")
            self.assertIn("وضعیت کانال", posts[0]["deliveries"][0]["error"])
            self.assertEqual(await self.api.process_due_scheduled(), 0)

        asyncio.run(run())

    def test_profile_dialog_dict_persists_display_name(self):
        async def run():
            saved = await self.api.save_profile({
                "display_name": "Test User",
                "username": "test_user",
            })
            loaded = await self.api.get_profile()
            self.assertEqual(saved["display_name"], "Test User")
            self.assertEqual(loaded["display_name"], "Test User")
            self.assertEqual(loaded["name"], "Test User")

        asyncio.run(run())

    def test_local_history_filters_and_exports_csv_and_json(self):
        async def run():
            await self.api._save("channels", [{
                "id": 2, "title": "کانال آزمایشی", "chat_id": "test",
            }])
            await self.api._save("posts", [{
                "id": 8,
                "title": "برنامه آزمایشی",
                "text": "متن منتشرشده",
                "status": "published",
                "created_at": "2026-01-01T10:00:00",
                "deliveries": [{
                    "channel_id": 2,
                    "status": "published",
                    "remote_message_id": 42,
                    "done_at": "2026-01-01T10:01:00",
                }],
            }])
            history = await self.api.get("/history", params={
                "q": "آزمایشی",
                "channel_id": 2,
                "status": "published",
            })
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["deliveries"][0]["remote_message_id"], 42)

            json_name, json_data = await self.api.export_history("json")
            csv_name, csv_data = await self.api.export_history("csv")
            self.assertTrue(json_name.endswith(".json"))
            self.assertTrue(csv_name.endswith(".csv"))
            self.assertIn("متن منتشرشده", json_data.decode("utf-8"))
            self.assertIn("42", csv_data.decode("utf-8-sig"))

            self.assertEqual(await self.api.get("/history", params={
                "channel_id": 3,
            }), [])

        asyncio.run(run())

    def test_local_draft_edit_keeps_previous_revision(self):
        async def run():
            created = await self.api._create_post({
                "title": "Before",
                "text": "Original",
                "mode": "draft",
            })
            await self.api._update_draft(created["id"], {
                "title": "After",
                "text": "Corrected",
            })
            post = (await self.api.get("/posts"))[0]
            self.assertEqual(post["title"], "After")
            self.assertEqual(post["revisions"][0]["title"], "Before")
            self.assertEqual(post["revisions"][0]["text"], "Original")

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
                    "viewCountForDelete": 50,
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
            posts = await self.api.get("/posts")
            self.assertEqual(
                posts[0]["deliveries"][0]["remote_message_id"], 1
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

    def test_latest_release_exposes_only_stable_github_apk_links(self):
        async def run():
            payload = {
                "draft": False,
                "prerelease": False,
                "tag_name": "v2.1.0",
                "name": "Release 2.1.0",
                "body": "Release notes",
                "html_url": (
                    "https://github.com/Abolfazlsoltan/"
                    "eitaa_app/releases/tag/v2.1.0"
                ),
                "assets": [{
                    "name": "manager.apk",
                    "browser_download_url": (
                        "https://github.com/Abolfazlsoltan/"
                        "eitaa_app/releases/download/v2.1.0/manager.apk"
                    ),
                }],
            }
            response = SimpleNamespace(
                status_code=200, json=lambda: payload,
            )
            client = mock_client(response)
            client.get = AsyncMock(return_value=response)
            with patch("api.httpx.AsyncClient", return_value=client):
                release = await self.api.latest_release()
            self.assertEqual(release["tag"], "v2.1.0")
            self.assertTrue(release["apk_url"].endswith("manager.apk"))

            payload["prerelease"] = True
            with patch("api.httpx.AsyncClient", return_value=client):
                with self.assertRaisesRegex(ApiError, "نسخه پایدار"):
                    await self.api.latest_release()

            payload["prerelease"] = False
            payload["html_url"] = "https://example.com/fake-release"
            with patch("api.httpx.AsyncClient", return_value=client):
                with self.assertRaisesRegex(ApiError, "پیوند انتشار"):
                    await self.api.latest_release()

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

    def test_groq_random_fallback_uses_requested_models_and_records_success(self):
        async def run():
            self.storage.values.update({
                "ai_provider": "groq",
                "groq_key": "test-key",
            })
            responses = [
                SimpleNamespace(status_code=404, json=lambda: {}),
                SimpleNamespace(
                    status_code=200,
                    json=lambda: {"choices": [{
                        "message": {"content": "پاسخ مدل جایگزین"},
                    }]},
                ),
            ]
            client = MagicMock()
            client.post = AsyncMock(side_effect=responses)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            with (
                patch("api.random.shuffle", side_effect=lambda items: None),
                patch("api.httpx.AsyncClient", return_value=client),
            ):
                result = await self.api._ai({
                    "action": "create", "text": "یک ایده",
                })

            self.assertEqual(result["text"], "پاسخ مدل جایگزین")
            models = [
                call.kwargs["json"]["model"]
                for call in client.post.await_args_list
            ]
            self.assertEqual(models, [
                "llama-3.1-8b-instant",
                "allam-2-7b",
            ])
            self.assertEqual(len(await self.api._load("ai_usage", [])), 1)

        asyncio.run(run())

    def test_groq_all_failures_do_not_record_successful_usage(self):
        async def run():
            self.storage.values.update({
                "ai_provider": "groq",
                "groq_key": "test-key",
            })
            client = MagicMock()
            client.post = AsyncMock(side_effect=[
                SimpleNamespace(status_code=404, json=lambda: {})
                for _ in range(3)
            ])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            with (
                patch("api.random.shuffle", side_effect=lambda items: None),
                patch("api.httpx.AsyncClient", return_value=client),
            ):
                with self.assertRaisesRegex(ApiError, "هیچ‌یک از مدل‌ها"):
                    await self.api._ai({
                        "action": "create", "text": "یک ایده",
                    })

            self.assertEqual(client.post.await_count, 3)
            self.assertEqual(await self.api._load("ai_usage", []), [])

        asyncio.run(run())

    def test_groq_settings_hide_legacy_model_selection(self):
        async def run():
            self.storage.values.update({
                "ai_provider": "groq",
                "ai_model": "retired-model",
            })
            settings = await self.api.ai_settings()
            self.assertEqual(settings["model"], "llama-3.1-8b-instant")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
