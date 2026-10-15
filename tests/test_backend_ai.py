import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.ai import AIError, GroqProvider


def mock_client(responses):
    client = MagicMock()
    client.post = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


class GroqFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_randomized_model_order_falls_back_after_failure(self):
        responses = [
            SimpleNamespace(status_code=404, json=lambda: {}),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{
                    "message": {"content": "fallback response"},
                }]},
            ),
        ]
        client = mock_client(responses)
        with (
            patch.dict(os.environ, {"AI_API_KEY": "test-key"}),
            patch("backend.ai.random.shuffle", side_effect=lambda items: None),
            patch("backend.ai.httpx.AsyncClient", return_value=client),
        ):
            provider = GroqProvider()
            result = await provider.complete("system", "user")

        self.assertEqual(result, "fallback response")
        self.assertEqual(
            [call.kwargs["json"]["model"] for call in client.post.await_args_list],
            ["llama-3.1-8b-instant", "allam-2-7b"],
        )

    async def test_all_model_failures_return_explicit_error(self):
        client = mock_client([
            SimpleNamespace(status_code=404, json=lambda: {})
            for _ in GroqProvider.models
        ])
        with (
            patch.dict(os.environ, {"AI_API_KEY": "test-key"}),
            patch("backend.ai.random.shuffle", side_effect=lambda items: None),
            patch("backend.ai.httpx.AsyncClient", return_value=client),
        ):
            provider = GroqProvider()
            with self.assertRaisesRegex(AIError, "هیچ‌یک از مدل‌ها"):
                await provider.complete("system", "user")

        self.assertEqual(client.post.await_count, 3)

    async def test_empty_model_response_triggers_fallback(self):
        client = mock_client([
            SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{
                    "message": {"content": "  "},
                }]},
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{
                    "message": {"content": "valid response"},
                }]},
            ),
        ])
        with (
            patch.dict(os.environ, {"AI_API_KEY": "test-key"}),
            patch("backend.ai.random.shuffle", side_effect=lambda items: None),
            patch("backend.ai.httpx.AsyncClient", return_value=client),
        ):
            provider = GroqProvider()
            result = await provider.complete("system", "user")

        self.assertEqual(result, "valid response")
        self.assertEqual(client.post.await_count, 2)


class AIRequestHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_insecure_remote_endpoint_before_network(self):
        with (
            patch.dict(os.environ, {"AI_API_KEY": "test-key"}),
            patch("backend.ai.GroqProvider.endpoint", "http://example.com/chat"),
            patch("backend.ai.httpx.AsyncClient") as client,
        ):
            provider = GroqProvider()
            with self.assertRaisesRegex(AIError, "HTTPS"):
                await provider.complete("system", "user")
            client.assert_not_called()

    async def test_rejects_empty_and_oversized_prompts(self):
        with patch.dict(os.environ, {"AI_API_KEY": "test-key"}):
            provider = GroqProvider()
            with self.assertRaises(AIError):
                await provider.complete("system", " ")
            with self.assertRaises(AIError):
                await provider.complete("system", "x" * 24_001)


if __name__ == "__main__":
    unittest.main()
