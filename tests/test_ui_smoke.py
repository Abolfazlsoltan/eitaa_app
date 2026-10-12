import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import flet as ft
from main import App


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

            self.assertIsInstance(self.app.body.content, ft.Container)
            self.assertIsInstance(self.app.body.content.content, ft.Column)

        self.loop.run_until_complete(run())

    def test_ai_settings_and_core_views_construct_with_installed_flet(self):
        async def run():
            await self.app.api.bootstrap()
            for view in (
                "home",
                "posts",
                "templates",
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
                    self.assertIsInstance(self.app.body.content, ft.Container)

        self.loop.run_until_complete(run())

    def test_composer_ai_dropdowns_use_supported_selection_callbacks(self):
        async def run():
            await self.app.api.bootstrap()
            view = await self.app.v_new()
            dropdowns = self._find_controls(view, ft.Dropdown)
            callbacks = [
                control.on_select
                for control in dropdowns
                if control.on_select is not None
            ]
            self.assertEqual(len(callbacks), 3)

        self.loop.run_until_complete(run())

    def test_ai_provider_dropdown_registers_supported_selection_callback(self):
        async def run():
            await self.app.api.bootstrap()
            view = await self.app.v_ai_settings()
            dropdowns = self._find_controls(view, ft.Dropdown)
            self.assertEqual(len(dropdowns), 1)
            self.assertTrue(callable(dropdowns[0].on_select))

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
