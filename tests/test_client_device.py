import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import Api, ApiError
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

    def test_auth_state_does_not_claim_local_user_exists(self):
        with self.assertRaisesRegex(ApiError, "احراز هویت به اتصال سرور نیاز دارد"):
            asyncio.run(self.api._device("GET", "/auth/state", None))

    def test_auth_profile_does_not_return_mock_user(self):
        with self.assertRaisesRegex(ApiError, "احراز هویت به اتصال سرور نیاز دارد"):
            asyncio.run(self.api._device("GET", "/auth/me", None))

    def test_offline_scheduling_saves_a_valid_future_post(self):
        async def run():
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

    def test_schedule_timestamp_round_trips_local_time_as_utc(self):
        local = datetime(2026, 10, 12, 9, 30)

        utc_iso = to_utc_iso(local)

        self.assertNotIn("+", utc_iso)
        self.assertEqual(to_local(utc_iso), local)


if __name__ == "__main__":
    unittest.main()
