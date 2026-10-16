"""Eitaa Manager — a modern client for managing Eitaa channels.

The app handles content creation with AI assistance, direct publishing to
EitaaYar, and lightweight analytics — all through a single, cohesive UI.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import io
import json
import logging
import mimetypes
import re
import time
from pathlib import Path

import flet as ft

import config
from api import Api, ApiError
from fa import (
    combine_local,
    digits,
    humanize_bytes,
    is_valid_username,
    jdate,
    jfull_utc,
    to_utc_iso,
)

logger = logging.getLogger(__name__)


# =================================================================== palette

class Palette:
    # Brand
    PRIMARY = "#2563EB"
    PRIMARY_DARK = "#1D4ED8"
    ACCENT = "#06B6D4"
    # Surfaces (mutated by apply_appearance for light/dark)
    SURFACE = "#F8FAFC"
    SURFACE_DARK = "#0B1120"
    CARD = "#FFFFFF"
    CARD_DARK = "#141C2B"
    # Text
    TEXT = "#0F172A"
    TEXT_DARK = "#F1F5F9"
    MUTED = "#64748B"
    MUTED_DARK = "#94A3B8"
    # Borders & dividers
    BORDER = "#E2E8F0"
    BORDER_DARK = "#1E293B"
    # Semantic
    SUCCESS = "#10B981"
    WARNING = "#F59E0B"
    DANGER = "#EF4444"
    # Soft backgrounds for pills / chips
    SUCCESS_SOFT = "#D1FAE5"
    WARNING_SOFT = "#FEF3C7"
    DANGER_SOFT = "#FEE2E2"
    PRIMARY_SOFT = "#DBEAFE"
    # Aliases kept for backward-compat inside this module
    SURFACE_LIGHT = SURFACE
    CARD_LIGHT = CARD
    TEXT_LIGHT = TEXT
    MUTED_LIGHT = MUTED


# ===================================================================== state

ST = {
    "draft": "پیش‌نویس",
    "pending": "در انتظار تأیید",
    "scheduled": "زمان‌بندی‌شده",
    "queued": "در صف",
    "sending": "در حال ارسال",
    "published": "منتشرشده",
    "failed": "ناموفق",
    "cancelled": "لغوشده",
}

ST_COLOR = {
    "draft": "#94A3B8",
    "pending": Palette.WARNING,
    "scheduled": Palette.PRIMARY,
    "queued": Palette.WARNING,
    "sending": Palette.WARNING,
    "published": Palette.SUCCESS,
    "failed": Palette.DANGER,
    "cancelled": "#94A3B8",
}

# Statuses for which a post can still be cancelled by the user.
CANCELLABLE = ("draft", "pending", "scheduled", "queued")

AI_ACTIONS = [
    ("create", "✨ ساخت", "ساخت پست از توضیح"),
    ("rewrite", "✏️ بازنویسی", "بازنویسی متن"),
    ("shorter", "🔻 کوتاه‌تر", "خلاصه‌سازی"),
    ("longer", "🔺 کامل‌تر", "توسعه متن"),
    ("engaging", "🔥 جذاب‌تر", "افزایش جذابیت"),
    ("formal", "🎩 رسمی", "لحن رسمی"),
    ("casual", "💬 خودمانی", "لحن دوستانه"),
    ("news", "📰 خبری", "سبک خبری"),
    ("titles", "🏷️ عنوان", "پیشنهاد عنوان"),
    ("hashtags", "#️⃣ هشتگ", "پیشنهاد هشتگ"),
    ("proofread", "✓ اصلاح", "اصلاح نگارشی"),
    ("summarize", "خلاصه", "خلاصه‌سازی"),
    ("translate", "ترجمه", "ترجمه متن"),
    ("variants", "چند نسخه", "پیشنهاد چند نسخه"),
]

ACCENTS = {
    "آبی": ("#2563EB", "#1D4ED8"),
    "سبزآبی": ("#0D9488", "#0F766E"),
    "بنفش": ("#7C3AED", "#6D28D9"),
    "نارنجی": ("#EA580C", "#C2410C"),
    "صورتی": ("#DB2777", "#BE185D"),
}

# Tabs shown in the bottom navigation bar, in order.
NAV_VIEWS = ["home", "posts", "chat", "settings"]

# Map notification `icon` keys to Flet icon constants.
_NOTIFICATION_ICONS = {
    "auto_awesome": ft.Icons.AUTO_AWESOME,
    "person_outline": ft.Icons.PERSON_OUTLINE,
    "system_update_alt": ft.Icons.SYSTEM_UPDATE_ALT,
    "new_releases": ft.Icons.NEW_RELEASES,
    "info_outline": ft.Icons.INFO_OUTLINE,
    "feedback_outlined": ft.Icons.FEEDBACK_OUTLINED,
}


# ==================================================================== helpers

T = lambda s, **k: ft.Text(str(s), **k)


def tf(label, **k):
    return ft.TextField(
        label=label,
        border_radius=16,
        border_color=Palette.BORDER,
        focused_border_color=Palette.PRIMARY,
        cursor_color=Palette.PRIMARY,
        text_size=15,
        content_padding=ft.Padding.symmetric(horizontal=16, vertical=14),
        **k,
    )


def btn_primary(text, fn, **k):
    return ft.Container(
        content=T(text, color="#FFFFFF", weight=ft.FontWeight.W_600, size=15),
        padding=ft.Padding.symmetric(horizontal=22, vertical=15),
        alignment=ft.Alignment(0, 0),
        border_radius=16,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.CENTER_LEFT,
            end=ft.Alignment.CENTER_RIGHT,
            colors=[Palette.PRIMARY, Palette.PRIMARY_DARK],
        ),
        shadow=ft.BoxShadow(
            blur_radius=16,
            color=ft.Colors.with_opacity(0.28, Palette.PRIMARY),
            offset=ft.Offset(0, 6),
        ),
        ink=True,
        on_click=fn,
        **k,
    )


def btn_ghost(text, fn, **k):
    return ft.Container(
        content=T(text, color=Palette.PRIMARY, weight=ft.FontWeight.W_600, size=15),
        padding=ft.Padding.symmetric(horizontal=20, vertical=13),
        alignment=ft.Alignment(0, 0),
        border_radius=16,
        border=ft.Border.all(1.5, Palette.BORDER),
        bgcolor=ft.Colors.with_opacity(0.04, Palette.PRIMARY),
        ink=True,
        on_click=fn,
        **k,
    )


def icon_btn(icon, fn, tooltip=None, color=None, size=22):
    return ft.IconButton(
        icon=icon,
        icon_size=size,
        icon_color=color or Palette.TEXT,
        tooltip=tooltip,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=14),
            padding=10,
        ),
        on_click=fn,
    )


def surface_card(*children, padding=18, elevation=True, **k):
    shadow = None
    if elevation:
        shadow = ft.BoxShadow(
            blur_radius=24,
            spread_radius=-2,
            color=ft.Colors.with_opacity(0.08, "#0F172A"),
            offset=ft.Offset(0, 8),
        )
    return ft.Container(
        content=ft.Column(list(children), spacing=10, tight=True),
        padding=padding,
        border_radius=22,
        bgcolor=Palette.CARD,
        border=ft.Border.all(1, Palette.BORDER),
        shadow=shadow,
        **k,
    )


def pill(label, color):
    return ft.Container(
        content=T(label, size=11, color=color, weight=ft.FontWeight.W_700),
        padding=ft.Padding.symmetric(horizontal=12, vertical=5),
        border_radius=999,
        bgcolor=ft.Colors.with_opacity(0.14, color),
    )


def empty_state(icon, title, subtitle, action=None):
    """Consistent empty / zero-data illustration used across screens."""
    children = [
        ft.Container(
            content=ft.Icon(icon, size=48, color=Palette.MUTED),
            width=96, height=96, border_radius=28,
            alignment=ft.Alignment(0, 0),
            bgcolor=ft.Colors.with_opacity(0.08, Palette.PRIMARY),
        ),
        ft.Container(height=8),
        T(title, size=17, weight=ft.FontWeight.W_600, color=Palette.TEXT,
          text_align=ft.TextAlign.CENTER),
        T(subtitle, size=13, color=Palette.MUTED,
          text_align=ft.TextAlign.CENTER),
    ]
    if action is not None:
        children.append(ft.Container(height=12))
        children.append(action)
    return ft.Container(
        content=ft.Column(
            children,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=6,
        ),
        padding=ft.Padding.symmetric(horizontal=32, vertical=48),
        alignment=ft.Alignment(0, 0),
        expand=True,
    )


# ======================================================================= App

class App:
    # -------- Markdown / code-fence parsing ----------
    # Fenced code block: ```lang\ncode``` (language optional, newline required)
    _CODE_FENCE = re.compile(
        r"```([A-Za-z0-9_+\-.]*)[ \t]*\n(.*?)```",
        re.DOTALL,
    )
    # Patterns that identify technical content that must never reach the UI.
    _TECHNICAL_RE = re.compile(
        r"Traceback|File \"|<[^>]{1,80}>|Exception|"
        r"https?://|/api/|HTTP \d{3}|AttributeError|KeyError|"
        r"TypeError|ValueError"
    )

    def __init__(self, page: ft.Page, loop: asyncio.AbstractEventLoop, prefs):
        self.page = page
        self.loop = loop

        self.me: dict = {"name": "محلی", "role": "local"}
        self.channels: list = []
        self.view = "home"
        self._last_primary_view = "home"
        self.theme_mode = "light"
        self.accent_name = "آبی"
        self.chat_messages: list[dict] = []
        self._last_notification_at = 0.0

        self.body = ft.Container(expand=True)
        self.api = Api(Storage(prefs))
        self.file_picker = ft.FilePicker()
        self.url_launcher = ft.UrlLauncher()
        self.server_base = self.api.base
        self.api.on_unauthorized = lambda: self.spawn(self._session_expired())

    # ------------------------------------------------------------ utilities
    def spawn(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _local_schedule_loop(self):
        # Local scheduling is foreground-only; a configured backend has its
        # own persistent worker and does not use this loop.
        while True:
            await asyncio.sleep(15)
            if self.api.online:
                continue
            try:
                await self.api.process_due_scheduled()
            except Exception:
                logger.exception("Local scheduled-post worker failed")

    def _safe_message(self, message) -> str:
        """Return a message that is safe to show in the UI.

        Strips technical noise (stack traces, URLs, HTTP statuses, English
        exception text) and truncates overly long messages. When nothing
        usable remains, a generic Persian fallback is returned so the user
        always has a next step.
        """
        fallback = "خطایی رخ داد؛ لطفاً دوباره تلاش کنید."
        if not isinstance(message, str):
            return fallback
        clean = message.strip()
        if not clean:
            return fallback
        if self._TECHNICAL_RE.search(clean):
            return fallback
        if len(clean) > 220:
            return fallback
        return clean

    def toast(self, msg: str, error: bool = False):
        text = self._safe_message(msg) if error else str(msg)[:220]
        color = Palette.DANGER if error else None
        self.page.show_dialog(
            ft.SnackBar(
                content=T(text, color="#FFFFFF" if color else None),
                bgcolor=color,
                behavior=ft.SnackBarBehavior.FLOATING,
                margin=16,
            )
        )

    async def guard(self, coro):
        try:
            return await coro
        except ApiError as e:
            self.toast(str(e), error=True)
            return None

    async def confirm(self, msg: str) -> bool:
        fut = self.loop.create_future()

        def done(v):
            def h(e):
                if not fut.done():
                    fut.set_result(v)
                self.page.pop_dialog()
            return h

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            content=T(msg, size=15),
            actions=[
                ft.TextButton("لغو", on_click=done(False)),
                ft.FilledButton("تأیید", on_click=done(True)),
            ],
        ))
        return await fut

    # ------------------------------------------------------------- surfaces
    def show(self, content):
        self.body.content = content
        self.page.update()

    # ------------------------------------------------------- modern header
    def gradient_header(self, title, *, subtitle=None, actions=None):
        header_actions = list(actions or [])
        needs_back = self.view not in (
            "home", "posts", "settings", "auth", "onboarding",
        )
        has_back = any(
            getattr(getattr(action, "content", None), "icon", None)
            == ft.Icons.ARROW_FORWARD
            for action in header_actions
        )
        if needs_back and not has_back:
            header_actions.insert(0, self._header_icon(
                ft.Icons.ARROW_FORWARD,
                on_click=lambda e: self.spawn(self.handle_back()),
            ))
        rows = [
            ft.Row([
                T(title, size=22, weight=ft.FontWeight.BOLD, color="#FFFFFF",
                  expand=True),
                *header_actions,
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ]
        if subtitle:
            rows.append(T(subtitle, size=13, color="#FFFFFFB0"))

        return ft.Container(
            content=ft.Column(rows, spacing=4, tight=True),
            padding=ft.Padding.only(left=20, right=20, top=20, bottom=24),
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
                colors=[Palette.PRIMARY, Palette.PRIMARY_DARK],
            ),
            border_radius=ft.BorderRadius.only(
                bottom_left=28, bottom_right=28,
            ),
        )

    def _header_icon(self, icon, on_click=None):
        return ft.Container(
            content=ft.Icon(icon, color="#FFFFFF", size=22),
            width=44, height=44, border_radius=14,
            alignment=ft.Alignment(0, 0),
            bgcolor="#FFFFFF20",
            ink=on_click is not None,
            on_click=on_click,
        )

    # ----------------------------------------------------------- skeletons
    def _skeleton_line(self, *, width=None, height=12):
        return ft.Container(
            width=width,
            height=height,
            border_radius=height // 2,
            bgcolor=Palette.BORDER,
        )

    def _skeleton_card(self, lines: int = 3):
        return ft.Container(
            content=ft.Column([
                ft.Row([
                    self._skeleton_line(width=110, height=14),
                    ft.Container(expand=True),
                    self._skeleton_line(width=64, height=20),
                ], spacing=8),
                self._skeleton_line(width=180, height=10),
                *[
                    self._skeleton_line(height=10)
                    for _ in range(max(0, lines - 2))
                ],
            ], spacing=10, tight=True),
            padding=16,
            border_radius=18,
            bgcolor=Palette.CARD_LIGHT,
            shadow=ft.BoxShadow(blur_radius=14, color=ft.Colors.with_opacity(0.06, "#0F172A"),
                                offset=ft.Offset(0, 3)),
        )

    def _skeleton_list(self, count: int = 3):
        return ft.Column(
            [self._skeleton_card() for _ in range(max(1, count))],
            spacing=10,
        )

    # =========================================================== boot flow =
    async def boot(self):
        # Splash
        self.show(ft.Container(
            content=ft.Column(
                [
                    self._logo(96),
                    ft.Container(height=20),
                    T(config.APP_NAME, size=24,
                      weight=ft.FontWeight.BOLD, color=Palette.TEXT_LIGHT),
                    T(config.APP_TAGLINE, size=13, color=Palette.MUTED_LIGHT),
                    ft.Container(height=28),
                    ft.ProgressRing(width=28, height=28, stroke_width=3,
                                    color=Palette.PRIMARY),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        ))

        try:
            await self.api.bootstrap()
        except ApiError as e:
            return self.show(self._error_view(str(e), "home"))
        except Exception:
            logger.exception("Application startup failed during bootstrap")
            return self.show(self._error_view(
                "ذخیره‌سازی برنامه در دسترس نیست. دوباره تلاش کنید.", "home"
            ))
        theme_mode = await self.api.storage.get("appearance_theme") or "system"
        accent_name = await self.api.storage.get("appearance_accent") or "آبی"
        self.apply_appearance(theme_mode, accent_name)
        if self.api.online:
            try:
                state = await self.api.auth_state()
            except ApiError as e:
                return self.show(self._auth_unavailable(str(e)))
            return await self.go("auth", not state.get("has_users", False))
        await self.enter()

    def apply_appearance(self, mode: str, accent_name: str) -> None:
        if mode not in ("system", "light", "dark"):
            mode = "system"
        if accent_name not in ACCENTS:
            accent_name = "آبی"
        self.theme_mode = mode
        self.accent_name = accent_name
        Palette.PRIMARY, Palette.PRIMARY_DARK = ACCENTS[accent_name]
        brightness = getattr(self.page, "platform_brightness", None)
        dark = mode == "dark" or (
            mode == "system" and brightness == ft.Brightness.DARK
        )
        # Core surfaces & text
        Palette.SURFACE = Palette.SURFACE_DARK if dark else "#F8FAFC"
        Palette.CARD = Palette.CARD_DARK if dark else "#FFFFFF"
        Palette.TEXT = Palette.TEXT_DARK if dark else "#0F172A"
        Palette.MUTED = Palette.MUTED_DARK if dark else "#64748B"
        Palette.BORDER = "#1E293B" if dark else "#E2E8F0"
        # Soft semantic backgrounds (slightly adjusted for dark)
        Palette.PRIMARY_SOFT = "#1E3A5F" if dark else "#DBEAFE"
        Palette.SUCCESS_SOFT = "#064E3B" if dark else "#D1FAE5"
        Palette.WARNING_SOFT = "#78350F" if dark else "#FEF3C7"
        Palette.DANGER_SOFT = "#7F1D1D" if dark else "#FEE2E2"
        # Backward-compat aliases used throughout the file
        Palette.SURFACE_LIGHT = Palette.SURFACE
        Palette.CARD_LIGHT = Palette.CARD
        Palette.TEXT_LIGHT = Palette.TEXT
        Palette.MUTED_LIGHT = Palette.MUTED
        self.page.theme_mode = {
            "system": ft.ThemeMode.SYSTEM,
            "light": ft.ThemeMode.LIGHT,
            "dark": ft.ThemeMode.DARK,
        }[mode]
        self.page.theme = ft.Theme(
            color_scheme_seed=Palette.PRIMARY,
            font_family="Vazirmatn",
            visual_density=ft.VisualDensity.COMFORTABLE,
        )
        self.page.dark_theme = ft.Theme(
            color_scheme_seed=Palette.PRIMARY,
            font_family="Vazirmatn",
            visual_density=ft.VisualDensity.COMFORTABLE,
            color_scheme=ft.ColorScheme(
                primary=Palette.PRIMARY,
                surface=Palette.SURFACE_DARK,
                on_surface=Palette.TEXT_DARK,
            ),
        )
        self.page.bgcolor = Palette.SURFACE

    def _logo(self, size=84):
        return ft.Container(
            content=ft.Icon(ft.Icons.CAMPAIGN, size=size // 2, color="#FFFFFF"),
            width=size, height=size,
            border_radius=size // 3.2,
            alignment=ft.Alignment(0, 0),
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
                colors=[Palette.ACCENT, Palette.PRIMARY],
            ),
            shadow=ft.BoxShadow(
                blur_radius=30, color=ft.Colors.with_opacity(0.4, Palette.PRIMARY),
                offset=ft.Offset(0, 10),
            ),
        )

    # ========================================================== main shell =
    async def enter(self):
        if self.api.online and not self.api.token:
            return await self.go("auth", True)
        try:
            self.channels = await self.api.get("/channels")
        except ApiError:
            self.channels = []

        if not self.channels:
            self.page.navigation_bar = None
            self.page.floating_action_button = None
            return await self.onboarding()

        await self.go("home")

    async def _session_expired(self):
        self.api.logout()
        self.me = {"name": "محلی", "role": "local"}
        self.toast("نشست شما منقضی شده است؛ دوباره وارد شوید.", error=True)
        try:
            state = await self.api.auth_state()
        except ApiError as e:
            return self.show(self._auth_unavailable(str(e)))
        await self.go("auth", not state.get("has_users", False))

    def _auth_unavailable(self, message):
        return ft.Container(
            content=ft.Column([
                ft.Container(height=56),
                self._logo(72),
                ft.Container(height=16),
                T("اتصال به سرور برقرار نشد", size=18,
                  weight=ft.FontWeight.BOLD, color=Palette.TEXT_LIGHT),
                T(self._safe_message(message), size=13,
                  color=Palette.MUTED_LIGHT, text_align=ft.TextAlign.CENTER),
                ft.Container(height=20),
                btn_primary("تلاش دوباره", lambda e: self.spawn(self._retry_backend())),
                ft.Container(height=8),
                btn_ghost("ادامه بدون سرور", lambda e: self.spawn(self._continue_offline())),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            padding=32,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        )

    async def _retry_backend(self):
        await self.boot()

    async def _continue_offline(self):
        self.api.set_backend("")
        await self.enter()

    async def v_auth(self, register: bool = False):
        username = tf("نام کاربری")
        password = tf("رمز عبور", password=True, can_reveal_password=True)
        display = tf("نام نمایشی") if register else None

        async def submit():
            u = (username.value or "").strip()
            p = password.value or ""
            if not u or not p:
                return self.toast("نام کاربری و رمز عبور را وارد کنید.", error=True)
            if register and len(p) < 8:
                return self.toast("رمز عبور حداقل ۸ نویسه باشد.", error=True)
            try:
                if register:
                    user = await self.api.register(
                        u, p, (display.value or "").strip() if display else ""
                    )
                else:
                    user = await self.api.login(u, p)
            except ApiError as e:
                return self.toast(str(e), error=True)
            self.me = user
            self.toast("ورود موفق بود.")
            await self.enter()

        heading = "ساخت حساب مالک" if register else "ورود به سرور"
        action = "ثبت‌نام" if register else "ورود"
        fields = [username, password]
        if display is not None:
            fields.insert(1, display)

        return ft.Container(
            content=ft.Column([
                ft.Container(height=36),
                self._logo(84),
                T(heading, size=22, weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                T(
                    "حساب مالک را برای این سرور بسازید."
                    if register else "برای ادامه به سرور امن متصل شوید.",
                    size=13, color=Palette.MUTED_LIGHT,
                    text_align=ft.TextAlign.CENTER,
                ),
                surface_card(
                    *fields,
                    T("رمز عبور حداقل ۸ نویسه باشد.",
                      size=11, color=Palette.MUTED_LIGHT),
                    btn_primary(action, lambda e: self.spawn(submit())),
                ),
                T("نشست فقط تا زمان بسته‌بودن برنامه در حافظه می‌ماند.",
                  size=11, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
               spacing=16, scroll=ft.ScrollMode.AUTO),
            padding=24,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        )

    def _nav_bar(self, selected: int):
        return ft.NavigationBar(
            selected_index=selected,
            bgcolor=Palette.CARD,
            indicator_color=ft.Colors.with_opacity(0.18, Palette.PRIMARY),
            label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
            height=68,
            elevation=0,
            shadow_color=ft.Colors.with_opacity(0.06, "#0F172A"),
            on_change=lambda e: self.spawn(self.go_nav(int(e.control.selected_index))),
            destinations=[
                ft.NavigationBarDestination(
                    icon=ft.Icons.HOME_OUTLINED,
                    selected_icon=ft.Icons.HOME_ROUNDED,
                    label="خانه",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.ARTICLE_OUTLINED,
                    selected_icon=ft.Icons.ARTICLE_ROUNDED,
                    label="پست‌ها",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.AUTO_AWESOME_OUTLINED,
                    selected_icon=ft.Icons.AUTO_AWESOME,
                    label="دستیار",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.SETTINGS_OUTLINED,
                    selected_icon=ft.Icons.SETTINGS_ROUNDED,
                    label="تنظیمات",
                ),
            ],
        )

    async def go_nav(self, index: int):
        await self.go(NAV_VIEWS[index])

    async def handle_back(self):
        """Navigate inside the app instead of unexpectedly closing it."""
        if self.view in ("auth", "onboarding"):
            self.toast("برای ادامه، همین مرحله را کامل کن.")
            return
        if self.view in NAV_VIEWS:
            if self.view != "home":
                await self.go("home")
            else:
                self.toast("الان در صفحه اصلی هستی؛ برای خروج از برنامه از گزینه خروج استفاده کن.")
            return
        back_targets = {
            "new": "posts",
            "chat": "home",
            "channel_history": "posts",
            "reminders": "settings",
            "ai_settings": "settings",
        }
        target = back_targets.get(self.view, self._last_primary_view)
        if target == self.view and target not in NAV_VIEWS:
            target = "home"
        await self.go(target)

    async def go(self, name: str, *args):
        if name in NAV_VIEWS:
            self._last_primary_view = name
        self.view = name
        self.page.floating_action_button = self._fab_for(name)
        # The bottom bar is a tab bar; sub-screens (e.g. "new") hide it.
        self.page.navigation_bar = (
            self._nav_bar(NAV_VIEWS.index(name)) if name in NAV_VIEWS else None
        )

        if name in NAV_VIEWS:
            # Tab views: render a lightweight skeleton while data loads.
            self.body.content = ft.Container(
                content=ft.Column([
                    self._skeleton_list(2),
                    ft.Container(height=12),
                    self._skeleton_list(2),
                    ft.Container(height=80),
                ], scroll=ft.ScrollMode.AUTO, spacing=0, expand=True),
                padding=16,
                expand=True,
                bgcolor=Palette.SURFACE_LIGHT,
            )
        else:
            self.body.content = ft.Container(
                content=ft.Column([
                    ft.ProgressRing(width=24, height=24, stroke_width=2.5,
                                    color=Palette.PRIMARY),
                ], alignment=ft.MainAxisAlignment.CENTER,
                   horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                expand=True,
                bgcolor=Palette.SURFACE_LIGHT,
            )
        self.page.update()

        try:
            content = await getattr(self, f"v_{name}")(*args)
        except ApiError as e:
            content = self._error_view(str(e), name)
        except Exception:
            logger.exception("Failed to load screen %r", name)
            content = self._error_view(
                "بارگذاری این صفحه ناموفق بود. دوباره تلاش کنید یا به صفحه قبل برگردید.",
                name,
                retry_args=args,
            )

        self.body.content = content
        self.page.update()

        # Notifications only appear on primary tab screens, never on sub-
        # screens such as the composer, and never while a dialog is open.
        if config.FEATURES_GLOBAL_NOTIFICATIONS and name in NAV_VIEWS:
            asyncio.create_task(self._maybe_show_notification())

    def _fab_for(self, view):
        if view in ("home", "posts"):
            return ft.FloatingActionButton(
                icon=ft.Icons.AUTO_AWESOME,
                tooltip="گفت‌وگو با دستیار هوشمند",
                bgcolor="#6D4AFF",
                foreground_color="#FFFFFF",
                on_click=lambda e: self.spawn(self.go("chat")),
            )
        if view == "chat":
            return ft.FloatingActionButton(
                icon=ft.Icons.ADD,
                tooltip="ساخت پیش‌نویس",
                bgcolor=Palette.PRIMARY,
                foreground_color="#FFFFFF",
                on_click=lambda e: self.spawn(self.go("new")),
            )
        return None

    def _error_view(self, message, back_to="home", retry_args=()):
        # A failed sub-screen should go back to a screen that can render.
        target = back_to if back_to in NAV_VIEWS else "home"
        safe = self._safe_message(message)
        controls = [
            ft.Container(height=80),
            ft.Container(
                content=ft.Icon(ft.Icons.ERROR_OUTLINE, size=48,
                                color=Palette.MUTED_LIGHT),
                width=88, height=88, border_radius=28,
                alignment=ft.Alignment(0, 0),
                bgcolor=ft.Colors.with_opacity(0.08, Palette.DANGER),
            ),
            T(safe, size=14, color=Palette.MUTED_LIGHT,
              text_align=ft.TextAlign.CENTER),
            ft.Container(height=16),
            btn_primary("تلاش دوباره",
                        lambda e: self.spawn(self.go(back_to, *retry_args))),
        ]
        if back_to not in NAV_VIEWS:
            controls.append(
                btn_ghost("بازگشت",
                          lambda e: self.spawn(self.go(target)))
            )
        return ft.Container(
            content=ft.Column(
                controls,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10,
            ),
            padding=32,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        )

    # ------------------------------------------------------- onboarding ----
    async def onboarding(self):
        """First onboarding screen: optional local profile."""
        if not config.FEATURES_LOCAL_PROFILE:
            return await self._continue_after_onboarding()

        try:
            profile = await self.api.get_profile() or {}
        except Exception:
            profile = {}

        name_field = tf(
            "نام نمایشی",
            value=profile.get("display_name", ""),
            max_length=config.PROFILE_DISPLAY_NAME_MAX,
        )
        username_field = tf(
            "نام کاربری",
            value=profile.get("username", ""),
            max_length=config.PROFILE_USERNAME_MAX,
        )

        async def skip():
            await self._continue_after_onboarding()

        async def save_and_continue():
            name = (name_field.value or "").strip()
            username = (username_field.value or "").strip()
            if username and not is_valid_username(
                username,
                min_len=config.PROFILE_USERNAME_MIN,
                max_len=config.PROFILE_USERNAME_MAX,
                allowed_chars=config.PROFILE_USERNAME_ALLOWED_CHARS,
            ):
                return self.toast(
                    "نام کاربری فقط حروف انگلیسی، عدد و _ باشد.",
                    error=True,
                )
            try:
                await self.api.save_profile({
                    "display_name": name,
                    "username": username,
                })
            except ApiError as e:
                return self.toast(str(e), error=True)
            await self._continue_after_onboarding()

        self.show(ft.Container(
            content=ft.Column([
                ft.Container(height=40),
                self._logo(84),
                ft.Container(height=16),
                T("پروفایل خودت را بساز", size=22,
                  weight=ft.FontWeight.BOLD, color=Palette.TEXT_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                T("این اطلاعات فقط روی دستگاه تو ذخیره می‌شود "
                  "و می‌توانی هر زمان آن را تغییر دهی.",
                  size=13, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                ft.Container(height=24),
                surface_card(
                    name_field,
                    username_field,
                    T("این گام کاملاً اختیاری است.",
                      size=11, color=Palette.MUTED_LIGHT),
                ),
                ft.Container(height=16),
                ft.Row([
                    btn_ghost("رد کردن", lambda e: self.spawn(skip())),
                    btn_primary("ذخیره و ادامه",
                                lambda e: self.spawn(save_and_continue())),
                ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
               spacing=10, scroll=ft.ScrollMode.AUTO),
            padding=24,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        ))
        self.page.update()

    async def _continue_after_onboarding(self):
        try:
            await self.api.bootstrap()
        except Exception:
            pass
        self.channels = await self._safe_get("/channels", [])
        if self.channels:
            await self.enter()
        else:
            self.channel_step()

    def channel_step(self):
        """Last onboarding screen: connect the first channel."""
        cid = tf("شناسه یا لینک کانال/گروه",
                 hint_text="مثلاً eitaa.com/mychannel")
        title = tf("نام نمایشی", hint_text="اختیاری")
        bot_token = ft.TextField(
            label="توکن ایتایار (در صورت نیاز)",
            hint_text="توکن جدید را فقط اگر اتصالی ندارید وارد کنید",
            password=True,
            can_reveal_password=True,
            border_radius=14,
        )

        async def connect(e=None):
            if not cid.value or not cid.value.strip():
                return self.toast("شناسه کانال را وارد کنید", error=True)
            try:
                connection = None
                if (bot_token.value or "").strip():
                    connection = await self.api.post("/connections", {
                        "name": "اتصال اصلی",
                        "token": bot_token.value.strip(),
                    })
                else:
                    existing = await self.api.get("/connections")
                    connection = existing[0] if existing else None
                channel_payload = {
                    "value": cid.value.strip(),
                    "title": (title.value or "").strip() or cid.value.strip(),
                }
                if isinstance(connection, dict) and connection.get("id") is not None:
                    channel_payload["connection_id"] = connection["id"]
                await self.api.post("/channels", channel_payload)
            except ApiError as err:
                return self.toast(str(err), error=True)
            self.toast("کانال با موفقیت اضافه شد.")
            await self.enter()

        self.show(ft.Container(
            content=ft.Column([
                ft.Container(height=40),
                ft.Container(
                    content=ft.Icon(ft.Icons.CAMPAIGN, size=40, color="#FFFFFF"),
                    width=80, height=80, border_radius=24,
                    alignment=ft.Alignment(0, 0),
                    gradient=ft.LinearGradient(
                        colors=[Palette.ACCENT, Palette.PRIMARY],
                    ),
                    shadow=ft.BoxShadow(
                        blur_radius=24,
                        color=ft.Colors.with_opacity(0.35, Palette.PRIMARY),
                        offset=ft.Offset(0, 8),
                    ),
                ),
                ft.Container(height=16),
                T("اولین کانال را اضافه کن", size=20,
                  weight=ft.FontWeight.BOLD, color=Palette.TEXT_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                T("برای شروع کار حداقل یک کانال لازم است.",
                  size=13, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                ft.Container(height=20),
                surface_card(
                    cid, title, bot_token,
                    T("توکن فقط در صورتی لازم است که هنوز اتصالی "
                      "ثبت نشده باشد.", size=11, color=Palette.MUTED_LIGHT),
                    btn_primary("اتصال و ادامه",
                                lambda e: self.spawn(connect())),
                ),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
               spacing=8, scroll=ft.ScrollMode.AUTO),
            padding=24,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        ))

    # ----------------------------------------------------- notifications ---
    async def _maybe_show_notification(self):
        if time.time() - self._last_notification_at < 8:
            return
        try:
            notifications = await self.api.pending_notifications()
        except Exception:
            return
        if not notifications:
            return
        now = dt.datetime.utcnow().isoformat(timespec="seconds")
        for notification in notifications:
            try:
                decision = await self.api.notification_decision(
                    notification, now,
                )
            except Exception:
                continue
            if decision != "show":
                continue
            nid = notification.get("id") or ""
            try:
                await self.api.record_notification_shown(nid)
            except Exception:
                pass
            self._last_notification_at = time.time()
            self._show_notification_dialog(notification)
            return

    def _show_notification_dialog(self, notification):
        nid = notification.get("id") or ""
        icon_key = notification.get("icon", "info_outline")
        icon = _NOTIFICATION_ICONS.get(icon_key, ft.Icons.INFO_OUTLINE)

        async def dismiss_and_close(_=None):
            try:
                await self.api.dismiss_notification(nid)
            except Exception:
                pass
            self.page.pop_dialog()

        async def snooze_and_close(_=None):
            try:
                await self.api.snooze_notification(nid, days=3)
            except Exception:
                pass
            self.page.pop_dialog()

        async def run_action(_=None):
            self.page.pop_dialog()
            await self._handle_notification_action(
                notification.get("action_key")
            )

        action_label = notification.get("action_label")
        actions: list[ft.Control] = []
        if action_label:
            actions.append(ft.FilledButton(
                action_label,
                on_click=lambda e: self.spawn(run_action()),
            ))
        actions.append(ft.TextButton(
            notification.get("secondary_label") or "بعداً",
            on_click=lambda e: self.spawn(snooze_and_close()),
        ))
        actions.append(ft.TextButton(
            "دیگر نشان نده",
            on_click=lambda e: self.spawn(dismiss_and_close()),
        ))

        self.page.show_dialog(ft.AlertDialog(
            modal=False,
            content=ft.Column([
                ft.Container(
                    content=ft.Icon(icon, size=26, color="#FFFFFF"),
                    width=52, height=52, border_radius=18,
                    alignment=ft.Alignment(0, 0),
                    gradient=ft.LinearGradient(
                        colors=[Palette.ACCENT, Palette.PRIMARY],
                    ),
                ),
                T(notification.get("title", ""),
                  size=17, weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                T(notification.get("body", ""), size=13,
                  color=Palette.MUTED_LIGHT),
            ], spacing=12, tight=True,
               horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            actions=actions,
        ))

    async def _handle_notification_action(self, action_key):
        if not action_key:
            return
        if action_key == "open_help":
            await self.go("help")
        elif action_key == "open_profile":
            await self._profile_dialog()
        elif action_key == "check_updates":
            await self.go("updates")
        elif action_key == "open_feedback":
            await self.go("feedback")
        # Unknown keys are ignored; the notification still counts as shown.

    # =============================================================== views =
    async def v_home(self):
        stats = await self._safe_get("/stats", {})
        if isinstance(stats, dict):
            stats["ai_calls"] = len(await self.api._load("ai_usage", []))
        posts = await self._safe_get("/posts", [])
        self.channels = await self._safe_get("/channels", self.channels)
        names = {c["id"]: c["title"] for c in self.channels}

        recent = [p for p in posts if p["status"] == "published"][:3]

        children = [
            self.gradient_header(
                "سلام 👋",
                subtitle=jdate(dt.datetime.now()),
                actions=[self._header_icon(ft.Icons.CAMPAIGN)],
            ),
            ft.Container(height=24),

            # ---- stats grid ----
            ft.Container(
                content=ft.Row([
                    self._stat_tile("کانال‌ها", stats.get("channels", 0),
                                    ft.Icons.CAMPAIGN, Palette.PRIMARY),
                    self._stat_tile("منتشرشده", stats.get("published", 0),
                                    ft.Icons.CHECK_CIRCLE, Palette.SUCCESS),
                    self._stat_tile("پیش‌نویس", stats.get("drafts", 0),
                                    ft.Icons.EDIT_NOTE, Palette.PRIMARY),
                ], spacing=12),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(
                content=ft.Row([
                    self._stat_tile("زمان‌بندی", stats.get("scheduled", 0),
                                    ft.Icons.SCHEDULE, Palette.WARNING),
                    self._stat_tile("ناموفق", stats.get("failed", 0),
                                    ft.Icons.ERROR_OUTLINE, Palette.DANGER),
                    self._stat_tile("درخواست AI", stats.get("ai_calls", 0),
                                    ft.Icons.AUTO_AWESOME, "#6D4AFF"),
                ], spacing=12),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(height=20),
            self._parse_web_card(),
            ft.Container(height=28),
        ]

        # ---- recent posts ----
        if recent:
            children.append(ft.Container(
                content=T("پست‌های اخیر", size=16,
                          weight=ft.FontWeight.BOLD,
                          color=Palette.TEXT_LIGHT),
                padding=ft.Padding.only(left=20, right=20, bottom=12),
            ))
            children.extend(self._post_summary(p, names) for p in recent)
            children.append(ft.Container(height=16))
        else:
            children.append(ft.Container(
                content=ft.Column([
                    ft.Container(
                        content=ft.Icon(ft.Icons.ARTICLE_OUTLINED,
                                        size=40, color=Palette.MUTED),
                        width=80, height=80, border_radius=24,
                        alignment=ft.Alignment(0, 0),
                        bgcolor=ft.Colors.with_opacity(0.08, Palette.PRIMARY),
                    ),
                    T("هنوز پستی منتشر نشده", size=15,
                      weight=ft.FontWeight.W_600, color=Palette.TEXT),
                    T("از دکمه + شروع کن", size=12, color=Palette.MUTED),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                   spacing=8),
                padding=ft.Padding.symmetric(vertical=40),
                alignment=ft.Alignment(0, 0),
            ))

        return ft.Column(
            children,
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    def _parse_web_card(self):
        """Small promotional / tip card shown on the home screen."""
        return ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Icon(ft.Icons.LIGHTBULB_OUTLINE,
                                    size=22, color=Palette.PRIMARY),
                    width=44, height=44, border_radius=14,
                    alignment=ft.Alignment(0, 0),
                    bgcolor=Palette.PRIMARY_SOFT,
                ),
                ft.Column([
                    T("نکته روز", size=13, weight=ft.FontWeight.W_600,
                      color=Palette.TEXT),
                    T("از دستیار AI برای بازنویسی و ساخت عنوان "
                      "استفاده کن تا پست‌ها جذاب‌تر شوند.",
                      size=12, color=Palette.MUTED),
                ], spacing=2, expand=True),
            ], spacing=12),
            padding=16,
            margin=ft.Margin.symmetric(horizontal=16),
            border_radius=18,
            bgcolor=Palette.CARD,
            border=ft.Border.all(1, Palette.BORDER),
        )

    async def _open_external(self, url: str):
        if not url:
            return
        try:
            await self.url_launcher.launch_url(url)
        except Exception:
            self.toast("باز کردن لینک ممکن نشد.", error=True)

    def _is_dark(self) -> bool:
        mode = self.theme_mode
        if mode == "dark":
            return True
        if mode == "light":
            return False
        brightness = getattr(self.page, "platform_brightness", None)
        return brightness == ft.Brightness.DARK

    def _markdown_style_sheet(self):
        dark = self._is_dark()
        text_color = Palette.TEXT
        muted = Palette.MUTED
        link = Palette.PRIMARY
        code_color = "#E2E8F0" if dark else "#1E293B"
        inline_code_bg = "#1E293B" if dark else "#F1F5F9"
        code_bg = "#0B1220" if dark else "#0F172A"
        code_fg = "#E2E8F0" if dark else "#F1F5F9"
        base = ft.TextStyle(size=14, color=text_color)

        return ft.MarkdownStyleSheet(
            p_text_style=base,
            a_text_style=ft.TextStyle(
                size=14, color=link, weight=ft.FontWeight.W_500,
                decoration=ft.TextDecoration.UNDERLINE,
                decoration_color=link,
            ),
            em_text_style=ft.TextStyle(italic=True, color=text_color),
            strong_text_style=ft.TextStyle(
                weight=ft.FontWeight.BOLD, color=text_color,
            ),
            code_text_style=ft.TextStyle(
                font_family="monospace",
                size=12.5,
                color=code_color,
                bgcolor=inline_code_bg,
            ),
            h1_text_style=ft.TextStyle(
                size=22, weight=ft.FontWeight.BOLD, color=text_color,
            ),
            h2_text_style=ft.TextStyle(
                size=19, weight=ft.FontWeight.BOLD, color=text_color,
            ),
            h3_text_style=ft.TextStyle(
                size=17, weight=ft.FontWeight.BOLD, color=text_color,
            ),
            h4_text_style=ft.TextStyle(
                size=15, weight=ft.FontWeight.BOLD, color=text_color,
            ),
            h5_text_style=ft.TextStyle(
                size=14, weight=ft.FontWeight.BOLD, color=text_color,
            ),
            h6_text_style=ft.TextStyle(
                size=13, weight=ft.FontWeight.BOLD, color=muted,
            ),
            blockquote_text_style=ft.TextStyle(
                color=muted, italic=True,
            ),
            blockquote_decoration=ft.BoxDecoration(
                border=ft.Border.only(
                    left=ft.BorderSide(3, Palette.PRIMARY)
                ),
                bgcolor=("#111827" if dark else "#F8FAFC"),
            ),
            blockquote_padding=ft.Padding.symmetric(
                horizontal=12, vertical=8,
            ),
            codeblock_padding=12,
            codeblock_decoration=ft.BoxDecoration(
                bgcolor=code_bg,
                border_radius=10,
            ),
            block_spacing=8,
            list_bullet_text_style=base,
        )

    def _markdown_widget(self, text: str) -> ft.Control:
        """A theme-aware Markdown view for assistant prose."""
        dark = self._is_dark()
        return ft.Markdown(
            value=text,
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme=(
                ft.MarkdownCodeTheme.ATOM_ONE_DARK
                if dark else ft.MarkdownCodeTheme.GITHUB
            ),
            auto_follow_links=False,
            on_tap_link=lambda e: self.spawn(self._open_external(e.data)),
            md_style_sheet=self._markdown_style_sheet(),
        )

    def _split_markdown(self, text: str):
        """Split assistant text into prose / fenced-code segments."""
        segments: list[tuple[str, str]] = []
        last = 0
        for m in self._CODE_FENCE.finditer(text):
            if m.start() > last:
                segments.append(("prose", text[last:m.start()]))
            lang = m.group(1) or ""
            code = m.group(2)
            segments.append(("code", code if not lang else f"{lang}\n{code}"))
            last = m.end()
        if last < len(text):
            segments.append(("prose", text[last:]))
        return segments or [("prose", text)]

    def _code_block(self, code: str) -> ft.Control:
        dark = self._is_dark()
        code_bg = "#0B1220" if dark else "#0F172A"
        code_fg = "#E2E8F0" if dark else "#F1F5F9"

        async def copy_code(_=None):
            try:
                clip = getattr(self.page, "clipboard", None)
                setter = getattr(clip, "set", None) if clip is not None else None
                if callable(setter):
                    result = setter(code)
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    page_setter = getattr(self.page, "set_clipboard", None)
                    if callable(page_setter):
                        result = page_setter(code)
                        if asyncio.iscoroutine(result):
                            await result
                self.toast("کپی شد")
            except Exception:
                self.toast("کپی ممکن نشد", error=True)

        return ft.Container(
            content=ft.Column([
                ft.Row([
                    T("کد", size=11, color=Palette.MUTED),
                    ft.Container(expand=True),
                    ft.IconButton(
                        icon=ft.Icons.CONTENT_COPY,
                        icon_size=16,
                        icon_color=Palette.MUTED,
                        tooltip="کپی",
                        on_click=lambda e: self.spawn(copy_code()),
                    ),
                ], spacing=4),
                ft.SelectionArea(
                    content=T(code, size=12.5, color=code_fg,
                              font_family="monospace",
                              selectable=True),
                ),
            ], spacing=4, tight=True),
            padding=12,
            border_radius=12,
            bgcolor=code_bg,
            border=ft.Border.all(1, "#1F2A3D" if dark else Palette.BORDER),
        )

    def _assistant_message_body(self, text: str) -> ft.Control:
        segments = self._split_markdown(text)
        controls = []
        for kind, payload in segments:
            if kind == "code":
                controls.append(self._code_block(payload))
            else:
                stripped = payload.strip()
                if stripped:
                    controls.append(self._markdown_widget(stripped))
        if not controls:
            controls.append(self._markdown_widget(text))
        return ft.Column(controls, spacing=8, tight=True)

    def _stat_tile(self, label, value, icon, color):
        return ft.Container(
            content=ft.Column([
                ft.Container(
                    content=ft.Icon(icon, size=20, color=color),
                    width=42, height=42, border_radius=14,
                    alignment=ft.Alignment(0, 0),
                    bgcolor=ft.Colors.with_opacity(0.13, color),
                ),
                ft.Container(height=10),
                T(digits(value), size=24, weight=ft.FontWeight.W_700,
                  color=Palette.TEXT),
                T(label, size=12, color=Palette.MUTED, weight=ft.FontWeight.W_500),
            ], spacing=0, horizontal_alignment=ft.CrossAxisAlignment.START),
            padding=ft.Padding.symmetric(horizontal=14, vertical=16),
            border_radius=20,
            bgcolor=Palette.CARD,
            border=ft.Border.all(1, Palette.BORDER),
            shadow=ft.BoxShadow(
                blur_radius=18,
                spread_radius=-2,
                color=ft.Colors.with_opacity(0.07, "#0F172A"),
                offset=ft.Offset(0, 6),
            ),
            expand=True,
        )

    def _post_summary(self, post: dict, names: dict):
        status = post["status"]
        color = ST_COLOR.get(status, Palette.MUTED)
        channel_names = "، ".join(
            names.get(d["channel_id"], "") for d in post.get("deliveries", [])
        ) or "—"
        return ft.Container(
            content=ft.Container(
                content=ft.Column([
                    ft.Row([
                        T(post["title"] or post["text"][:40],
                          size=14, weight=ft.FontWeight.W_600,
                          color=Palette.TEXT, expand=True,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        pill(ST.get(status, status), color),
                    ]),
                    T(channel_names, size=12, color=Palette.MUTED,
                      max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=8, tight=True),
                padding=16,
                border_radius=18,
                bgcolor=Palette.CARD,
                border=ft.Border.all(1, Palette.BORDER),
                shadow=ft.BoxShadow(
                    blur_radius=16,
                    spread_radius=-1,
                    color=ft.Colors.with_opacity(0.06, "#0F172A"),
                    offset=ft.Offset(0, 4),
                ),
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=5),
            ink=True,
            on_click=lambda e: self.spawn(self.go("posts")),
        )

    async def _safe_get(self, path, default):
        try:
            return await self.api.get(path)
        except ApiError:
            return default

    async def _persist_ai_preference(self, name: str, value: str):
        try:
            await self.api._store_verified(
                f"ai_preference_{name}", value or ""
            )
        except Exception:
            pass

    async def v_posts(self, flt: str = "all", q: str = ""):
        posts = await self._safe_get("/posts", [])
        self.channels = await self._safe_get("/channels", self.channels)
        names = {c["id"]: c["title"] for c in self.channels}

        if flt != "all":
            posts = [p for p in posts if p.get("status") == flt]
        if q:
            ql = q.lower()
            posts = [
                p for p in posts
                if ql in (p.get("title") or "").lower()
                or ql in (p.get("text") or "").lower()
            ]

        filters = [
            ("all", "همه"),
            ("draft", "پیش‌نویس"),
            ("scheduled", "زمان‌بندی"),
            ("published", "منتشرشده"),
            ("failed", "ناموفق"),
        ]

        filter_row = ft.Row([
            ft.Container(
                content=T(label, size=12,
                          color="#FFFFFF" if flt == key else Palette.TEXT_LIGHT,
                          weight=ft.FontWeight.W_600),
                padding=ft.Padding.symmetric(horizontal=14, vertical=8),
                border_radius=20,
                bgcolor=Palette.PRIMARY if flt == key else Palette.CARD,
                border=ft.Border.all(
                    1, Palette.PRIMARY if flt == key else Palette.BORDER
                ),
                ink=True,
                on_click=lambda e, k=key: self.spawn(self.go("posts", k, q)),
            )
            for key, label in filters
        ], spacing=8, scroll=ft.ScrollMode.AUTO)

        search = tf("جست‌وجو در پست‌ها...", value=q)
        search.on_submit = lambda e: self.spawn(
            self.go("posts", flt, (search.value or "").strip())
        )

        header = self.gradient_header(
            "پست‌ها",
            subtitle=digits(f"{len(posts)} مورد"),
            actions=[
                self._header_icon(
                    ft.Icons.ADD,
                    on_click=lambda e: self.spawn(self.go("new")),
                ),
            ],
        )

        body = [header, ft.Container(height=12)]
        body.append(ft.Container(content=filter_row, padding=ft.Padding.symmetric(horizontal=16)))
        body.append(ft.Container(height=10))
        body.append(ft.Container(content=search, padding=ft.Padding.symmetric(horizontal=16)))
        body.append(ft.Container(height=14))

        if not posts:
            body.append(empty_state(
                ft.Icons.ARTICLE_OUTLINED,
                "پستی پیدا نشد",
                "پیش‌نویس جدید بساز یا فیلتر را تغییر بده",
                action=btn_primary("ساخت پست", lambda e: self.spawn(self.go("new"))),
            ))
        else:
            for post in posts:
                body.append(self._post_card(post, names))

        body.append(ft.Container(height=80))
        return ft.Column(body, spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

    def _post_card(self, post: dict, names: dict):
        status = post.get("status", "draft")
        color = ST_COLOR.get(status, Palette.MUTED)
        channel_names = "، ".join(
            names.get(d["channel_id"], "") for d in post.get("deliveries", [])
        ) or "—"
        text_preview = (post.get("text") or "")[:120]
        title = post.get("title") or text_preview[:40] or "بدون عنوان"

        body = [
            ft.Row([
                T(title, size=15, weight=ft.FontWeight.W_600,
                  color=Palette.TEXT, expand=True,
                  max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                pill(ST.get(status, status), color),
            ]),
            T(text_preview, size=13, color=Palette.MUTED,
              max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            T(channel_names, size=11, color=Palette.MUTED),
        ]

        if status in CANCELLABLE:
            body.append(ft.Row([
                ft.TextButton(
                    "لغو",
                    icon=ft.Icons.CLOSE,
                    on_click=lambda e, i=post["id"]: self.spawn(
                        self._cancel_post(i)),
                ),
            ], alignment=ft.MainAxisAlignment.END))
        elif status == "draft":
            body.append(ft.Row([
                ft.TextButton(
                    "ویرایش",
                    icon=ft.Icons.EDIT_OUTLINED,
                    on_click=lambda e, i=post["id"]: self.spawn(
                        self.go("new", i)),
                ),
                ft.TextButton(
                    "حذف پیش‌نویس",
                    icon=ft.Icons.DELETE_OUTLINE,
                    on_click=lambda e, i=post["id"]: self.spawn(
                        self._delete_draft(i)),
                ),
            ], alignment=ft.MainAxisAlignment.END))
        else:
            actions = [
                ft.TextButton(
                    "جزئیات",
                    icon=ft.Icons.INFO_OUTLINE,
                    on_click=lambda e, p=post: self.spawn(
                        self._show_post_details(p)
                    ),
                ),
            ]
            if status == "published":
                actions.append(ft.TextButton(
                    "نسخه اصلاحی",
                    icon=ft.Icons.CONTENT_COPY,
                    on_click=lambda e, p=post: self.spawn(
                        self._corrected_copy(p)
                    ),
                ))
            body.append(ft.Row(
                actions, alignment=ft.MainAxisAlignment.END
            ))

        return ft.Container(
            content=ft.Container(
                content=ft.Column(body, spacing=8, tight=True),
                padding=16,
                border_radius=18,
                bgcolor=Palette.CARD_LIGHT,
                shadow=ft.BoxShadow(blur_radius=14, color=ft.Colors.with_opacity(0.06, "#0F172A"),
                                    offset=ft.Offset(0, 3)),
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=6),
        )

    async def _show_post_details(self, post: dict):
        revisions = post.get("revisions", [])
        if self.api.online:
            try:
                revisions = await self.api.get(
                    f"/posts/{post['id']}/revisions"
                )
            except ApiError:
                revisions = []
        delivery_lines = []
        for delivery in post.get("deliveries", []):
            channel = next(
                (item for item in self.channels
                 if item.get("id") == delivery.get("channel_id")),
                {},
            )
            message_id = delivery.get("remote_message_id")
            details = channel.get("title") or "—"
            if message_id:
                details += f"  ·  شناسه پیام: {digits(message_id)}"
            delivery_lines.append(T(details, size=12, color=Palette.MUTED))

        rev_widgets = []
        for rev in (revisions or [])[-5:]:
            rev_widgets.append(
                T(
                    f"نسخه {digits(rev.get('version', '?'))} — "
                    f"{(rev.get('text') or '')[:80]}",
                    size=12, color=Palette.MUTED,
                )
            )

        self.page.show_dialog(ft.AlertDialog(
            title=T(post.get("title") or "جزئیات پست", size=16,
                    weight=ft.FontWeight.BOLD),
            content=ft.Column([
                pill(ST.get(post.get("status"), post.get("status")),
                     ST_COLOR.get(post.get("status"), Palette.MUTED)),
                T((post.get("text") or "")[:400], size=13,
                  color=Palette.TEXT),
                ft.Divider(height=12),
                T("ارسال‌ها", size=13, weight=ft.FontWeight.W_600),
                *(delivery_lines or [T("—", size=12, color=Palette.MUTED)]),
                ft.Divider(height=12) if rev_widgets else ft.Container(),
                T("نسخه‌های قبلی", size=13,
                  weight=ft.FontWeight.W_600) if rev_widgets else ft.Container(),
                *rev_widgets,
            ], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO,
               height=320),
            actions=[
                ft.TextButton("بستن",
                              on_click=lambda e: self.page.pop_dialog()),
            ],
        ))

    async def _cancel_post(self, post_id: int):
        if not await self.confirm("این پست لغو شود؟"):
            return
        try:
            await self.api.post(f"/posts/{post_id}/cancel")
            self.toast("لغو شد")
            await self.go("posts")
        except ApiError as e:
            self.toast(str(e), error=True)

    async def _delete_draft(self, post_id: int):
        if not await self.confirm("پیش‌نویس حذف شود؟"):
            return
        try:
            await self.api.delete(f"/posts/{post_id}")
            self.toast("حذف شد")
            await self.go("posts")
        except ApiError as e:
            self.toast(str(e), error=True)

    async def _corrected_copy(self, post: dict):
        try:
            await self.api.post(f"/posts/{post['id']}/corrected-copy")
            self.toast("نسخه اصلاحی به‌عنوان پیش‌نویس ساخته شد")
            await self.go("posts", "draft")
        except ApiError as e:
            self.toast(str(e), error=True)

    async def v_channel_history(self, channel_id: int):
        channel = next(
            (c for c in self.channels if c.get("id") == channel_id), None
        )
        title = channel.get("title") if channel else "تاریخچه کانال"
        try:
            posts = await self.api.get("/history", params={
                "channel_id": channel_id,
                "status": "all",
                "limit": 200,
                "offset": 0,
            })
        except ApiError:
            posts = []
        children = [
            self.gradient_header(
                title or "تاریخچه",
                actions=[
                    self._header_icon(
                        ft.Icons.OPEN_IN_NEW,
                        on_click=lambda e: self.spawn(self._open_external(
                            "https://eitaa.com/" + str(channel.get("chat_id"))
                        )) if channel and channel.get("chat_id") else None,
                    ),
                    self._header_icon(
                        ft.Icons.ARROW_FORWARD,
                        on_click=lambda e: self.spawn(self.go("posts")),
                    ),
                ],
            ),
            ft.Container(height=12),
        ]
        if not posts:
            children.append(empty_state(
                ft.Icons.HISTORY,
                "سابقه‌ای ثبت نشده",
                "فقط پیام‌هایی که از این برنامه ارسال شده‌اند اینجا دیده می‌شوند",
            ))
        else:
            for item in posts:
                children.append(ft.Container(
                    content=ft.Column([
                        T(item.get("text", "")[:160], size=13,
                          color=Palette.TEXT, max_lines=3,
                          overflow=ft.TextOverflow.ELLIPSIS),
                        T(item.get("created_at", ""), size=11,
                          color=Palette.MUTED),
                    ], spacing=4),
                    padding=14,
                    margin=ft.Margin.symmetric(horizontal=16, vertical=4),
                    border_radius=16,
                    bgcolor=Palette.CARD,
                    border=ft.Border.all(1, Palette.BORDER),
                ))
        children.append(ft.Container(height=40))
        return ft.Column(children, spacing=0, scroll=ft.ScrollMode.AUTO,
                         expand=True)

    async def v_reminders(self):
        items = await self._safe_get("/reminders", [])
        children = [
            self.gradient_header(
                "یادآوری‌ها",
                subtitle="حذف دستی پیام‌های منتشرشده",
                actions=[
                    self._header_icon(
                        ft.Icons.ARROW_FORWARD,
                        on_click=lambda e: self.spawn(self.go("settings")),
                    ),
                ],
            ),
            ft.Container(height=16),
        ]
        if not items:
            children.append(empty_state(
                ft.Icons.NOTIFICATIONS_NONE,
                "یادآوری‌ای نیست",
                "برای پیام‌های منتشرشده می‌توانی یادآوری بررسی تنظیم کنی",
            ))
        else:
            for item in items:
                children.append(ft.Container(
                    content=ft.Column([
                        T(item.get("note") or "یادآوری", size=14,
                          weight=ft.FontWeight.W_600, color=Palette.TEXT),
                        T(item.get("due_at", ""), size=12,
                          color=Palette.MUTED),
                    ], spacing=4),
                    padding=14,
                    margin=ft.Margin.symmetric(horizontal=16, vertical=4),
                    border_radius=16,
                    bgcolor=Palette.CARD,
                    border=ft.Border.all(1, Palette.BORDER),
                ))
        children.append(ft.Container(height=40))
        return ft.Column(children, spacing=0, scroll=ft.ScrollMode.AUTO,
                         expand=True)

    # ------------------------------------------------------------- chat ----
    async def v_chat(self):
        self.chat_messages = await self.api._load("chat_history", []) or []
        if not isinstance(self.chat_messages, list):
            self.chat_messages = []

        list_view = ft.ListView(expand=True, spacing=10, padding=16,
                                auto_scroll=True)

        def rebuild():
            list_view.controls.clear()
            for msg in self.chat_messages[-config.CHAT_HISTORY_LIMIT:]:
                is_user = msg.get("role") == "user"
                bubble_bg = Palette.PRIMARY if is_user else Palette.CARD
                bubble_fg = "#FFFFFF" if is_user else Palette.TEXT
                content = msg.get("text") or ""
                if is_user:
                    body = T(content, size=14, color=bubble_fg,
                             selectable=True)
                    bubble_content = (
                        ft.Column([
                            body,
                            ft.TextButton(
                                "تلاش دوباره",
                                on_click=lambda e, value=content: self.spawn(
                                    send(retry_text=value)
                                ),
                            ),
                        ], spacing=2, tight=True)
                        if msg.get("status") == "failed" else body
                    )
                else:
                    body = self._assistant_message_body(content)
                    if msg.get("status") == "typing":
                        bubble_content = ft.Row([
                            ft.ProgressRing(width=16, height=16, stroke_width=2,
                                            color=Palette.PRIMARY),
                            T("در حال نوشتن...", size=13, color=Palette.MUTED),
                        ], spacing=8, tight=True)
                    elif msg.get("status") == "streaming":
                        bubble_content = ft.Column([
                            body,
                            ft.Row([
                                ft.ProgressRing(width=12, height=12, stroke_width=1.5,
                                                color=Palette.PRIMARY),
                                T("در حال نوشتن...", size=11, color=Palette.MUTED),
                            ], spacing=6, tight=True),
                        ], spacing=6, tight=True)
                    else:
                        bubble_content = body
                list_view.controls.append(
                    ft.Row([
                        ft.Container(
                            content=bubble_content,
                            padding=ft.Padding.symmetric(
                                horizontal=14, vertical=10
                            ),
                            border_radius=18,
                            bgcolor=bubble_bg,
                            border=None if is_user else ft.Border.all(
                                1, Palette.BORDER
                            ),
                            shadow=ft.BoxShadow(
                                blur_radius=10,
                                color=ft.Colors.with_opacity(0.06, "#0F172A"),
                                offset=ft.Offset(0, 3),
                            ) if not is_user else None,
                            width=(
                                min(320, self.page.width * 0.82)
                                if isinstance(self.page.width, (int, float))
                                and self.page.width > 0 else 280
                            ),
                        ),
                    ], alignment=(
                        ft.MainAxisAlignment.END if is_user
                        else ft.MainAxisAlignment.START
                    )),
                )
            self.page.update()

        rebuild()

        prompt = tf("پیام خود را بنویس...", multiline=True, min_lines=1,
                    max_lines=4)
        chat_state = {"sending": False}

        async def send(_=None, retry_text: str | None = None):
            if chat_state["sending"]:
                return
            text = (retry_text if retry_text is not None else prompt.value or "").strip()
            if not text:
                return
            chat_state["sending"] = True

            retrying = (
                retry_text is not None
                and bool(self.chat_messages)
                and self.chat_messages[-1].get("role") == "user"
                and self.chat_messages[-1].get("status") == "failed"
                and self.chat_messages[-1].get("text") == text
            )
            if retrying:
                user_message = self.chat_messages[-1]
                user_message["status"] = "sending"
            else:
                user_message = {
                    "role": "user", "text": text, "status": "sending",
                }
                self.chat_messages.append(user_message)

            prompt.value = ""
            assistant_message = {
                "role": "assistant", "text": "", "status": "typing",
            }
            self.chat_messages.append(assistant_message)
            rebuild()
            try:
                reply = await self.api.ai("create", text, prompt=text)
            except ApiError as e:
                # Keep the user's text recoverable and offer a one-tap retry.
                prompt.value = text
                user_message["status"] = "failed"
                if assistant_message in self.chat_messages:
                    self.chat_messages.remove(assistant_message)
                chat_state["sending"] = False
                rebuild()
                try:
                    await self.api._store_verified(
                        "chat_history",
                        self.chat_messages[-config.CHAT_HISTORY_LIMIT:],
                    )
                except Exception:
                    pass
                self.toast(str(e), error=True)
                return
            except Exception:
                logger.exception("Unexpected AI chat failure")
                prompt.value = text
                user_message["status"] = "failed"
                if assistant_message in self.chat_messages:
                    self.chat_messages.remove(assistant_message)
                chat_state["sending"] = False
                rebuild()
                self.toast(
                    "ارتباط با دستیار ناموفق بود؛ متن درخواست حفظ شد.",
                    error=True,
                )
                return

            user_message["status"] = "ready"
            assistant_message["status"] = "streaming"
            # Reveal the answer in small chunks to avoid a sudden full-text jump.
            chunk_size = 72
            for end in range(chunk_size, len(reply), chunk_size):
                assistant_message["text"] = reply[:end]
                rebuild()
                await asyncio.sleep(0.025)
            assistant_message["text"] = reply
            assistant_message["status"] = "ready"
            chat_state["sending"] = False
            try:
                await self.api._store_verified(
                    "chat_history",
                    self.chat_messages[-config.CHAT_HISTORY_LIMIT:],
                )
            except Exception:
                pass
            rebuild()

        async def save_as_draft(_=None):
            if not self.chat_messages:
                return self.toast("پیامی برای ذخیره نیست", error=True)
            last = next(
                (m for m in reversed(self.chat_messages)
                 if m.get("role") == "assistant"),
                None,
            )
            if not last:
                return self.toast("پاسخ دستیار پیدا نشد", error=True)
            try:
                await self.api.post("/posts", {
                    "title": "",
                    "text": last["text"],
                    "status": "draft",
                })
                self.toast("به‌عنوان پیش‌نویس ذخیره شد")
            except ApiError as e:
                self.toast(str(e), error=True)

        header = self.gradient_header(
            "دستیار هوشمند",
            subtitle="ساخت و بازنویسی محتوا",
            actions=[
                self._header_icon(
                    ft.Icons.SAVE_ALT,
                    on_click=lambda e: self.spawn(save_as_draft()),
                ),
            ],
        )

        return ft.Column([
            header,
            list_view,
            ft.Container(
                content=ft.Row([
                    ft.Container(content=prompt, expand=True),
                    ft.IconButton(
                        icon=ft.Icons.SEND_ROUNDED,
                        icon_color="#FFFFFF",
                        bgcolor=Palette.PRIMARY,
                        on_click=lambda e: self.spawn(send()),
                    ),
                ], spacing=8),
                padding=ft.Padding.only(
                    left=14, right=14, top=10, bottom=14
                ),
                bgcolor=Palette.SURFACE_LIGHT,
            ),
        ], spacing=0, expand=True)

    # -------------------------------------------------------- new post -----
    async def v_new(self, post_id: int | None = None):
        post = None
        if post_id is not None:
            posts = await self.api.get("/posts")
            post = next((p for p in posts if p.get("id") == post_id), None)

        title_f = tf("عنوان (اختیاری)", value=(post or {}).get("title", ""))
        body_f = tf(
            "متن پست",
            value=(post or {}).get("text", ""),
            multiline=True,
            min_lines=6,
            max_lines=14,
        )

        # Channel selection
        self.channels = await self._safe_get("/channels", self.channels)
        state = {"selected": set()}
        if post:
            for d in post.get("deliveries", []):
                cid = d.get("channel_id")
                if cid is not None:
                    state["selected"].add(cid)

        channel_checks = []
        for ch in self.channels:
            def make_toggle(channel=ch):
                def on_change(e):
                    if e.control.value:
                        state["selected"].add(channel["id"])
                    else:
                        state["selected"].discard(channel["id"])
                    _refresh_preview()
                return on_change
            channel_checks.append(
                ft.Checkbox(
                    label=ch["title"],
                    value=ch["id"] in state["selected"],
                    on_change=make_toggle(),
                )
            )

        # Schedule
        date_f = tf("تاریخ (YYYY-MM-DD)", hint_text="مثلاً 2026-10-20")
        time_f = tf("ساعت (HH:MM)", hint_text="مثلاً 14:30")

        # AI quick actions
        ai_status = ft.Row([
            ft.ProgressRing(width=16, height=16, stroke_width=2,
                            color=Palette.PRIMARY),
            T("هوش مصنوعی در حال نوشتن است...", size=12,
              color=Palette.PRIMARY),
        ], spacing=8, tight=True, visible=False)
        ai_busy = {"running": False}
        ai_row = ft.Row([
            ft.Container(
                content=T(label, size=12, color=Palette.PRIMARY,
                          weight=ft.FontWeight.W_600),
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                border_radius=14,
                border=ft.Border.all(1, Palette.BORDER),
                bgcolor=ft.Colors.with_opacity(0.04, Palette.PRIMARY),
                ink=True,
                on_click=lambda e, a=action: self.spawn(
                    _run_ai(a)
                ),
            )
            for action, label, _ in AI_ACTIONS[:6]
        ], spacing=8, scroll=ft.ScrollMode.AUTO)

        preview = ft.Container(
            content=T("پیش‌نمایش اینجا نمایش داده می‌شود", size=13,
                      color=Palette.MUTED),
            padding=16,
            border_radius=16,
            bgcolor=Palette.CARD,
            border=ft.Border.all(1, Palette.BORDER),
        )

        def _refresh_preview(_=None):
            text = (body_f.value or "").strip()
            title = (title_f.value or "").strip()
            preview.content = ft.Column([
                T(title or "بدون عنوان", size=15,
                  weight=ft.FontWeight.W_600, color=Palette.TEXT),
                T(text or "متن خالی است", size=13, color=Palette.MUTED),
            ], spacing=6, tight=True)
            self.page.update()

        body_f.on_change = _refresh_preview
        title_f.on_change = _refresh_preview

        async def _run_ai(action: str):
            if ai_busy["running"]:
                return
            src = (body_f.value or "").strip() or (title_f.value or "").strip()
            if not src and action != "create":
                return self.toast("متنی برای پردازش وارد کنید", error=True)
            ai_busy["running"] = True
            ai_status.visible = True
            self.page.update()
            try:
                result = await self.api.ai(action, src)
                # Keep the generated text readable as it is revealed rather
                # than replacing the whole editor in one sudden jump.
                body_f.value = ""
                _refresh_preview()
                chunk_size = 48
                for end in range(chunk_size, len(result), chunk_size):
                    body_f.value = result[:end]
                    _refresh_preview()
                    await asyncio.sleep(0.02)
                body_f.value = result
                _refresh_preview()
                self.toast("آماده شد")
            except ApiError as e:
                self.toast(str(e), error=True)
            except Exception:
                logger.exception("AI composer action failed")
                self.toast("ارتباط با هوش مصنوعی ناموفق بود؛ متن قبلی حفظ شد.", error=True)
            finally:
                ai_busy["running"] = False
                ai_status.visible = False
                self.page.update()

        async def save_draft(_=None):
            text = (body_f.value or "").strip()
            if not text:
                return self.toast("متن پست خالی است", error=True)
            payload = {
                "title": (title_f.value or "").strip(),
                "text": text,
                "mode": "draft",
                "channel_ids": list(state["selected"]),
            }
            try:
                if post_id is not None:
                    await self.api.put(f"/posts/{post_id}", payload)
                else:
                    await self.api.post("/posts", payload)
                self.toast("پیش‌نویس ذخیره شد")
                await self.go("posts", "draft")
            except ApiError as e:
                self.toast(str(e), error=True)

        async def publish_now(_=None):
            text = (body_f.value or "").strip()
            if not text:
                return self.toast("متن پست خالی است", error=True)
            if not state["selected"]:
                return self.toast("حداقل یک کانال انتخاب کنید", error=True)
            payload = {
                "title": (title_f.value or "").strip(),
                "text": text,
                "mode": "now",
                "channel_ids": list(state["selected"]),
            }
            try:
                if post_id is not None:
                    await self.api.put(f"/posts/{post_id}", payload)
                else:
                    await self.api.post("/posts", payload)
                self.toast("درخواست انتشار ثبت شد؛ وضعیت را در فهرست پست‌ها ببینید.")
                await self.go("posts")
            except ApiError as e:
                self.toast(str(e), error=True)

        async def schedule(_=None):
            text = (body_f.value or "").strip()
            if not text:
                return self.toast("متن پست خالی است", error=True)
            if not state["selected"]:
                return self.toast("حداقل یک کانال انتخاب کنید", error=True)
            when = combine_local(
                (date_f.value or "").strip(),
                (time_f.value or "").strip(),
            )
            if when is None:
                return self.toast("تاریخ و ساعت معتبر وارد کنید", error=True)
            payload = {
                "title": (title_f.value or "").strip(),
                "text": text,
                "mode": "schedule",
                "channel_ids": list(state["selected"]),
                "scheduled_at": to_utc_iso(when),
            }
            try:
                if post_id is not None:
                    await self.api.put(f"/posts/{post_id}", payload)
                else:
                    await self.api.post("/posts", payload)
                self.toast(
                    "زمان‌بندی شد" if self.api.online else
                    "زمان‌بندی ثبت شد؛ برای اجرای خودکار، برنامه باید باز بماند."
                )
                await self.go("posts", "scheduled")
            except ApiError as e:
                self.toast(str(e), error=True)

        header = self.gradient_header(
            "ویرایش پست" if post else "پست جدید",
            actions=[
                self._header_icon(
                    ft.Icons.ARROW_FORWARD,
                    on_click=lambda e: self.spawn(self.go("posts")),
                ),
            ],
        )

        return ft.Column([
            header,
            ft.Container(
                content=ft.Column([
                    title_f,
                    body_f,
                    T("ابزارهای هوشمند", size=13,
                      weight=ft.FontWeight.W_600, color=Palette.TEXT),
                    ai_status,
                    ai_row,
                    T("کانال‌ها", size=13,
                      weight=ft.FontWeight.W_600, color=Palette.TEXT),
                    *(channel_checks or [
                        T("کانالی ثبت نشده — از تنظیمات اضافه کنید",
                          size=12, color=Palette.MUTED)
                    ]),
                    T("زمان‌بندی (اختیاری)", size=13,
                      weight=ft.FontWeight.W_600, color=Palette.TEXT),
                    ft.Row([date_f, time_f], spacing=10),
                    T("پیش‌نمایش", size=13,
                      weight=ft.FontWeight.W_600, color=Palette.TEXT),
                    preview,
                    ft.Container(height=8),
                    btn_primary("انتشار فوری",
                                lambda e: self.spawn(publish_now())),
                    btn_ghost("زمان‌بندی",
                              lambda e: self.spawn(schedule())),
                    btn_ghost("ذخیره پیش‌نویس",
                              lambda e: self.spawn(save_draft())),
                ], spacing=12, scroll=ft.ScrollMode.AUTO),
                padding=16,
                expand=True,
            ),
        ], spacing=0, expand=True)

    async def v_settings(self):
        theme_mode = await self.api.storage.get("appearance_theme") or "system"
        accent_name = await self.api.storage.get("appearance_accent") or "آبی"

        async def set_theme(mode: str):
            await self.api.storage.set("appearance_theme", mode)
            self.apply_appearance(mode, self.accent_name)
            await self.go("settings")

        async def set_accent(name: str):
            await self.api.storage.set("appearance_accent", name)
            self.apply_appearance(self.theme_mode, name)
            await self.go("settings")

        theme_options = ft.Row([
            self._chip("سیستم", theme_mode == "system",
                       lambda e: self.spawn(set_theme("system"))),
            self._chip("روشن", theme_mode == "light",
                       lambda e: self.spawn(set_theme("light"))),
            self._chip("تاریک", theme_mode == "dark",
                       lambda e: self.spawn(set_theme("dark"))),
        ], spacing=8, wrap=True)

        accent_options = ft.Row([
            self._chip(name, accent_name == name,
                       lambda e, n=name: self.spawn(set_accent(n)))
            for name in ACCENTS
        ], spacing=8, wrap=True)

        def tile(icon, title, subtitle, on_click, trailing=None):
            return ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Icon(icon, size=22, color=Palette.PRIMARY),
                        width=44, height=44, border_radius=14,
                        alignment=ft.Alignment(0, 0),
                        bgcolor=Palette.PRIMARY_SOFT,
                    ),
                    ft.Column([
                        T(title, size=14, weight=ft.FontWeight.W_600,
                          color=Palette.TEXT),
                        T(subtitle, size=12, color=Palette.MUTED),
                    ], spacing=2, expand=True),
                    trailing or ft.Icon(ft.Icons.CHEVRON_LEFT,
                                         color=Palette.MUTED, size=20),
                ], spacing=12),
                padding=14,
                border_radius=16,
                bgcolor=Palette.CARD,
                border=ft.Border.all(1, Palette.BORDER),
                ink=True,
                on_click=on_click,
            )

        children = [
            self.gradient_header("تنظیمات"),
            ft.Container(height=16),
            ft.Container(
                content=ft.Column([
                    T("ظاهر", size=13, weight=ft.FontWeight.W_600,
                      color=Palette.MUTED),
                    theme_options,
                    ft.Container(height=8),
                    T("رنگ اصلی", size=13, weight=ft.FontWeight.W_600,
                      color=Palette.MUTED),
                    accent_options,
                ], spacing=8),
                padding=16,
                margin=ft.Margin.symmetric(horizontal=16),
                border_radius=18,
                bgcolor=Palette.CARD,
                border=ft.Border.all(1, Palette.BORDER),
            ),
            ft.Container(height=16),
            ft.Container(
                content=ft.Column([
                    tile(
                        ft.Icons.PERSON_OUTLINE,
                        "پروفایل محلی",
                        "نام و تصویر نمایشی روی همین دستگاه",
                        lambda e: self.spawn(self._profile_dialog()),
                    ),
                    tile(
                        ft.Icons.CAMPAIGN_OUTLINED,
                        "کانال‌های من",
                        "مدیریت اتصالات و کانال‌ها",
                        lambda e: self.spawn(self.go("help")),
                    ),
                    tile(
                        ft.Icons.GROUPS_OUTLINED,
                        "اعضای تیم",
                        "نقش‌ها و دسترسی‌ها (نیاز به سرور)",
                        lambda e: self.spawn(self.go("team")),
                    ),
                    tile(
                        ft.Icons.AUTO_AWESOME,
                        "تنظیمات دستیار AI",
                        "ارائه‌دهنده و مدل",
                        lambda e: self.spawn(self.go("ai_settings")),
                    ),
                    tile(
                        ft.Icons.NOTIFICATIONS_OUTLINED,
                        "یادآوری‌ها",
                        "بررسی پیام‌های منتشرشده",
                        lambda e: self.spawn(self.go("reminders")),
                    ),
                    tile(
                        ft.Icons.SYSTEM_UPDATE_ALT,
                        "بررسی به‌روزرسانی",
                        f"نسخه فعلی: {config.VERSION}",
                        lambda e: self.spawn(self.go("updates")),
                    ) if config.FEATURES_UPDATE_CHECK else ft.Container(),
                    tile(
                        ft.Icons.FEEDBACK_OUTLINED,
                        "بازخورد",
                        "ارسال نظر ناشناس",
                        lambda e: self.spawn(self.go("feedback")),
                    ),
                    tile(
                        ft.Icons.HELP_OUTLINE,
                        "راهنما و اتصال",
                        "نحوه اتصال ربات و کانال",
                        lambda e: self.spawn(self.go("help")),
                    ),
                ], spacing=10),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(height=24),
            ft.Container(
                content=T(
                    f"{config.APP_NAME}  ·  v{config.VERSION}",
                    size=12, color=Palette.MUTED,
                    text_align=ft.TextAlign.CENTER,
                ),
                alignment=ft.Alignment(0, 0),
            ),
            ft.Container(height=80),
        ]
        return ft.Column(children, spacing=0, scroll=ft.ScrollMode.AUTO,
                         expand=True)

    def _chip(self, label, selected, on_click, disabled=False):
        return ft.Container(
            content=T(
                label, size=13,
                color="#FFFFFF" if selected else Palette.TEXT,
                weight=ft.FontWeight.W_600,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border_radius=20,
            bgcolor=Palette.PRIMARY if selected else Palette.CARD,
            border=ft.Border.all(
                1, Palette.PRIMARY if selected else Palette.BORDER
            ),
            ink=not disabled,
            on_click=None if disabled else on_click,
            opacity=0.5 if disabled else 1,
        )

    # ----------------------------------------------------- profile dialog --
    async def _profile_dialog(self):
        if not config.FEATURES_LOCAL_PROFILE:
            self.toast("ویرایش پروفایل در این نسخه فعال نیست.", error=True)
            return
        profile = await self.api.get_profile()
        name_field = tf(
            "نام نمایشی",
            value=profile.get("display_name", ""),
            max_length=config.PROFILE_DISPLAY_NAME_MAX,
        )
        username_field = tf(
            "نام کاربری",
            value=profile.get("username", ""),
            max_length=config.PROFILE_USERNAME_MAX,
        )

        async def save(_=None):
            name = (name_field.value or "").strip()
            username = (username_field.value or "").strip()
            if username and not is_valid_username(
                username,
                min_len=config.PROFILE_USERNAME_MIN,
                max_len=config.PROFILE_USERNAME_MAX,
                allowed_chars=config.PROFILE_USERNAME_ALLOWED_CHARS,
            ):
                return self.toast(
                    "نام کاربری فقط حروف انگلیسی، عدد و _ باشد.",
                    error=True,
                )
            try:
                await self.api.save_profile({
                    "display_name": name,
                    "username": username,
                })
                self.toast("پروفایل ذخیره شد")
                self.page.pop_dialog()
            except ApiError as e:
                self.toast(str(e), error=True)

        self.page.show_dialog(ft.AlertDialog(
            title=T("پروفایل محلی", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                name_field,
                username_field,
                T("این اطلاعات فقط روی همین دستگاه ذخیره می‌شود.",
                  size=11, color=Palette.MUTED),
            ], spacing=12, tight=True, height=200),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("ذخیره",
                                on_click=lambda e: self.spawn(save())),
            ],
        ))

    async def _legacy_ai_settings(self):
        """Kept for older navigation paths; redirects to the new screen."""
        await self.go("ai_settings")

    async def _open_ai_provider(self):
        await self.go("ai_settings")

    async def v_ai_settings(self):
        # The current app AI path uses the built-in anonymous provider.
        # Avoid exposing legacy provider/key controls that are not used by
        # Api.ai(), which would mislead users into thinking they take effect.
        return ft.Column([
            self.gradient_header(
                "دستیار هوشمند",
                subtitle="تنظیمات و راهنمای استفاده",
                actions=[
                    self._header_icon(
                        ft.Icons.ARROW_FORWARD,
                        on_click=lambda e: self.spawn(self.go("settings")),
                    ),
                ],
            ),
            ft.Container(
                content=ft.Column([
                    surface_card(
                        ft.Row([
                            ft.Container(
                                content=ft.Icon(
                                    ft.Icons.AUTO_AWESOME,
                                    color="#FFFFFF",
                                    size=24,
                                ),
                                width=52,
                                height=52,
                                border_radius=16,
                                alignment=ft.Alignment(0, 0),
                                gradient=ft.LinearGradient(
                                    begin=ft.Alignment.TOP_LEFT,
                                    end=ft.Alignment.BOTTOM_RIGHT,
                                    colors=[Palette.ACCENT, Palette.PRIMARY],
                                ),
                            ),
                            ft.Column([
                                T(
                                    "هوش مصنوعی آماده استفاده است",
                                    size=16,
                                    weight=ft.FontWeight.BOLD,
                                    color=Palette.TEXT,
                                ),
                                T(
                                    "برای استفاده معمولاً نیازی به واردکردن "
                                    "کلید API نیست.",
                                    size=12,
                                    color=Palette.MUTED,
                                ),
                            ], spacing=4, expand=True),
                        ], spacing=12),
                    ),
                    surface_card(
                        T("پیش از انتشار بازبینی کنید", size=14,
                          weight=ft.FontWeight.W_600, color=Palette.TEXT),
                        T(
                            "پاسخ‌های هوش مصنوعی ممکن است اشتباه یا ناقص "
                            "باشند. متن نهایی، لینک‌ها و اطلاعات حساس را "
                            "پیش از انتشار در کانال بررسی کنید.",
                            size=13, color=Palette.MUTED,
                        ),
                    ),
                    surface_card(
                        T("دسترسی و حریم خصوصی", size=14,
                          weight=ft.FontWeight.W_600, color=Palette.TEXT),
                        T(
                            "متنی که برای تولید یا بازنویسی می‌فرستید برای "
                            "پردازش به سرویس هوش مصنوعی ارسال می‌شود؛ "
                            "اطلاعات محرمانه یا توکن ربات را در درخواست‌ها "
                            "قرار ندهید.",
                            size=13, color=Palette.MUTED,
                        ),
                    ),
                    btn_primary(
                        "گفت‌وگو با دستیار",
                        lambda e: self.spawn(self.go("chat")),
                    ),
                ], spacing=12),
                padding=16,
                expand=True,
            ),
        ], spacing=0, expand=True)

    async def v_help(self):
        steps = [
            ("۱", "از سایت ایتایار یک ربات بساز و توکن بگیر."),
            ("۲", "ربات را در کانال ادمین کن و دسترسی ارسال پیام بده."),
            ("۳", "در تنظیمات ← کانال‌های من، توکن و شناسه کانال را ثبت کن."),
            ("۴", "پیش‌نویس بساز، زمان‌بندی کن یا فوری منتشر کن."),
        ]
        cards = []
        for num, text in steps:
            cards.append(
                ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=T(num, size=16, weight=ft.FontWeight.BOLD,
                                      color="#FFFFFF"),
                            width=36, height=36, border_radius=12,
                            alignment=ft.Alignment(0, 0),
                            bgcolor=Palette.PRIMARY,
                        ),
                        T(text, size=14, color=Palette.TEXT, expand=True),
                    ], spacing=12),
                    padding=14,
                    border_radius=16,
                    bgcolor=Palette.CARD,
                    border=ft.Border.all(1, Palette.BORDER),
                )
            )

        return ft.Column([
            self.gradient_header(
                "راهنمای اتصال",
                actions=[
                    self._header_icon(
                        ft.Icons.ARROW_FORWARD,
                        on_click=lambda e: self.spawn(self.go("settings")),
                    ),
                ],
            ),
            ft.Container(
                content=ft.Column(cards, spacing=10),
                padding=16,
            ),
            ft.Container(
                content=surface_card(
                    T("نکته مهم", size=14, weight=ft.FontWeight.W_600,
                      color=Palette.TEXT),
                    T(
                        "تاریخچه کامل پیام‌های کانال از API در دسترس نیست؛ "
                        "فقط پیام‌هایی که از همین برنامه ارسال شده‌اند "
                        "در تاریخچه ثبت می‌شوند.",
                        size=13, color=Palette.MUTED,
                    ),
                ),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(height=40),
        ], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

    async def v_team(self):
        if not self.api.online or (self.me or {}).get("role") != "owner":
            return self._error_view(
                "مدیریت اعضا فقط برای مالک متصل به سرور در دسترس است.",
                "settings",
            )
        try:
            members = await self.api.get("/team")
        except ApiError as exc:
            return self._error_view(str(exc), "settings")

        cards = []
        for member in members or []:
            role = member.get("role", "—")
            cards.append(
                ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=T(
                                (member.get("display_name")
                                 or member.get("username")
                                 or "?")[:1].upper(),
                                size=16, color="#FFFFFF",
                                weight=ft.FontWeight.BOLD,
                            ),
                            width=42, height=42, border_radius=14,
                            alignment=ft.Alignment(0, 0),
                            bgcolor=Palette.PRIMARY,
                        ),
                        ft.Column([
                            T(member.get("display_name")
                              or member.get("username") or "—",
                              size=14, weight=ft.FontWeight.W_600,
                              color=Palette.TEXT),
                            T(role, size=12, color=Palette.MUTED),
                        ], spacing=2, expand=True),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_color=Palette.DANGER,
                            tooltip="حذف",
                            on_click=lambda e, m=member: self.spawn(
                                self._remove_member(m)
                            ),
                        ) if member.get("role") != "owner" else ft.Container(),
                    ], spacing=10),
                    padding=12,
                    border_radius=16,
                    bgcolor=Palette.CARD,
                    border=ft.Border.all(1, Palette.BORDER),
                )
            )

        return ft.Column([
            self.gradient_header(
                "اعضای تیم",
                actions=[
                    self._header_icon(
                        ft.Icons.PERSON_ADD,
                        on_click=lambda e: self._add_member_dialog(),
                    ),
                    self._header_icon(
                        ft.Icons.ARROW_FORWARD,
                        on_click=lambda e: self.spawn(self.go("settings")),
                    ),
                ],
            ),
            ft.Container(
                content=ft.Column(
                    cards or [
                        empty_state(
                            ft.Icons.GROUPS_OUTLINED,
                            "عضوی ثبت نشده",
                            "با دکمه + عضو جدید اضافه کنید",
                        )
                    ],
                    spacing=10,
                ),
                padding=16,
                expand=True,
            ),
        ], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

    def _add_member_dialog(self):
        username = tf("نام کاربری")
        password = tf("رمز عبور موقت", password=True, can_reveal_password=True)
        display = tf("نام نمایشی")
        role_dd = ft.Dropdown(
            label="نقش",
            value="editor",
            options=[
                ft.dropdown.Option("editor", "ویرایشگر"),
                ft.dropdown.Option("approver", "تأییدکننده"),
                ft.dropdown.Option("publisher", "ناشر"),
                ft.dropdown.Option("analyst", "تحلیل‌گر"),
            ],
            border_radius=14,
        )

        async def submit(_=None):
            u = (username.value or "").strip()
            p = password.value or ""
            if not u or len(p) < 8:
                return self.toast(
                    "نام کاربری و رمز حداقل ۸ نویسه لازم است.", error=True
                )
            try:
                await self.api.post("/team", {
                    "username": u,
                    "password": p,
                    "display_name": (display.value or "").strip(),
                    "role": role_dd.value or "editor",
                })
            except ApiError as exc:
                return self.toast(str(exc), error=True)
            password.value = ""
            self.page.pop_dialog()
            self.toast("عضو تیم اضافه شد.")
            await self.go("team")

        self.page.show_dialog(ft.AlertDialog(
            title=T("افزودن عضو", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                username, password, display, role_dd,
            ], spacing=10, tight=True, height=280),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("افزودن",
                                on_click=lambda e: self.spawn(submit())),
            ],
        ))

    async def _remove_member(self, member: dict):
        name = member.get("display_name") or member.get("username") or "عضو"
        if not await self.confirm(f"{name} از تیم حذف شود؟"):
            return
        try:
            await self.api.delete(f"/team/{member['id']}")
            self.toast("حذف شد")
            await self.go("team")
        except ApiError as e:
            self.toast(str(e), error=True)

    async def _logout(self):
        if not await self.confirm("از حساب خارج می‌شوید؟"):
            return
        self.api.logout()
        self.me = {"name": "محلی", "role": "local"}
        await self.go("auth", False)

    async def _signature_dialog(self):
        current = await self.api.storage.get("default_signature") or ""
        field = tf("امضای ثابت", value=current, multiline=True,
                   min_lines=2, max_lines=4)

        async def save(_=None):
            await self.api.storage.set(
                "default_signature", (field.value or "").strip()
            )
            self.toast("امضا ذخیره شد")
            self.page.pop_dialog()

        self.page.show_dialog(ft.AlertDialog(
            title=T("امضای پیش‌فرض", size=16, weight=ft.FontWeight.BOLD),
            content=field,
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("ذخیره",
                                on_click=lambda e: self.spawn(save())),
            ],
        ))

    async def _channels_sheet(self):
        self.channels = await self._safe_get("/channels", self.channels)
        items = []
        for ch in self.channels:
            items.append(
                ft.ListTile(
                    title=T(ch.get("title") or "—", size=14,
                            weight=ft.FontWeight.W_600),
                    subtitle=T(str(ch.get("channel_id") or ""), size=12,
                               color=Palette.MUTED),
                    trailing=ft.IconButton(
                        icon=ft.Icons.HISTORY,
                        tooltip="تاریخچه",
                        on_click=lambda e, c=ch: self.spawn(
                            self._open_channel_history(c)
                        ),
                    ),
                )
            )
        self.page.show_dialog(ft.AlertDialog(
            title=T("کانال‌های من", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column(
                items or [T("کانالی ثبت نشده", size=13, color=Palette.MUTED)],
                spacing=4, tight=True, height=280,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("افزودن",
                              on_click=lambda e: (
                                  self.page.pop_dialog(),
                                  self.spawn(self._add_channel_dialog()),
                              )),
                ft.TextButton("بستن",
                              on_click=lambda e: self.page.pop_dialog()),
            ],
        ))

    def _add_connection_dialog(self):
        name_f = tf("نام اتصال")
        token_f = tf("توکن ایتایار", password=True, can_reveal_password=True)

        async def submit(_=None):
            token = (token_f.value or "").strip()
            if not token:
                return self.toast("توکن را وارد کنید", error=True)
            try:
                await self.api.post("/connections", {
                    "name": (name_f.value or "").strip() or "اتصال",
                    "token": token,
                })
                self.toast("اتصال ثبت شد")
                self.page.pop_dialog()
            except ApiError as e:
                self.toast(str(e), error=True)

        self.page.show_dialog(ft.AlertDialog(
            title=T("اتصال ربات", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column([name_f, token_f], spacing=10, tight=True),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("ثبت",
                                on_click=lambda e: self.spawn(submit())),
            ],
        ))

    async def _open_channel_history(self, channel: dict):
        self.page.pop_dialog()
        await self.go("channel_history", channel.get("id"))

    async def _appearance_dialog(self):
        await self.go("settings")

    async def _add_channel_dialog(self):
        cid = tf("شناسه یا لینک کانال")
        title = tf("نام نمایشی")

        async def submit(_=None):
            if not (cid.value or "").strip():
                return self.toast("شناسه کانال لازم است", error=True)
            try:
                existing = await self.api.get("/connections")
                connection = existing[0] if existing else None
                payload = {
                    "value": cid.value.strip(),
                    "title": (title.value or "").strip() or cid.value.strip(),
                }
                if isinstance(connection, dict) and connection.get("id") is not None:
                    payload["connection_id"] = connection["id"]
                await self.api.post("/channels", payload)
                self.toast("کانال اضافه شد")
                self.page.pop_dialog()
                self.channels = await self._safe_get("/channels", [])
            except ApiError as e:
                self.toast(str(e), error=True)

        self.page.show_dialog(ft.AlertDialog(
            title=T("افزودن کانال", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column([cid, title], spacing=10, tight=True),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("افزودن",
                                on_click=lambda e: self.spawn(submit())),
            ],
        ))

    async def v_updates(self):
        try:
            release = await self.api.latest_release()
        except ApiError as exc:
            return ft.Container(
                content=ft.Column([
                    self.gradient_header("به‌روزرسانی"),
                    surface_card(
                        T("اطلاعات انتشار دریافت نشد.", size=15,
                          weight=ft.FontWeight.BOLD),
                        T(str(exc), size=12, color=Palette.MUTED_LIGHT),
                        btn_ghost(
                            "تلاش دوباره",
                            lambda e: self.spawn(self.go("updates")),
                        ),
                    ),
                ], spacing=16, scroll=ft.ScrollMode.AUTO),
                padding=16,
                expand=True,
            )

        current = tuple(int(part) for part in re.findall(
            r"\d+", config.VERSION
        )[:3])
        latest = tuple(int(part) for part in re.findall(
            r"\d+", release["tag"]
        )[:3])
        has_update = latest > current
        target_url = release.get("apk_url") or release["url"]
        notes = release.get("notes") or "یادداشت انتشار ثبت نشده است."
        return ft.Container(
            content=ft.Column([
                self.gradient_header(
                    "به‌روزرسانی",
                    subtitle=f"نسخه فعلی {config.VERSION}",
                ),
                ft.Container(
                    content=surface_card(
                        T(
                            f"{release['name']} ({release['tag']})",
                            size=16, weight=ft.FontWeight.BOLD,
                            color=Palette.TEXT_LIGHT,
                        ),
                        pill(
                            "نسخه جدید موجود است" if has_update
                            else "نسخه پایدار بررسی شد",
                            Palette.SUCCESS if has_update else Palette.PRIMARY,
                        ),
                        T(notes, size=12, color=Palette.MUTED_LIGHT,
                          selectable=True),
                        btn_primary(
                            "دریافت APK در مرورگر"
                            if release.get("apk_url")
                            else "مشاهده انتشار",
                            lambda e: self.spawn(
                                self._open_external(target_url)
                            ),
                            expand=True,
                        ),
                        btn_ghost(
                            "بازگشت",
                            lambda e: self.spawn(self.go("settings")),
                        ),
                    ),
                    padding=16,
                ),
            ], scroll=ft.ScrollMode.AUTO),
            expand=True,
        )

    async def v_feedback(self):
        if not self.api.online:
            return ft.Container(
                content=ft.Column([
                    self.gradient_header("بازخورد"),
                    surface_card(
                        T("بازخورد فقط در حالت متصل به سرور ارسال می‌شود.",
                          size=14, color=Palette.MUTED),
                        btn_ghost("بازگشت",
                                  lambda e: self.spawn(self.go("settings"))),
                    ),
                ], spacing=16),
                padding=16,
                expand=True,
            )

        message_f = tf("پیام شما", multiline=True, min_lines=4, max_lines=8)
        contact_f = tf("راه ارتباطی (اختیاری)")

        async def submit(_=None):
            text = (message_f.value or "").strip()
            if len(text) < 5:
                return self.toast("پیام کوتاه است.", error=True)
            try:
                result = await self.api.post("/feedback", {
                    "message": text,
                    "contact": (contact_f.value or "").strip(),
                })
                ticket = (result or {}).get("ticket") or "—"
                self.toast(f"ثبت شد · شماره پیگیری: {ticket}")
                await self.go("settings")
            except ApiError as e:
                self.toast(str(e), error=True)

        return ft.Column([
            self.gradient_header(
                "بازخورد",
                subtitle="ناشناس و بدون داده اضافی",
                actions=[
                    self._header_icon(
                        ft.Icons.ARROW_FORWARD,
                        on_click=lambda e: self.spawn(self.go("settings")),
                    ),
                ],
            ),
            ft.Container(
                content=surface_card(
                    message_f,
                    contact_f,
                    btn_primary("ارسال", lambda e: self.spawn(submit())),
                ),
                padding=16,
            ),
        ], spacing=0, expand=True)

    async def _show_feedback_admin(self):
        if not self.api.online:
            return self.toast("فقط در حالت سرور", error=True)
        try:
            items = await self.api.get("/feedback")
        except ApiError as e:
            return self.toast(str(e), error=True)
        rows = []
        for item in items or []:
            rows.append(T(
                f"#{item.get('id')} — {(item.get('message') or '')[:80]}",
                size=12, color=Palette.TEXT,
            ))
        self.page.show_dialog(ft.AlertDialog(
            title=T("بازخوردها", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column(
                rows or [T("موردی نیست", size=13, color=Palette.MUTED)],
                spacing=6, height=280, scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("بستن",
                              on_click=lambda e: self.page.pop_dialog()),
            ],
        ))

    async def _about(self):
        self.page.show_dialog(ft.AlertDialog(
            title=T(config.APP_NAME, size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                T(config.APP_TAGLINE, size=13, color=Palette.MUTED),
                T(f"نسخه {config.VERSION}", size=13, color=Palette.TEXT),
                T(config.PACKAGE_ID, size=11, color=Palette.MUTED),
            ], spacing=8, tight=True),
            actions=[
                ft.TextButton("بستن",
                              on_click=lambda e: self.page.pop_dialog()),
            ],
        ))

    def _empty_state(self, icon, title, subtitle=None):
        children = [
            ft.Container(
                content=ft.Icon(icon, size=56, color="#CBD5E1"),
                alignment=ft.Alignment(0, 0),
            ),
            ft.Container(height=20),
            T(title, size=16, weight=ft.FontWeight.BOLD,
              color=Palette.TEXT_LIGHT, text_align=ft.TextAlign.CENTER),
        ]
        if subtitle:
            children.append(T(subtitle, size=13, color=Palette.MUTED_LIGHT,
                              text_align=ft.TextAlign.CENTER))
        return ft.Container(
            content=ft.Column(
                children,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
            padding=32,
            alignment=ft.Alignment(0, 0),
        )


# ===================================================================== Store

class Storage:
    """Async key/value storage backed by Flet's SharedPreferences service.

    Flet 1.0 removed ``page.client_storage``; ``ft.SharedPreferences`` is its
    replacement. Failures are swallowed on purpose: a broken store must never
    crash the UI, the callers simply fall back to their defaults.
    """

    def __init__(self, prefs):
        self.prefs = prefs

    async def get(self, key):
        try:
            return await self.prefs.get(key)
        except Exception:
            return None

    async def set(self, key, value):
        try:
            await self.prefs.set(key, value)
        except Exception:
            pass

    async def remove(self, key):
        try:
            await self.prefs.remove(key)
        except Exception:
            pass


# ====================================================================== main

async def main(page: ft.Page):
    loop = asyncio.get_running_loop()

    page.title = config.APP_NAME
    page.rtl = True
    page.padding = 0
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(
        color_scheme_seed=Palette.PRIMARY,
        font_family="Vazirmatn",
    )
    page.bgcolor = Palette.SURFACE_LIGHT

    try:
        page.window.icon = "assets/icon.png"
    except Exception:
        pass

    prefs = ft.SharedPreferences()
    page.services.append(prefs)

    app = App(page, loop, prefs)
    if not app.api.online:
        asyncio.create_task(app._local_schedule_loop())

    def handle_keyboard_back(event):
        key = str(getattr(event, "key", "") or "").lower()
        event_type = str(getattr(event, "type", "") or "").lower()
        if key in ("back", "escape", "browserback") and event_type in (
            "", "keydown", "key-down"
        ):
            app.spawn(app.handle_back())

    page.on_keyboard_event = handle_keyboard_back
    page.on_view_pop = lambda event: app.spawn(app.handle_back())
    page.services.append(app.file_picker)
    page.services.append(app.url_launcher)
    page.on_platform_brightness_change = lambda e: (
        app.apply_appearance(app.theme_mode, app.accent_name),
        page.update(),
    )
    page.add(ft.SafeArea(content=app.body, expand=True))
    await app.boot()


if __name__ == "__main__":
    ft.run(main)
