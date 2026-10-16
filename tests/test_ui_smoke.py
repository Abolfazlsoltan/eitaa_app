import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import flet as ft
from main import ApiError, App


class MemoryStorage:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value

    async def remove(self, key):
        self.values.pop(key, None)


class UISmokeTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.page = MagicMock()
        self.page.platform_brightness = ft.Brightness.LIGHT
        self.app = App(self.page, self.loop, MemoryStorage())

    def tearDown(self):
        self.loop.close()

    def test_plus_navigation_builds_post_composer(self):
        async def run():
            await self.app.api.bootstrap()
            with patch.object(
                self.app,
                "_error_view",
                side_effect=AssertionError("unexpected error screen"),
            ):
                await self.app.go("new")

            self.assertIsInstance(self.app.body.content, ft.Column)
            self.assertTrue(self._find_controls(self.app.body.content, ft.TextField))

        self.loop.run_until_complete(run())

    def test_ai_settings_and_core_views_construct_with_installed_flet(self):
        async def run():
            await self.app.api.bootstrap()
            for view in (
                "home",
                "posts",
                "chat",
                "settings",
                "ai_settings",
                "help",
                "feedback",
                "auth",
                "channel_history",
            ):
                with self.subTest(view=view):
                    with patch.object(
                        self.app,
                        "_error_view",
                        side_effect=AssertionError(
                            f"{view} unexpectedly showed an error screen"
                        ),
                    ):
                        if view == "channel_history":
                            await self.app.go(view, 1)
                        else:
                            await self.app.go(view)
                    self.assertIsNotNone(self.app.body.content)

        self.loop.run_until_complete(run())

    def test_back_navigation_returns_to_the_previous_logical_screen(self):
        async def run():
            self.app.view = "new"
            with patch.object(self.app, "go", new=AsyncMock()) as go:
                await self.app.handle_back()
                go.assert_awaited_once_with("posts")

            self.app.view = "auth"
            with (
                patch.object(self.app, "go", new=AsyncMock()) as go,
                patch.object(self.app, "toast") as toast,
            ):
                await self.app.handle_back()
                go.assert_not_awaited()
                toast.assert_called_once()

        self.loop.run_until_complete(run())

    def test_navigation_replaces_templates_with_ai_chat(self):
        from main import NAV_VIEWS

        self.assertEqual(NAV_VIEWS, ["home", "posts", "chat", "settings"])
        self.assertFalse(hasattr(self.app, "v_templates"))
        self.assertEqual(
            self.app._fab_for("home").icon,
            ft.Icons.AUTO_AWESOME,
        )

    def test_chat_view_builds_with_composer_and_ai_action_selector(self):
        async def run():
            await self.app.api.bootstrap()
            view = await self.app.v_chat()
            self.assertIsInstance(view, ft.Column)
            self.assertEqual(
                len(self._find_controls(view, ft.TextField)), 1
            )
            self.assertEqual(self._find_controls(view, ft.Dropdown), [])
            self.assertTrue(any(
                control.icon == ft.Icons.SEND_ROUNDED
                for control in self._find_controls(view, ft.IconButton)
            ))

        self.loop.run_until_complete(run())

    def test_chat_request_failure_preserves_input_and_retry_succeeds(self):
        async def run():
            await self.app.api.bootstrap()
            self.app.spawn = lambda coro: asyncio.create_task(coro)
            self.app.api.ai = AsyncMock(
                side_effect=[
                    ApiError("مدل اول در دسترس نبود."),
                    "پیشنهاد آماده",
                ]
            )
            view = await self.app.v_chat()
            prompt = self._find_controls(view, ft.TextField)[0]
            send = next(
                control for control in self._find_controls(
                    view, ft.IconButton
                )
                if control.icon == ft.Icons.SEND_ROUNDED
            )
            prompt.value = "یک پست معرفی محصول بساز"
            await send.on_click(None)
            self.assertEqual(prompt.value, "یک پست معرفی محصول بساز")
            self.assertEqual(self.app.chat_messages[0]["status"], "failed")

            retry = next(
                control for control in self._find_controls(
                    view, ft.TextButton
                )
                if control.content == "تلاش دوباره"
            )
            await retry.on_click(None)
            self.assertEqual(prompt.value, "")
            self.assertEqual(self.app.chat_messages[1]["text"], "پیشنهاد آماده")
            self.assertEqual(self.app.chat_messages[1]["status"], "ready")
            self.assertEqual(self.app.api.ai.await_count, 2)
            await asyncio.sleep(0)

        self.loop.run_until_complete(run())

    def test_composer_ai_dropdowns_use_supported_selection_callbacks(self):
        async def run():
            await self.app.api.bootstrap()
            view = await self.app.v_new()
            self.assertEqual(self._find_controls(view, ft.Dropdown), [])
            labels = [
                control.value
                for control in self._find_controls(view, ft.Text)
                if isinstance(control.value, str)
            ]
            self.assertTrue(any("بازنویسی" in label for label in labels))
            self.assertTrue(any("کوتاه" in label for label in labels))

        self.loop.run_until_complete(run())

    def test_ai_settings_screen_hides_legacy_provider_and_key_controls(self):
        async def run():
            await self.app.api.bootstrap()
            visible = await self.app.v_ai_settings()
            self.assertIsInstance(visible, ft.Column)
            self.assertEqual(
                self._find_controls(visible, ft.Dropdown), []
            )
            self.assertEqual(
                self._find_controls(visible, ft.TextField), []
            )
            labels = [
                control.value
                for control in self._find_controls(visible, ft.Text)
                if isinstance(control.value, str)
            ]
            self.assertFalse(any(
                "API key" in label or "ارائه‌دهنده" in label
                for label in labels
            ))

        self.loop.run_until_complete(run())

    def test_posts_view_warns_when_loading_fails_instead_of_showing_empty_only(self):
        async def run():
            async def get(path):
                if path == "/posts":
                    raise ApiError("simulated connection failure")
                if path == "/channels":
                    return []
                return {}

            with patch.object(self.app.api, "get", new=AsyncMock(side_effect=get)):
                view = await self.app.v_posts()

            labels = [
                control.value
                for control in self._find_controls(view, ft.Text)
                if isinstance(control.value, str)
            ]
            self.assertTrue(any(
                "اتصال یا دریافت اطلاعات با مشکل روبه‌رو شد" in label
                for label in labels
            ))
            self.assertTrue(any("تلاش دوباره" in label for label in labels))
            self.assertIn("/posts", self.app._api_read_errors)

        self.loop.run_until_complete(run())

    def test_unexpected_screen_errors_are_logged_and_offer_retry(self):
        async def run():
            async def broken_view():
                raise RuntimeError("simulated view failure")

            self.app.v_broken = broken_view
            with self.assertLogs("main", level="ERROR") as captured:
                await self.app.go("broken")

            self.assertIn("Failed to load screen 'broken'", captured.output[0])
            actions = [
                control
                for control in self._find_controls(
                    self.app.body.content, ft.Container
                )
                if callable(control.on_click)
            ]
            self.assertGreaterEqual(len(actions), 2)

        self.loop.run_until_complete(run())

    @staticmethod
    def _find_controls(root, control_type):
        found = []
        seen = set()
        stack = [root]
        while stack:
            value = stack.pop()
            if isinstance(value, control_type):
                found.append(value)
            if isinstance(value, ft.Control):
                if id(value) in seen:
                    continue
                seen.add(id(value))
                if dataclasses.is_dataclass(value):
                    stack.extend(
                        getattr(value, field.name)
                        for field in dataclasses.fields(value)
                        if field.name not in ("parent", "page", "services")
                    )
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
        return found
