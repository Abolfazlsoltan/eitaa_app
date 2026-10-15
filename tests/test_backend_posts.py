import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

_test_dir = tempfile.TemporaryDirectory(prefix="eitaa-manager-tests-")
os.environ["APP_SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir.name}/test.db"
os.environ["MEDIA_DIR"] = f"{_test_dir.name}/media"

from fastapi.testclient import TestClient

from backend import auth
from backend.db import (
    Channel, Connection, Delivery, Post, RemovalReminder, SchemaMigration,
    Session, User, fernet,
)
from backend.main import app, deliver, _promote_due_reminders


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

    def _headers_for_role(self, role, username):
        with Session() as session:
            user = User(
                workspace_id=1,
                username=username,
                display_name=username,
                pw_hash=auth.hash_pw("test-password-123"),
                role=role,
            )
            session.add(user)
            session.commit()
            uid = user.id
        return {"Authorization": f"Bearer {auth.make_token(uid)}"}

    def test_analyst_cannot_create_posts(self):
        headers = self._headers_for_role("analyst", "analyst-create-test")
        response = self.client.post(
            "/api/posts", headers=headers, json={"text": "must be blocked"}
        )
        self.assertEqual(response.status_code, 403)

    def test_unmatched_write_routes_are_denied_to_non_owners(self):
        headers = self._headers_for_role("editor", "editor-default-deny-test")
        response = self.client.post(
            "/api/unregistered-write-route", headers=headers, json={}
        )
        self.assertEqual(response.status_code, 403)

    def test_editor_cannot_publish_posts(self):
        headers = self._headers_for_role("editor", "editor-publish-test")
        response = self.client.post(
            "/api/posts/1/publish", headers=headers, json={}
        )
        self.assertEqual(response.status_code, 403)

    def test_analytics_returns_exact_requested_date_window(self):
        response = self.client.get("/api/analytics?days=7", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        days = response.json()["days"]
        self.assertEqual(len(days), 7)
        keys = [item["date"] for item in days]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), 7)

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

    def test_draft_edits_keep_previous_text_revision(self):
        created = self.client.post(
            "/api/posts",
            headers=self.headers,
            json={"title": "before", "text": "old text"},
        )
        post_id = created.json()["id"]
        edited = self.client.put(
            f"/api/posts/{post_id}",
            headers=self.headers,
            json={"title": "after", "text": "new text"},
        )
        self.assertEqual(edited.status_code, 200)
        revisions = self.client.get(
            f"/api/posts/{post_id}/revisions", headers=self.headers
        )
        self.assertEqual(revisions.status_code, 200)
        self.assertEqual(revisions.json()[0]["title"], "before")
        self.assertEqual(revisions.json()[0]["text"], "old text")

    def test_history_search_and_channel_filter_return_app_delivery_metadata(self):
        with Session() as session:
            post = Post(
                title="History searchable title",
                text="recorded content",
                status="published",
            )
            session.add(post)
            session.flush()
            session.add(Delivery(
                post_id=post.id,
                channel_id=self.channel_id,
                idem_key=f"test-history:{post.id}",
                status="published",
                done_at=datetime.now(timezone.utc).replace(tzinfo=None),
                remote_message_id=321,
            ))
            session.commit()
        response = self.client.get(
            "/api/history",
            headers=self.headers,
            params={
                "q": "searchable",
                "channel_id": self.channel_id,
                "status": "published",
            },
        )
        self.assertEqual(response.status_code, 200)
        record = next(
            item for item in response.json()
            if item["title"] == "History searchable title"
        )
        self.assertEqual(record["deliveries"][0]["remote_message_id"], 321)
        self.assertIsNotNone(record["deliveries"][0]["done_at"])

    def test_delivery_stores_message_id_only_from_successful_send_result(self):
        with Session() as session:
            connection = Connection(
                name="delivery test",
                token_enc=fernet.encrypt(b"token-for-test").decode(),
            )
            session.add(connection)
            session.flush()
            channel = Channel(
                connection_id=connection.id,
                title="delivery test",
                chat_id="delivery-test",
            )
            session.add(channel)
            post = Post(title="delivery", text="hello", status="queued")
            session.add(post)
            session.flush()
            delivery = Delivery(
                post_id=post.id,
                channel_id=channel.id,
                idem_key=f"delivery-test:{post.id}",
                status="queued",
            )
            session.add(delivery)
            session.commit()
            delivery_id = delivery.id

        with patch(
            "backend.main.eitaa.send_message",
            new=AsyncMock(return_value={
                "ok": True, "result": {"message_id": 789},
            }),
        ):
            import asyncio
            asyncio.run(deliver(delivery_id))
        with Session() as session:
            stored = session.get(Delivery, delivery_id)
            self.assertEqual(stored.status, "published")
            self.assertEqual(stored.remote_message_id, 789)
            self.assertIsNotNone(stored.done_at)

    def test_anonymous_feedback_has_ticket_and_owner_can_respond(self):
        response = self.client.post(
            "/api/feedback",
            json={"category": "bug", "message": "A problem occurred."},
        )
        self.assertEqual(response.status_code, 200)
        ticket = response.json()["ticket"]
        lookup = self.client.get("/api/feedback", params={"ticket": ticket})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["status"], "open")

        records = self.client.get(
            "/api/feedback/admin", headers=self.headers
        )
        self.assertEqual(records.status_code, 200)
        item = next(row for row in records.json() if row["ticket"] == ticket)
        self.assertNotIn("username", item)
        self.assertNotIn("user_id", item)

        updated = self.client.patch(
            f"/api/feedback/admin/{ticket}",
            headers=self.headers,
            json={"status": "resolved", "reply": "Fixed."},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "resolved")
        self.assertEqual(
            self.client.get(
                "/api/feedback", params={"ticket": ticket}
            ).json()["reply"],
            "Fixed.",
        )

    def test_removal_reminder_is_persistent_editable_and_not_an_auto_delete(self):
        with Session() as session:
            post = Post(title="reminder test", text="published", status="published")
            session.add(post)
            session.flush()
            delivery = Delivery(
                post_id=post.id,
                channel_id=self.channel_id,
                idem_key=f"reminder-test:{post.id}",
                status="published",
                done_at=datetime.now(timezone.utc).replace(tzinfo=None),
                remote_message_id=654,
            )
            session.add(delivery)
            session.commit()
            delivery_id = delivery.id

        due_at = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
        created = self.client.post(
            "/api/removal-reminders",
            headers=self.headers,
            json={"delivery_id": delivery_id, "due_at": due_at},
        )
        self.assertEqual(created.status_code, 200)
        reminder_id = created.json()["id"]
        self.assertEqual(created.json()["remote_message_id"], 654)
        self.assertEqual(created.json()["status"], "scheduled")

        duplicate = self.client.post(
            "/api/removal-reminders",
            headers=self.headers,
            json={"delivery_id": delivery_id, "due_at": due_at},
        )
        self.assertEqual(duplicate.status_code, 409)

        updated_at = (
            datetime.now(timezone.utc) + timedelta(days=2)
        ).isoformat()
        updated = self.client.post(
            f"/api/removal-reminders/{reminder_id}/reschedule",
            headers=self.headers,
            json={"due_at": updated_at},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "scheduled")

        cancelled = self.client.post(
            f"/api/removal-reminders/{reminder_id}/cancel",
            headers=self.headers,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(
            self.client.get(
                "/api/removal-reminders",
                headers=self.headers,
            ).json()[0]["status"],
            "cancelled",
        )

        with Session() as session:
            reminder = RemovalReminder(
                delivery_id=delivery_id,
                remote_message_id=654,
                channel_id=self.channel_id,
                due_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=1),
            )
            session.add(reminder)
            session.commit()
            due_id = reminder.id
            self.assertEqual(_promote_due_reminders(session), 1)
            session.commit()
            self.assertEqual(_promote_due_reminders(session), 0)
            session.commit()
            self.assertEqual(session.get(RemovalReminder, due_id).status, "due")

    def test_removal_reminder_requires_recorded_remote_message_id(self):
        with Session() as session:
            post = Post(title="without message id", text="pending", status="published")
            session.add(post)
            session.flush()
            delivery = Delivery(
                post_id=post.id,
                channel_id=self.channel_id,
                idem_key=f"reminder-no-id:{post.id}",
                status="published",
            )
            session.add(delivery)
            session.commit()
            delivery_id = delivery.id
        response = self.client.post(
            "/api/removal-reminders",
            headers=self.headers,
            json={
                "delivery_id": delivery_id,
                "due_at": (
                    datetime.now(timezone.utc) + timedelta(days=1)
                ).isoformat(),
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_schema_migrations_include_reminder_addition(self):
        with Session() as session:
            versions = set(session.query(SchemaMigration.version).all())
        self.assertIn((3,), versions)

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
