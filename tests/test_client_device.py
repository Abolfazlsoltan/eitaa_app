import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import Api, ApiError
from public_history import PublicHistoryError
from fa import to_local, to_utc_iso


class MemoryStorage:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


class ClientDeviceModeTests(unittest.TestCase):
    def setUp(self):
        self.api = Api(MemoryStorage())

    def test_public_channel_history_is_archived_and_survives_fetch_failure(self):
        async def run():
            await self.api._save("channels", [{
                "id": 7, "title": "نمونه", "chat_id": "sample_channel"
            }])
            first = [{
                "id": "eitaa:sample_channel:123",
                "source_post_id": 123,
                "source_url": "https://eitaa.com/sample_channel/123",
                "source": "eitaa_public",
                "is_external": True,
                "text": "پست قدیمی",
                "created_at": "2026-10-15T10:00:00+00:00",
                "deliveries": [],
            }]
            with patch("api.fetch_public_channel_history", new=AsyncMock(return_value=first)):
                fresh = await self.api._device(
                    "GET", "/history/public", None, params={"channel_id": 7}
                )
            self.assertEqual(fresh[0]["text"], "پست قدیمی")

            with patch(
                "api.fetch_public_channel_history",
                new=AsyncMock(side_effect=PublicHistoryError("offline")),
            ):
                cached = await self.api._device(
                    "GET", "/history/public", None, params={"channel_id": 7}
                )
            self.assertEqual(cached[0]["source_post_id"], 123)
            self.assertTrue(cached[0]["is_stale"])

        asyncio.run(run())

    def test_auth_state_does_not_claim_local_user_exists(self):
        with self.assertRaisesRegex(ApiError, "احراز هویت به اتصال سرور نیاز دارد"):
            asyncio.run(self.api._device("GET", "/auth/state", None))

    def test_auth_profile_does_not_return_mock_user(self):
        with self.assertRaisesRegex(ApiError, "احراز هویت به اتصال سرور نیاز دارد"):
            asyncio.run(self.api._device("GET", "/auth/me", None))

    def test_offline_scheduling_saves_a_valid_future_post(self):
        async def run():
            await self.api.storage.set(
                "data:channels",
                json.dumps([{"id": 1, "title": "Test", "chat_id": "test"}]),
            )
            scheduled_at = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat()
            result = await self.api._device(
                "POST",
                "/posts",
                {
                    "text": "scheduled locally",
                    "mode": "schedule",
                    "channel_ids": [1],
                    "scheduled_at": scheduled_at,
                },
            )
            self.assertEqual(result["status"], "scheduled")
            posts = await self.api.get("/posts")
            self.assertEqual(posts[0]["status"], "scheduled")

        asyncio.run(run())

    def test_multi_channel_publish_persists_partial_delivery_results(self):
        class FakeResponse:
            status_code = 200

            def __init__(self, success):
                self.success = success

            def json(self):
                if self.success:
                    return {"ok": True, "result": {"message_id": 55}}
                return {"ok": False, "description": "channel unavailable"}

        class FakeClient:
            def __init__(self, storage, **kwargs):
                self.storage = storage
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, data):
                self.calls.append(data["chat_id"])
                saved = json.loads(await self.storage.get("data:posts"))
                delivery = next(
                    d for d in saved[0]["deliveries"]
                    if d["channel_id"] == (1 if data["chat_id"] == "good" else 2)
                )
                if delivery["status"] != "sending":
                    raise AssertionError("sending state was not persisted before request")
                return FakeResponse(data["chat_id"] == "good")

        async def run():
            storage = self.api.storage
            await storage.set("token", "test-token")
            await storage.set("data:channels", json.dumps([
                {"id": 1, "title": "Good", "chat_id": "good"},
                {"id": 2, "title": "Unavailable", "chat_id": "bad"},
            ]))
            fake_clients = []

            def make_client(**kwargs):
                client = FakeClient(storage, **kwargs)
                fake_clients.append(client)
                return client

            with patch("api.httpx.AsyncClient", side_effect=make_client):
                result = await self.api._device("POST", "/posts", {
                    "text": "hello",
                    "mode": "now",
                    "channel_ids": [1, 2],
                })

            self.assertEqual(result["status"], "partial")
            saved = json.loads(await storage.get("data:posts"))[0]
            self.assertEqual(saved["status"], "partial")
            self.assertEqual(
                [d["status"] for d in saved["deliveries"]],
                ["published", "failed"],
            )
            self.assertEqual(saved["deliveries"][0]["remote_message_id"], 55)
            self.assertIn("good", fake_clients[0].calls)
            self.assertIn("bad", fake_clients[0].calls)

        asyncio.run(run())

    def test_publisher_skips_channels_already_published(self):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {"message_id": 77}}

        class FakeClient:
            def __init__(self, **kwargs):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, data):
                self.calls.append(data["chat_id"])
                return FakeResponse()

        async def run():
            storage = self.api.storage
            await storage.set("token", "test-token")
            await storage.set("data:channels", json.dumps([
                {"id": 1, "title": "A", "chat_id": "already-sent"},
                {"id": 2, "title": "B", "chat_id": "not-sent-yet"},
            ]))
            post = {
                "id": 7, "title": "", "text": "hello", "signature": "",
                "silent": False, "pin": False, "deliveries": [
                    {"id": 7000, "channel_id": 1, "status": "published"},
                    {"id": 7001, "channel_id": 2, "status": "queued"},
                ],
            }
            await storage.set("data:posts", json.dumps([post]))
            clients = []

            def make_client(**kwargs):
                client = FakeClient(**kwargs)
                clients.append(client)
                return client

            with patch("api.httpx.AsyncClient", side_effect=make_client):
                await self.api._publish(post)

            self.assertEqual(clients[0].calls, ["not-sent-yet"])
            saved = json.loads(await storage.get("data:posts"))[0]
            self.assertEqual(saved["status"], "published")
            self.assertEqual(
                [d["status"] for d in saved["deliveries"]],
                ["published", "published"],
            )

        asyncio.run(run())

    def test_schedule_timestamp_round_trips_local_time_as_utc(self):
        local = datetime(2026, 10, 12, 9, 30)

        utc_iso = to_utc_iso(local)

        self.assertNotIn("+", utc_iso)
        self.assertEqual(to_local(utc_iso), local)


if __name__ == "__main__":
    unittest.main()
