import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet

_test_dir = tempfile.TemporaryDirectory(prefix="eitaa-manager-tests-")
os.environ["APP_SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir.name}/test.db"
os.environ["MEDIA_DIR"] = f"{_test_dir.name}/media"

from fastapi.testclient import TestClient

from backend.db import Channel, Connection, Session, fernet
from backend.main import app


class PostValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        response = cls.client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "test-password-123"},
        )
        if response.status_code != 200:
            raise RuntimeError("Could not initialize the isolated API test user.")
        cls.headers = {"Authorization": f"Bearer {response.json()['token']}"}
        with Session() as session:
            connection = Connection(
                name="test connection",
                token_enc=fernet.encrypt(b"test-only").decode(),
            )
            session.add(connection)
            session.flush()
            channel = Channel(
                connection_id=connection.id,
                title="test channel",
                chat_id="test-channel",
            )
            session.add(channel)
            session.commit()
            cls.channel_id = channel.id

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        _test_dir.cleanup()

    def test_rejects_unknown_post_mode(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"text": "draft", "mode": "publish"},
        )

        self.assertEqual(response.status_code, 422)

    def test_rejects_unknown_media_asset(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"text": "draft", "media_id": 999999},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "فایل رسانه‌ای پیدا نشد.")

    def test_rejects_missing_channel_before_queueing(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"text": "post", "mode": "now", "channel_ids": [999999]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "یکی از کانال‌های انتخاب‌شده پیدا نشد.")

    def test_rejects_duplicate_channels(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"text": "post", "mode": "now", "channel_ids": [999999, 999999]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "کانال تکراری انتخاب شده است.")

    def test_accepts_local_draft_without_channels(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"text": "editable draft"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "draft")

    def test_post_stores_supported_reply_and_view_delete_options(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "title": "message options",
                "text": "body",
                "channel_ids": [self.channel_id],
                "view_to_delete": 100,
                "reply_to_message_id": 42,
                "signature": "footer",
            },
        )
        self.assertEqual(response.status_code, 200)
        stored = next(
            post for post in self.client.get(
                "/api/posts", headers=self.headers
            ).json()
            if post["id"] == response.json()["id"]
        )
        self.assertEqual(stored["view_to_delete"], 100)
        self.assertEqual(stored["reply_to_message_id"], 42)
        self.assertEqual(stored["signature"], "footer")

    def test_rejects_nonpositive_view_threshold(self):
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"text": "body", "view_to_delete": 0},
        )
        self.assertEqual(response.status_code, 422)

    def test_accepts_scheduled_post_for_existing_channel(self):
        scheduled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        ).isoformat()
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "text": "scheduled post",
                "mode": "schedule",
                "scheduled_at": scheduled_at,
                "channel_ids": [self.channel_id],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "scheduled")

    def test_normalizes_timezone_aware_schedule_to_utc(self):
        offset = timezone(timedelta(hours=3, minutes=30))
        scheduled_at = datetime.now(offset) + timedelta(days=1)
        expected_utc = (
            scheduled_at.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat()
        )
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "text": "timezone-aware schedule",
                "mode": "schedule",
                "scheduled_at": scheduled_at.isoformat(),
                "channel_ids": [self.channel_id],
            },
        )

        self.assertEqual(response.status_code, 200)
        posts = self.client.get("/api/posts?status=scheduled", headers=self.headers)
        self.assertEqual(posts.status_code, 200)
        stored = next(
            post for post in posts.json()
            if post["text"] == "timezone-aware schedule"
        )
        self.assertEqual(stored["scheduled_at"], expected_utc)

    def test_rejects_past_schedule(self):
        scheduled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        ).isoformat()
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "text": "past schedule",
                "mode": "schedule",
                "scheduled_at": scheduled_at,
                "channel_ids": [self.channel_id],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "زمان انتشار باید در آینده باشد.")

    def test_rejects_schedule_beyond_thirty_days(self):
        scheduled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(days=31)
        ).isoformat()
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "text": "too far",
                "mode": "schedule",
                "scheduled_at": scheduled_at,
                "channel_ids": [self.channel_id],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"], "زمان‌بندی فقط تا ۳۰ روز آینده مجاز است."
        )

    def test_rejects_repeating_schedule_with_occurrence_after_thirty_days(self):
        scheduled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(days=25)
        ).isoformat()
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "text": "weekly repeat",
                "mode": "schedule",
                "scheduled_at": scheduled_at,
                "repeat": "weekly",
                "repeat_count": 2,
                "channel_ids": [self.channel_id],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "تمام نوبت‌های تکرار باید در ۳۰ روز آینده باشند.",
        )

    def test_creates_each_valid_repeating_schedule_occurrence(self):
        scheduled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(days=1)
        ).isoformat()
        response = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "text": "daily repeat",
                "mode": "schedule",
                "scheduled_at": scheduled_at,
                "repeat": "daily",
                "repeat_count": 3,
                "channel_ids": [self.channel_id],
            },
        )
        self.assertEqual(response.status_code, 200)
        scheduled = [
            post for post in self.client.get(
                "/api/posts", headers=self.headers
            ).json()
            if post["text"] == "daily repeat"
        ]
        self.assertEqual(len(scheduled), 3)
        self.assertTrue(all(post["status"] == "scheduled" for post in scheduled))
        self.assertEqual(
            len({post["scheduled_at"] for post in scheduled}), 3
        )

    def test_owner_can_edit_and_delete_draft(self):
        created = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"title": "draft", "text": "before"},
        )
        self.assertEqual(created.status_code, 200)
        post_id = created.json()["id"]

        edited = self.client.put(
            f"/api/posts/{post_id}",
            headers=self.headers,
            json={
                "title": "edited",
                "text": "after",
                "signature": "footer",
                "view_to_delete": 25,
                "reply_to_message_id": 8,
            },
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["status"], "draft")
        posts = self.client.get("/api/posts", headers=self.headers).json()
        stored = next(post for post in posts if post["id"] == post_id)
        self.assertEqual(stored["title"], "edited")
        self.assertEqual(stored["text"], "after")
        self.assertEqual(stored["signature"], "footer")
        self.assertEqual(stored["view_to_delete"], 25)
        self.assertEqual(stored["reply_to_message_id"], 8)

        deleted = self.client.delete(
            f"/api/posts/{post_id}", headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200)
        posts = self.client.get("/api/posts", headers=self.headers).json()
        self.assertFalse(any(post["id"] == post_id for post in posts))

    def test_draft_can_be_published_after_editing(self):
        created = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "title": "publish later",
                "text": "draft text",
                "channel_ids": [self.channel_id],
            },
        )
        self.assertEqual(created.status_code, 200)
        post_id = created.json()["id"]

        posts = self.client.get("/api/posts", headers=self.headers).json()
        draft = next(post for post in posts if post["id"] == post_id)
        self.assertEqual(draft["deliveries"][0]["channel_id"], self.channel_id)
        self.assertEqual(draft["deliveries"][0]["status"], "draft")

        published = self.client.put(
            f"/api/posts/{post_id}",
            headers=self.headers,
            json={
                "title": "publish later",
                "text": "edited text",
                "channel_ids": [self.channel_id],
                "mode": "now",
            },
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["status"], "queued")

    def test_published_post_cannot_be_edited_or_deleted_as_a_draft(self):
        created = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={
                "title": "queued post",
                "text": "published content",
                "mode": "now",
                "channel_ids": [self.channel_id],
            },
        )
        self.assertEqual(created.status_code, 200)
        post_id = created.json()["id"]

        edited = self.client.put(
            f"/api/posts/{post_id}",
            headers=self.headers,
            json={"title": "changed", "text": "changed"},
        )
        self.assertEqual(edited.status_code, 400)
        deleted = self.client.delete(
            f"/api/posts/{post_id}", headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 400)

    def test_owner_can_manage_subusers_with_roles(self):
        created = self.client.post(
            "/api/team",
            headers=self.headers,
            json={
                "username": "editor1",
                "password": "editor-password-123",
                "role": "editor",
                "display_name": "Editor",
            },
        )
        self.assertEqual(created.status_code, 200)
        member = created.json()
        self.assertEqual(member["role"], "editor")
        self.assertNotIn("pw_hash", member)

        listed = self.client.get("/api/team", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(user["id"] == member["id"] for user in listed.json()))

        deleted = self.client.delete(
            f"/api/team/{member['id']}", headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200)

    def test_rejects_upload_over_configured_limit(self):
        with patch("backend.main.MAX_UPLOAD", 4):
            response = self.client.post(
                "/api/media",
                headers=self.headers,
                files={"file": ("large.txt", b"12345", "text/plain")},
            )

        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
