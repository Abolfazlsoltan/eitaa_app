import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import eitaa


class EitaaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_forwards_supported_options(self):
        response = {"ok": True}
        with patch("backend.eitaa._call", new=AsyncMock(return_value=response)) as call:
            result = await eitaa.send_message(
                "test-token",
                "test-channel",
                "body",
                title="title",
                silent=True,
                pin=True,
                view_to_delete=100,
                reply_to_message_id=42,
            )

        self.assertIs(result, response)
        call.assert_awaited_once_with(
            "test-token",
            "sendMessage",
            chat_id="test-channel",
            text="body",
            title="title",
            disable_notification=1,
            pin=1,
            view_to_delete=100,
            reply_to_message_id=42,
        )

    async def test_send_document_forwards_caption_and_supported_options(self):
        with tempfile.NamedTemporaryFile() as upload:
            upload.write(b"test file")
            upload.flush()
            with patch(
                "backend.eitaa._call",
                new=AsyncMock(return_value={"ok": True}),
            ) as call:
                await eitaa.send_file(
                    "test-token",
                    "test-channel",
                    upload.name,
                    "test.pdf",
                    caption="caption",
                    view_to_delete=5,
                    reply_to_message_id=7,
                )

        call.assert_awaited_once()
        args, kwargs = call.await_args
        self.assertEqual(args[:2], ("test-token", "sendDocument"))
        self.assertEqual(kwargs["chat_id"], "test-channel")
        self.assertEqual(kwargs["caption"], "caption")
        self.assertEqual(kwargs["view_to_delete"], 5)
        self.assertEqual(kwargs["reply_to_message_id"], 7)
        self.assertEqual(kwargs["files"]["file"][0], "test.pdf")
