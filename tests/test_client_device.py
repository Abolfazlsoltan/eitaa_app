import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api import Api, ApiError
from fa import to_local, to_utc_iso


class ClientDeviceModeTests(unittest.TestCase):
    def setUp(self):
        self.api = Api.__new__(Api)

    def test_auth_state_does_not_claim_local_user_exists(self):
        with self.assertRaisesRegex(ApiError, "احراز هویت به اتصال سرور نیاز دارد"):
            asyncio.run(self.api._device("GET", "/auth/state", None))

    def test_auth_profile_does_not_return_mock_user(self):
        with self.assertRaisesRegex(ApiError, "احراز هویت به اتصال سرور نیاز دارد"):
            asyncio.run(self.api._device("GET", "/auth/me", None))

    def test_offline_scheduling_explains_server_requirement(self):
        with self.assertRaisesRegex(ApiError, "زمان‌بندی انتشار به اتصال سرور نیاز دارید"):
            asyncio.run(self.api._device(
                "POST",
                "/posts",
                {"text": "keep this draft", "mode": "schedule"},
            ))

    def test_schedule_timestamp_round_trips_local_time_as_utc(self):
        local = datetime(2026, 10, 12, 9, 30)

        utc_iso = to_utc_iso(local)

        self.assertNotIn("+", utc_iso)
        self.assertEqual(to_local(utc_iso), local)


if __name__ == "__main__":
    unittest.main()
