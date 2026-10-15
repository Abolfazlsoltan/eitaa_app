import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from ai import AIServiceError, AnonymousAI
from api import Api, ApiError


def response(status, payload):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def mock_http_client(posts=(), gets=()):
    client = MagicMock()
    client.post = AsyncMock(side_effect=list(posts))
    client.get = AsyncMock(side_effect=list(gets))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


class AnonymousAIServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pollinations_success_uses_keyless_chat_api(self):
        client = mock_http_client(posts=[
            response(200, {"choices": [{
                "message": {"content": "پاسخ"},
            }]}),
        ])
        with (
            patch("ai.random.shuffle", side_effect=lambda items: None),
            patch("ai.httpx.AsyncClient", return_value=client),
        ):
            result = await AnonymousAI().complete("system message", "prompt")

        self.assertEqual(result, "پاسخ")
        args, kwargs = client.post.await_args
        self.assertEqual(
            args[0],
            "https://text.pollinations.ai/openai/chat/completions",
        )
        self.assertEqual(kwargs["json"]["model"], "gpt-oss-20b")
        self.assertEqual(kwargs["json"]["messages"][0]["content"], "system message")
        self.assertNotIn("Authorization", kwargs["headers"])
        client.get.assert_not_awaited()

    async def test_pollinations_rate_limit_falls_back_to_live_unclose_model(self):
        client = mock_http_client(
            posts=[
                response(402, {"error": "rate limited"}),
                response(200, {"choices": [{
                    "message": {"content": "پاسخ جایگزین"},
                }]}),
            ],
            gets=[response(200, {"data": [{"id": "current-chat-model"}]})],
        )
        with (
            patch("ai.random.shuffle", side_effect=lambda items: None),
            patch("ai.httpx.AsyncClient", return_value=client),
        ):
            result = await AnonymousAI().complete("system", "prompt")

        self.assertEqual(result, "پاسخ جایگزین")
        self.assertEqual(client.get.await_args.args[0],
                         "https://hermes.ai.unturf.com/v1/models")
        self.assertEqual(
            [call.kwargs["json"]["model"] for call in client.post.await_args_list],
            ["gpt-oss-20b", "current-chat-model"],
        )
        self.assertNotIn(
            "Authorization",
            client.post.await_args_list[1].kwargs["headers"],
        )

    async def test_malformed_and_empty_results_fail_over_without_leaking_details(self):
        client = mock_http_client(
            posts=[
                response(200, {"choices": []}),
                response(200, {"choices": [{
                    "message": {"content": "  "},
                }]}),
            ],
            gets=[response(200, {"data": [{"id": "model"}]})],
        )
        with (
            patch("ai.random.shuffle", side_effect=lambda items: None),
            patch("ai.httpx.AsyncClient", return_value=client),
        ):
            with self.assertRaises(AIServiceError) as raised:
                await AnonymousAI().complete("system", "prompt")

        self.assertIn("دستیار هوشمند", str(raised.exception))
        self.assertNotIn("pollinations", str(raised.exception).lower())
        self.assertNotIn("unturf", str(raised.exception).lower())
        self.assertEqual(client.post.await_count, 2)

    async def test_timeout_on_first_provider_falls_back(self):
        client = mock_http_client(
            posts=[
                httpx.ReadTimeout("timed out"),
                response(200, {"choices": [{
                    "message": {"content": "recovered"},
                }]}),
            ],
            gets=[response(200, {"data": [{"id": "current-model"}]})],
        )
        with (
            patch("ai.random.shuffle", side_effect=lambda items: None),
            patch("ai.httpx.AsyncClient", return_value=client),
        ):
            result = await AnonymousAI().complete("system", "prompt")

        self.assertEqual(result, "recovered")
        self.assertEqual(client.post.await_count, 2)


class ApiAIDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_ai_bypasses_server_and_keeps_legacy_settings_untouched(self):
        class MemoryStorage:
            def __init__(self):
                self.values = {
                    "ai_provider": "groq",
                    "ai_model": "saved-model",
                    "groq_key": "saved-key",
                    "ai_base_url": "https://saved.example/v1",
                }

            async def get(self, name):
                return self.values.get(name)

            async def set(self, name, value):
                self.values[name] = value

        storage = MemoryStorage()
        api = Api(storage)
        api.base = "https://configured-server.example"
        api._ai_client.complete = AsyncMock(return_value="generated")
        api._dispatch = AsyncMock(
            side_effect=AssertionError("AI must not use the configured server")
        )

        result = await api.ai(
            "rewrite",
            "متن",
            language="fa",
            tone="formal",
            length="short",
        )

        self.assertEqual(result, "generated")
        api._dispatch.assert_not_awaited()
        self.assertIn("رسمی", api._ai_client.complete.await_args.args[1])
        self.assertEqual(storage.values["ai_provider"], "groq")
        self.assertEqual(storage.values["ai_model"], "saved-model")
        self.assertEqual(storage.values["groq_key"], "saved-key")
        self.assertEqual(
            storage.values["ai_base_url"], "https://saved.example/v1"
        )
        usage = await api._load("ai_usage", [])
        self.assertEqual(len(usage), 1)


class ApiAIInputValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = Api(object())
        self.api._ai_client.complete = AsyncMock(return_value="unused")

    async def test_rejects_unknown_action_before_calling_provider(self):
        with self.assertRaises(ApiError):
            await self.api.ai("not-an-action", "متن")
        self.api._ai_client.complete.assert_not_awaited()

    async def test_rejects_empty_request_before_calling_provider(self):
        with self.assertRaises(ApiError):
            await self.api.ai("rewrite", "  ", prompt="  ")
        self.api._ai_client.complete.assert_not_awaited()

    async def test_rejects_oversized_request_before_calling_provider(self):
        with self.assertRaises(ApiError):
            await self.api.ai("rewrite", "x" * 20_001)
        self.api._ai_client.complete.assert_not_awaited()

    async def test_rejects_unsupported_tone_before_calling_provider(self):
        with self.assertRaises(ApiError):
            await self.api.ai("rewrite", "متن", tone="unknown")
        self.api._ai_client.complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
