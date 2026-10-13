"""Eitaa Manager — a modern client for managing Eitaa channels.

The app handles content creation with AI assistance, direct publishing to
EitaaYar, and lightweight analytics — all through a single, cohesive UI.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import logging
import re
from pathlib import Path

import flet as ft

import config
from api import Api, ApiError
from fa import digits, jdate, jfull_utc, to_utc_iso

logger = logging.getLogger(__name__)


# =================================================================== palette

class Palette:
    PRIMARY = "#0066FF"
    PRIMARY_DARK = "#0047B2"
    ACCENT = "#00D9FF"
    SURFACE_LIGHT = "#F7F9FC"
    SURFACE_DARK = "#0E1420"
    CARD_LIGHT = "#FFFFFF"
    CARD_DARK = "#1A2233"
    TEXT_LIGHT = "#0F172A"
    TEXT_DARK = "#E8EEF7"
    MUTED_LIGHT = "#64748B"
    MUTED_DARK = "#8DA0BF"
    SUCCESS = "#10B981"
    WARNING = "#F59E0B"
    DANGER = "#EF4444"


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
    "آبی": ("#0066FF", "#0047B2"),
    "سبزآبی": ("#00897B", "#00695C"),
    "بنفش": ("#7C4DFF", "#512DA8"),
    "نارنجی": ("#EF6C00", "#E65100"),
}

# Tabs shown in the bottom navigation bar, in order.
NAV_VIEWS = ["home", "posts", "chat", "settings"]


# ==================================================================== helpers

T = lambda s, **k: ft.Text(str(s), **k)


def tf(label, **k):
    return ft.TextField(
        label=label,
        border_radius=14,
        border_color="#E2E8F0",
        focused_border_color=Palette.PRIMARY,
        **k,
    )


def btn_primary(text, fn, **k):
    return ft.Container(
        content=T(text, color="#FFFFFF", weight=ft.FontWeight.W_600, size=14),
        padding=ft.Padding.symmetric(horizontal=20, vertical=14),
        alignment=ft.Alignment(0, 0),
        border_radius=14,
        gradient=ft.LinearGradient(
            colors=[Palette.PRIMARY, Palette.PRIMARY_DARK],
        ),
        ink=True,
        on_click=fn,
        **k,
    )


def btn_ghost(text, fn, **k):
    return ft.Container(
        content=T(text, color=Palette.PRIMARY, weight=ft.FontWeight.W_600, size=14),
        padding=ft.Padding.symmetric(horizontal=18, vertical=12),
        alignment=ft.Alignment(0, 0),
        border_radius=14,
        border=ft.Border.all(1.5, "#E2E8F0"),
        ink=True,
        on_click=fn,
        **k,
    )


def icon_btn(icon, fn, tooltip=None, color=None, size=20):
    return ft.IconButton(
        icon=icon,
        icon_size=size,
        icon_color=color or Palette.TEXT_LIGHT,
        tooltip=tooltip,
        on_click=fn,
    )


def surface_card(*children, padding=16, **k):
    return ft.Container(
        content=ft.Column(list(children), spacing=8, tight=True),
        padding=padding,
        border_radius=20,
        bgcolor=Palette.CARD_LIGHT,
        shadow=ft.BoxShadow(
            blur_radius=20,
            spread_radius=0,
            color="#0F172A10",
            offset=ft.Offset(0, 4),
        ),
        **k,
    )


def pill(label, color):
    return ft.Container(
        content=T(label, size=11, color=color, weight=ft.FontWeight.W_600),
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        border_radius=20,
        bgcolor=ft.Colors.with_opacity(0.12, color),
    )


# ======================================================================= App

class App:
    def __init__(self, page: ft.Page, loop: asyncio.AbstractEventLoop, prefs):
        self.page = page
        self.loop = loop

        self.me: dict = {"name": "محلی", "role": "local"}
        self.channels: list = []
        self.view = "home"
        self.theme_mode = "light"
        self.accent_name = "آبی"
        self.chat_messages: list[dict] = []

        self.body = ft.Container(expand=True)
        self.api = Api(Storage(prefs))
        self.file_picker = ft.FilePicker()
        self.url_launcher = ft.UrlLauncher()
        self.server_base = self.api.base
        self.api.on_unauthorized = lambda: self.spawn(self._session_expired())

    # ------------------------------------------------------------ utilities
    def spawn(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def toast(self, msg: str, error: bool = False):
        color = Palette.DANGER if error else None
        self.page.show_dialog(
            ft.SnackBar(
                content=T(msg, color="#FFFFFF" if color else None),
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
        rows = [
            ft.Row([
                T(title, size=22, weight=ft.FontWeight.BOLD, color="#FFFFFF",
                  expand=True),
                *(actions or []),
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
                    ft.Container(height=40),
                    ft.ProgressRing(width=24, height=24, stroke_width=2.5,
                                    color=Palette.PRIMARY),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
            alignment=ft.Alignment(0, 0),
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
        Palette.SURFACE_LIGHT = "#0E1420" if dark else "#F7F9FC"
        Palette.CARD_LIGHT = "#1A2233" if dark else "#FFFFFF"
        Palette.TEXT_LIGHT = "#E8EEF7" if dark else "#0F172A"
        Palette.MUTED_LIGHT = "#8DA0BF" if dark else "#64748B"
        self.page.theme_mode = {
            "system": ft.ThemeMode.SYSTEM,
            "light": ft.ThemeMode.LIGHT,
            "dark": ft.ThemeMode.DARK,
        }[mode]
        self.page.theme = ft.Theme(
            color_scheme_seed=Palette.PRIMARY,
            font_family="Vazirmatn",
        )
        self.page.dark_theme = ft.Theme(
            color_scheme_seed=Palette.PRIMARY,
            font_family="Vazirmatn",
            color_scheme=ft.ColorScheme(
                primary=Palette.PRIMARY,
                surface=Palette.SURFACE_DARK,
            ),
        )
        self.page.bgcolor = Palette.SURFACE_LIGHT

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
                self._logo(80),
                T("اتصال به سرور برقرار نشد", size=20,
                  weight=ft.FontWeight.BOLD, color=Palette.TEXT_LIGHT),
                T(message, size=13, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                btn_primary(
                    "تلاش دوباره برای اتصال",
                    lambda e: self.spawn(self._retry_backend()),
                ),
                btn_ghost(
                    "ادامه در حالت محلی",
                    lambda e: self.spawn(self._continue_offline()),
                ),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=18),
            padding=24,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        )

    async def _retry_backend(self):
        self.api.set_backend(self.server_base)
        await self.boot()

    async def _continue_offline(self):
        self.api.set_backend("")
        self.me = {"name": "محلی", "role": "local"}
        try:
            await self.api.bootstrap()
        except ApiError as exc:
            return self.show(self._error_view(str(exc), "home"))
        await self.enter()

    async def v_auth(self, register: bool = False):
        username = tf("نام کاربری", autofocus=True)
        display_name = tf("نام نمایشی", hint_text="اختیاری")
        password = tf("رمز عبور", password=True, can_reveal_password=True)
        busy = {"value": False}

        async def submit(e=None):
            if busy["value"]:
                return
            name = (username.value or "").strip()
            secret = password.value or ""
            if not name or not secret:
                return self.toast("نام کاربری و رمز عبور را وارد کنید.", error=True)
            busy["value"] = True
            try:
                if register:
                    self.me = await self.api.register(
                        name, secret, (display_name.value or "").strip()
                    )
                else:
                    self.me = await self.api.login(name, secret)
                password.value = ""
                await self.enter()
            except ApiError as exc:
                self.toast(str(exc), error=True)
            finally:
                busy["value"] = False

        password.on_submit = submit
        fields = [username]
        if register:
            fields.append(display_name)
        fields.append(password)
        heading = "ساخت حساب مالک" if register else "ورود به فضای کاری"
        action = "ثبت‌نام" if register else "ورود"
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
            bgcolor=Palette.CARD_LIGHT,
            indicator_color=ft.Colors.with_opacity(0.15, Palette.PRIMARY),
            on_change=lambda e: self.spawn(self.go_nav(int(e.control.selected_index))),
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.HOME_OUTLINED,
                                            selected_icon=ft.Icons.HOME,
                                            label="خانه"),
                ft.NavigationBarDestination(icon=ft.Icons.ARTICLE_OUTLINED,
                                            selected_icon=ft.Icons.ARTICLE,
                                            label="پست‌ها"),
                ft.NavigationBarDestination(
                    icon=ft.Icons.AUTO_AWESOME_OUTLINED,
                    selected_icon=ft.Icons.AUTO_AWESOME,
                    label="دستیار AI",
                ),
                ft.NavigationBarDestination(icon=ft.Icons.SETTINGS_OUTLINED,
                                            selected_icon=ft.Icons.SETTINGS,
                                            label="تنظیمات"),
            ],
        )

    async def go_nav(self, index: int):
        await self.go(NAV_VIEWS[index])

    async def go(self, name: str, *args):
        self.view = name
        self.page.floating_action_button = self._fab_for(name)
        # The bottom bar is a tab bar; sub-screens (e.g. "new") hide it.
        self.page.navigation_bar = (
            self._nav_bar(NAV_VIEWS.index(name)) if name in NAV_VIEWS else None
        )
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
        return ft.Container(
            content=ft.Column([
                ft.Container(height=80),
                ft.Container(
                    content=ft.Icon(ft.Icons.ERROR_OUTLINE, size=48,
                                    color=Palette.MUTED_LIGHT),
                    alignment=ft.Alignment(0, 0),
                ),
                ft.Container(height=16),
                T(message, size=14, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                ft.Container(height=24),
                *(
                    [btn_ghost(
                        "تلاش دوباره",
                        lambda e: self.spawn(self.go(back_to, *retry_args)),
                    )]
                    if back_to not in NAV_VIEWS else []
                ),
                btn_ghost("بازگشت",
                          lambda e: self.spawn(self.go(target))),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=32,
            expand=True,
        )

    # ========================================================= onboarding =
    async def onboarding(self):
        step = {"n": 0}

        async def next_step(e=None):
            if step["n"] < 1:
                step["n"] += 1
                render()
                return
            try:
                await self.api.bootstrap()
            except Exception:
                pass
            self.channels = await self._safe_get("/channels", [])
            if self.channels:
                await self.enter()
            else:
                self.channel_step()

        def render():
            views = [
                (ft.Icons.AUTO_AWESOME, "محتوا را با هوش مصنوعی بساز",
                 "کافی است ایده‌ات را بنویسی؛ متن حرفه‌ای برای کانالت آماده می‌شود."),
                (ft.Icons.SEND_ROUNDED, "با یک ضربه منتشر کن",
                 "پست را همان لحظه به کانال‌های متصل بفرست و نتیجه را ببین."),
            ]
            icon, title, desc = views[step["n"]]
            last = step["n"] == len(views) - 1

            self.body.content = ft.Container(
                content=ft.Column([
                    ft.Container(height=40),
                    ft.Container(
                        content=ft.Icon(icon, size=48, color="#FFFFFF"),
                        width=120, height=120,
                        alignment=ft.Alignment(0, 0),
                        border_radius=32,
                        gradient=ft.LinearGradient(
                            begin=ft.Alignment.TOP_LEFT,
                            end=ft.Alignment.BOTTOM_RIGHT,
                            colors=[Palette.ACCENT, Palette.PRIMARY],
                        ),
                        shadow=ft.BoxShadow(
                            blur_radius=30,
                            color=ft.Colors.with_opacity(0.35, Palette.PRIMARY),
                            offset=ft.Offset(0, 12),
                        ),
                    ),
                    ft.Container(height=40),
                    T(title, size=24, weight=ft.FontWeight.BOLD,
                      color=Palette.TEXT_LIGHT, text_align=ft.TextAlign.CENTER),
                    ft.Container(height=12),
                    T(desc, size=15, color=Palette.MUTED_LIGHT,
                      text_align=ft.TextAlign.CENTER),
                    ft.Container(height=40),
                    ft.Row([
                        ft.Container(
                            width=24 if i == step["n"] else 8,
                            height=8, border_radius=4,
                            bgcolor=Palette.PRIMARY if i == step["n"] else "#E2E8F0",
                            animate=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
                        )
                        for i in range(len(views))
                    ], alignment=ft.MainAxisAlignment.CENTER, spacing=6),
                    ft.Container(height=24),
                    ft.Container(
                        content=ft.Row([
                            btn_primary("شروع کنیم" if last else "ادامه",
                                        lambda e: self.spawn(next_step()))
                        ], alignment=ft.MainAxisAlignment.CENTER),
                    ),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                   scroll=ft.ScrollMode.AUTO),
                padding=32,
                expand=True,
                bgcolor=Palette.SURFACE_LIGHT,
            )
            self.page.update()

        render()

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
                if (bot_token.value or "").strip():
                    await self.api.post("/connections", {
                        "name": "اتصال اصلی",
                        "token": bot_token.value.strip(),
                    })
                    bot_token.value = ""
                await self._add_channel(cid.value, title.value or "")
            except ApiError as exc:
                return self.toast(str(exc), error=True)
            else:
                await self.enter()

        self.body.content = ft.Container(
            content=ft.Column([
                ft.Container(height=40),
                ft.Container(
                    content=ft.Icon(ft.Icons.CAMPAIGN, size=48, color="#FFFFFF"),
                    width=120, height=120,
                    alignment=ft.Alignment(0, 0),
                    border_radius=32,
                    gradient=ft.LinearGradient(
                        begin=ft.Alignment.TOP_LEFT,
                        end=ft.Alignment.BOTTOM_RIGHT,
                        colors=[Palette.ACCENT, Palette.PRIMARY],
                    ),
                ),
                ft.Container(height=32),
                T("کانالت را وصل کن", size=22, weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT, text_align=ft.TextAlign.CENTER),
                ft.Container(height=8),
                T("ربات باید در کانال مدیر باشد تا بتواند پست بفرستد.",
                  size=13, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                T("اگر هنوز اتصال ایتایار ندارید، توکن را از راهنمای رسمی "
                  "دریافت و در کادر زیر وارد کنید.",
                  size=12, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                ft.Container(height=24),
                bot_token,
                cid,
                title,
                ft.Container(height=16),
                ft.Row([
                    btn_ghost("ادامه بدون اتصال",
                              lambda e: self.spawn(self.go("home"))),
                    btn_primary("اتصال", lambda e: self.spawn(connect())),
                ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=10,
               scroll=ft.ScrollMode.AUTO),
            padding=32,
            expand=True,
            bgcolor=Palette.SURFACE_LIGHT,
        )
        self.page.update()

    async def _add_channel(self, value: str, title: str = ""):
        """Add a channel through the active backend and refresh the cache."""
        conns = await self._safe_get("/connections", [])
        conn_id = conns[0]["id"] if conns else 1
        added = await self.api.post("/channels", {
            "connection_id": conn_id,
            "value": value,
            "title": title,
        })
        self.channels = await self._safe_get("/channels", self.channels)
        return added or True

    # =============================================================== views =
    async def v_home(self):
        stats = await self._safe_get("/stats", {})
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
                                        size=40, color="#CBD5E1"),
                        alignment=ft.Alignment(0, 0),
                    ),
                    T("هنوز پستی منتشر نکرده‌ای", size=14,
                      color=Palette.MUTED_LIGHT),
                    T("از دکمه + شروع کن", size=12, color="#94A3B8"),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                   spacing=8),
                padding=ft.Padding.symmetric(vertical=40, horizontal=20),
                alignment=ft.Alignment(0, 0),
            ))

        children.append(ft.Container(height=100))

        return ft.Container(
            content=ft.Column(children, scroll=ft.ScrollMode.AUTO, spacing=0),
            expand=True,
        )

    def _parse_web_card(self):
        return ft.Container(
            content=ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Icon(
                            ft.Icons.LANGUAGE, size=24, color=Palette.PRIMARY
                        ),
                        width=48, height=48, border_radius=15,
                        alignment=ft.Alignment(0, 0),
                        bgcolor=ft.Colors.with_opacity(0.10, Palette.PRIMARY),
                    ),
                    ft.Column([
                        T("Parse-Web", size=15, weight=ft.FontWeight.BOLD,
                          color=Palette.TEXT_LIGHT),
                        T("مشاهده وب‌سایت Parse-Web", size=12,
                          color=Palette.MUTED_LIGHT),
                    ], spacing=2, expand=True),
                    ft.Icon(ft.Icons.OPEN_IN_NEW, color=Palette.PRIMARY),
                ], spacing=12),
                padding=16,
                border_radius=18,
                bgcolor=Palette.CARD_LIGHT,
                border=ft.Border.all(
                    1, ft.Colors.with_opacity(0.18, Palette.PRIMARY)
                ),
                ink=True,
                on_click=lambda e: self.spawn(
                    self._open_external("https://parse-web.ir")
                ),
            ),
            padding=ft.Padding.symmetric(horizontal=16),
        )

    async def _open_external(self, url: str):
        try:
            await self.url_launcher.launch_url(
                url, mode=ft.LaunchMode.EXTERNAL_APPLICATION
            )
        except Exception:
            self.toast("بازکردن پیوند ممکن نشد؛ اتصال و مرورگر را بررسی کنید.",
                       error=True)

    def _stat_tile(self, label, value, icon, color):
        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Container(
                        content=ft.Icon(icon, size=20, color=color),
                        width=40, height=40, border_radius=12,
                        alignment=ft.Alignment(0, 0),
                        bgcolor=ft.Colors.with_opacity(0.12, color),
                    ),
                ], alignment=ft.MainAxisAlignment.END),
                ft.Container(height=8),
                T(digits(value), size=26, weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                T(label, size=12, color=Palette.MUTED_LIGHT),
            ], spacing=2),
            padding=16,
            border_radius=20,
            bgcolor=Palette.CARD_LIGHT,
            shadow=ft.BoxShadow(blur_radius=16, color="#0F172A08",
                                offset=ft.Offset(0, 3)),
            expand=True,
        )

    def _post_summary(self, post: dict, names: dict):
        status = post["status"]
        color = ST_COLOR.get(status, Palette.MUTED_LIGHT)
        channel_names = "، ".join(
            names.get(d["channel_id"], "") for d in post.get("deliveries", [])
        ) or "—"
        return ft.Container(
            content=ft.Container(
                content=ft.Column([
                    ft.Row([
                        T(post["title"] or post["text"][:40],
                          size=14, weight=ft.FontWeight.W_600,
                          color=Palette.TEXT_LIGHT, expand=True,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        pill(ST.get(status, status), color),
                    ]),
                    T(channel_names, size=12, color=Palette.MUTED_LIGHT,
                      max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=6, tight=True),
                padding=16,
                border_radius=18,
                bgcolor=Palette.CARD_LIGHT,
                shadow=ft.BoxShadow(blur_radius=14, color="#0F172A08",
                                    offset=ft.Offset(0, 3)),
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=6),
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
        except ApiError as exc:
            self.toast(str(exc), error=True)

    # ------------------------------------------------------------- posts --
    async def v_posts(self, flt: str = "all", q: str = ""):
        posts = await self._safe_get("/posts", [])
        self.channels = await self._safe_get("/channels", self.channels)
        names = {c["id"]: c["title"] for c in self.channels}
        needle = (q or "").strip().lower()
        filtered = [
            p for p in posts
            if (flt == "all" or p["status"] == flt)
            and needle in ((p.get("title") or "") + p["text"]).lower()
        ]

        filters = [
            ("all", "همه"), ("draft", "پیش‌نویس"), ("queued", "در صف"),
            ("scheduled", "زمان‌بندی"), ("published", "منتشرشده"),
            ("failed", "ناموفق"),
        ]

        subtitle = f"{digits(len(posts))} پست"
        if needle:
            subtitle += f" — جستجو: «{q.strip()}»"

        children = [
            self.gradient_header(
                "پست‌ها",
                subtitle=subtitle,
                actions=[self._header_icon(
                    ft.Icons.SEARCH,
                    lambda e: self.spawn(self._search_dialog(flt, q)),
                )],
            ),
            ft.Container(height=24),

            # ---- filter chips ----
            ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=T(label,
                                  size=13,
                                  color="#FFFFFF" if flt == key else Palette.TEXT_LIGHT,
                                  weight=ft.FontWeight.W_600),
                        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                        border_radius=14,
                        bgcolor=Palette.PRIMARY if flt == key else Palette.CARD_LIGHT,
                        ink=True,
                        on_click=lambda e, k=key: self.spawn(self._reload_posts(k, q)),
                    )
                    for key, label in filters
                ], scroll=ft.ScrollMode.AUTO, spacing=8),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(height=16),
        ]

        children.append(ft.Container(
            content=ft.Row([
                ft.TextButton(
                    "خروجی JSON",
                    icon=ft.Icons.DOWNLOAD,
                    on_click=lambda e: self.spawn(
                        self._export_history("json", q=q, status=flt)
                    ),
                ),
                ft.TextButton(
                    "خروجی CSV",
                    icon=ft.Icons.TABLE_VIEW,
                    on_click=lambda e: self.spawn(
                        self._export_history("csv", q=q, status=flt)
                    ),
                ),
            ], alignment=ft.MainAxisAlignment.END),
            padding=ft.Padding.symmetric(horizontal=16),
        ))

        # ---- list ----
        if filtered:
            children.extend(self._post_card(p, names) for p in filtered)
        else:
            children.append(self._empty_state(
                "پستی وجود ندارد",
                "برای ساخت اولین پست، روی + بزن",
                ft.Icons.ARTICLE_OUTLINED,
            ))
        children.append(ft.Container(height=100))

        return ft.Container(
            content=ft.Column(children, scroll=ft.ScrollMode.AUTO, spacing=0),
            expand=True,
        )

    async def v_channel_history(self, channel_id: int):
        try:
            posts = await self.api.get(
                "/history",
                params={"channel_id": channel_id, "status": "published"},
            )
            channels = await self.api.get("/channels")
        except ApiError as exc:
            return self._error_view(str(exc), "settings")
        channel = next(
            (item for item in channels if item.get("id") == channel_id), None
        )
        if channel is None:
            return self._error_view("کانال پیدا نشد.", "settings")
        self.channels = channels
        history = [
            post for post in posts
            if any(
                delivery.get("channel_id") == channel_id
                and delivery.get("status") == "published"
                for delivery in post.get("deliveries", [])
            )
        ]
        children = [
            self.gradient_header(
                channel["title"],
                subtitle="پیام‌های منتشرشده از طریق این برنامه",
                actions=[self._header_icon(
                    ft.Icons.ARROW_FORWARD,
                    lambda e: self.spawn(self.go("settings")),
                ), self._header_icon(
                    ft.Icons.DOWNLOAD,
                    lambda e: self.spawn(self._export_history(
                        "json", channel_id=channel_id,
                    )),
                )],
            ),
            ft.Container(
                content=surface_card(
                    T("این فهرست فقط سوابق ثبت‌شده در برنامه را نشان می‌دهد. "
                      "تاریخچه کامل پیام‌های قبلی کانال از ایتا دریافت نمی‌شود.",
                      size=12, color=Palette.MUTED_LIGHT),
                ),
                padding=16,
            ),
        ]
        if history:
            children.extend(
                self._post_card(post, {channel_id: channel["title"]})
                for post in history
            )
        else:
            children.append(self._empty_state(
                "ارسال ثبت‌شده‌ای نیست",
                "این صفحه فقط پیام‌هایی را نشان می‌دهد که برنامه منتشر کرده است.",
                ft.Icons.HISTORY,
            ))
        return ft.Container(
            content=ft.Column(children, scroll=ft.ScrollMode.AUTO, spacing=0),
            expand=True,
        )

    async def _export_history(
        self, export_format: str, *, q: str = "", status: str = "all",
        channel_id: int | None = None,
    ):
        try:
            name, content = await self.api.export_history(
                export_format,
                query=q,
                status=status,
                channel_id=channel_id,
            )
            saved = await self.file_picker.save_file(
                dialog_title="ذخیره خروجی تاریخچه",
                file_name=name,
                src_bytes=content,
            )
        except (ApiError, OSError, RuntimeError) as exc:
            self.toast(f"ساخت یا ذخیره خروجی انجام نشد: {exc}", error=True)
            return
        if saved:
            self.toast("خروجی تاریخچه ذخیره شد.")

    async def _reload_posts(self, flt, q):
        self.body.content = await self.v_posts(flt, q)
        self.page.update()

    async def _search_dialog(self, flt, q):
        field = tf("جستجو در پست‌ها", value=q or "", autofocus=True)

        def apply(e=None):
            self.page.pop_dialog()
            self.spawn(self._reload_posts(flt, field.value or ""))

        def clear(e=None):
            self.page.pop_dialog()
            self.spawn(self._reload_posts(flt, ""))

        field.on_submit = apply
        self.page.show_dialog(ft.AlertDialog(
            title=T("جستجو", weight=ft.FontWeight.BOLD),
            content=field,
            actions=[
                ft.TextButton("پاک کردن", on_click=clear),
                ft.FilledButton("جستجو", on_click=apply),
            ],
        ))

    def _post_card(self, post: dict, names: dict):
        status = post["status"]
        color = ST_COLOR.get(status, Palette.MUTED_LIGHT)

        channel_rows = []
        for d in post.get("deliveries", []):
            ds = d["status"]
            if ds == "published":
                icon, icon_color = ft.Icons.CHECK_CIRCLE, Palette.SUCCESS
            elif ds == "failed":
                icon, icon_color = ft.Icons.CANCEL, Palette.DANGER
            else:
                icon, icon_color = ft.Icons.SCHEDULE, Palette.MUTED_LIGHT

            row = [
                ft.Icon(icon, size=16, color=icon_color),
                T(names.get(d["channel_id"], "—"), size=12,
                  color=Palette.MUTED_LIGHT, expand=True),
            ]
            if d.get("error"):
                row.append(T(d["error"], size=11, color=Palette.DANGER,
                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))
            channel_rows.append(ft.Row(row, spacing=6))

        body = [
            ft.Row([
                T(post["title"] or "بدون عنوان", size=15,
                  weight=ft.FontWeight.BOLD, color=Palette.TEXT_LIGHT,
                  expand=True, max_lines=1,
                  overflow=ft.TextOverflow.ELLIPSIS),
                pill(ST.get(status, status), color),
            ]),
            T(post["text"][:100] + ("…" if len(post["text"]) > 100 else ""),
              size=13, color=Palette.MUTED_LIGHT, max_lines=2,
              overflow=ft.TextOverflow.ELLIPSIS),
            *channel_rows,
        ]

        if status in CANCELLABLE and status != "draft":
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
                shadow=ft.BoxShadow(blur_radius=14, color="#0F172A08",
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
            details = [
                f"کانال: {channel.get('title') or delivery.get('channel_id')}",
                f"وضعیت: {ST.get(delivery.get('status'), delivery.get('status'))}",
                f"شناسه پیام: {message_id or 'در پاسخ API موجود نبود'}",
                f"زمان ثبت نتیجه: {delivery.get('done_at') or '—'}",
            ]
            if delivery.get("error"):
                details.append(f"خطا: {delivery['error']}")
            line = ft.Column([
                T("\n".join(details), size=12),
                *(
                    [ft.TextButton(
                        "تنظیم یادآوری بررسی/حذف",
                        icon=ft.Icons.NOTIFICATIONS_OUTLINED,
                        on_click=lambda e, d=delivery: self.spawn(
                            self._create_removal_reminder(d)
                        ),
                    )]
                    if delivery.get("status") == "published"
                    and delivery.get("remote_message_id")
                    and self.api.online
                    and (self.me or {}).get("role") in ("owner", "editor")
                    else []
                ),
            ], spacing=2, tight=True)
            delivery_lines.append(line)
        self.page.show_dialog(ft.AlertDialog(
            title=T(post.get("title") or "جزئیات پیام",
                    weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column([
                    T(post.get("text") or "", size=13, selectable=True),
                    T(f"ثبت محلی: {post.get('created_at') or '—'}",
                      size=11, color=Palette.MUTED_LIGHT),
                    T("ارسال‌های ثبت‌شده", weight=ft.FontWeight.BOLD),
                    *delivery_lines,
                    T("نسخه‌های قبلی", weight=ft.FontWeight.BOLD),
                    *[
                        T(
                            f"{revision.get('created_at') or '—'}: "
                            f"{revision.get('text') or ''}",
                            size=11, selectable=True,
                        )
                        for revision in revisions
                    ],
                    T(
                        "این جزئیات فقط داده‌های ثبت‌شده در برنامه هستند؛ "
                        "تاریخچه کامل کانال را نمایش نمی‌دهند.",
                        size=11, color=Palette.MUTED_LIGHT,
                    ),
                ], scroll=ft.ScrollMode.AUTO, tight=True, spacing=10),
                width=360,
                height=420,
            ),
            actions=[ft.TextButton(
                "بستن", on_click=lambda e: self.page.pop_dialog()
            )],
        ))

    async def _corrected_copy(self, post: dict):
        if not await self.confirm(
            "یک پیش‌نویس تازه از این پیام بسازیم؟ "
            "پیام منتشرشده در کانال تغییر نمی‌کند."
        ):
            return
        payload = {
            "title": post.get("title") or "",
            "text": post.get("text") or "",
            "signature": post.get("signature") or "",
            "channel_ids": sorted({
                item.get("channel_id")
                for item in post.get("deliveries", [])
                if isinstance(item.get("channel_id"), int)
            }),
            "mode": "draft",
            "silent": bool(post.get("silent")),
            "pin": bool(post.get("pin")),
            "view_to_delete": post.get("view_to_delete"),
            "reply_to_message_id": post.get("reply_to_message_id"),
            "media_id": post.get("media_id"),
        }
        try:
            created = await self.api.post("/posts", payload)
        except ApiError as exc:
            self.toast(str(exc), error=True)
            return
        if not isinstance(created, dict) or not isinstance(
            created.get("id"), int
        ):
            self.toast("پیش‌نویس نسخه اصلاحی ساخته نشد.", error=True)
            return
        await self.go("new", created["id"])

    async def _cancel_post(self, post_id: int):
        if not await self.confirm("این پست لغو شود؟"):
            return
        if await self.guard(self.api.post(f"/posts/{post_id}/cancel")) is not None:
            self.toast("✓ لغو شد")
            await self.go("posts")

    async def _delete_draft(self, post_id: int):
        if not await self.confirm("این پیش‌نویس حذف شود؟"):
            return
        try:
            await self.api.delete(f"/posts/{post_id}")
        except ApiError as exc:
            return self.toast(str(exc), error=True)
        self.toast("پیش‌نویس حذف شد.")
        await self.go("posts")

    async def _create_removal_reminder(self, delivery: dict):
        local_default = dt.datetime.now() + dt.timedelta(days=1)
        date_field = tf(
            "تاریخ (YYYY-MM-DD)",
            value=local_default.strftime("%Y-%m-%d"),
        )
        time_field = tf("ساعت (HH:MM)", value="09:00")

        async def submit(_):
            try:
                local_time = dt.datetime.strptime(
                    f"{date_field.value or ''} {time_field.value or ''}",
                    "%Y-%m-%d %H:%M",
                )
                if local_time <= dt.datetime.now():
                    raise ValueError
                await self.api.post("/removal-reminders", {
                    "delivery_id": delivery["id"],
                    "due_at": to_utc_iso(local_time),
                })
            except ValueError:
                self.toast("تاریخ یا ساعت معتبرِ آینده وارد کنید.", error=True)
                return
            except ApiError as exc:
                self.toast(str(exc), error=True)
                return
            self.page.pop_dialog()
            self.toast("یادآوری ثبت شد؛ حذف خودکار انجام نمی‌شود.")
            await self.go("reminders")

        self.page.show_dialog(ft.AlertDialog(
            title=T("یادآوری بررسی پیام", weight=ft.FontWeight.BOLD),
            content=ft.Column([
                T(
                    "در موعد، یادآوری در برنامه ثبت می‌شود. "
                    "API فعلی ایتایار حذف پیام را پشتیبانی نمی‌کند.",
                    size=12,
                ),
                date_field,
                time_field,
            ], tight=True, spacing=10),
            actions=[
                ft.TextButton(
                    "لغو", on_click=lambda e: self.page.pop_dialog()
                ),
                ft.TextButton("ثبت یادآوری", on_click=submit),
            ],
        ))

    async def v_reminders(self):
        if not self.api.online:
            return self._error_view(
                "مدیریت یادآوری‌ها به سرور متصل نیاز دارد. "
                "در حالت محلی، زمان‌بندی در پس‌زمینه قابل تضمین نیست.",
                "settings",
            )
        reminders = await self.api.get("/removal-reminders")
        cards = []
        states = {
            "scheduled": "در انتظار موعد",
            "due": "موعد رسیده — بررسی دستی لازم است",
            "cancelled": "لغوشده",
        }
        for reminder in reminders:
            due_at = reminder.get("due_at")
            try:
                due_text = jfull_utc(due_at) if due_at else "—"
            except (TypeError, ValueError):
                due_text = str(due_at or "—")

            async def cancel(_event, item=reminder):
                if not await self.confirm("این یادآوری لغو شود؟"):
                    return
                try:
                    await self.api.post(
                        f"/removal-reminders/{item['id']}/cancel"
                    )
                except ApiError as exc:
                    self.toast(str(exc), error=True)
                    return
                await self.go("reminders")

            async def reschedule(_event, item=reminder):
                date_field = tf("تاریخ (YYYY-MM-DD)")
                time_field = tf("ساعت (HH:MM)", value="09:00")

                async def submit(_):
                    try:
                        new_time = dt.datetime.strptime(
                            f"{date_field.value or ''} "
                            f"{time_field.value or ''}",
                            "%Y-%m-%d %H:%M",
                        )
                        if new_time <= dt.datetime.now():
                            raise ValueError
                        await self.api.post(
                            f"/removal-reminders/{item['id']}/reschedule",
                            {"due_at": to_utc_iso(new_time)},
                        )
                    except ValueError:
                        self.toast(
                            "تاریخ یا ساعت معتبرِ آینده وارد کنید.",
                            error=True,
                        )
                        return
                    except ApiError as exc:
                        self.toast(str(exc), error=True)
                        return
                    self.page.pop_dialog()
                    await self.go("reminders")

                self.page.show_dialog(ft.AlertDialog(
                    title=T("تغییر زمان یادآوری"),
                    content=ft.Column(
                        [date_field, time_field], tight=True, spacing=10
                    ),
                    actions=[
                        ft.TextButton(
                            "لغو", on_click=lambda e: self.page.pop_dialog()
                        ),
                        ft.TextButton("ذخیره", on_click=submit),
                    ],
                ))

            controls = [
                T(
                    f"{reminder.get('channel_title') or 'کانال'} · "
                    f"پیام {reminder.get('remote_message_id')}",
                    size=14, weight=ft.FontWeight.W_600,
                ),
                T(f"{states.get(reminder.get('status'), 'نامشخص')} · {due_text}",
                  size=12, color=Palette.MUTED_LIGHT),
            ]
            if reminder.get("status") in ("scheduled", "due"):
                controls.append(ft.Row([
                    ft.TextButton("تغییر زمان", on_click=reschedule),
                    ft.TextButton("لغو", on_click=cancel),
                ], alignment=ft.MainAxisAlignment.END))
            cards.append(surface_card(*controls))

        return ft.Container(
            content=ft.Column([
                self.gradient_header(
                    "یادآوری‌های پیام",
                    subtitle="یادآوری دستی؛ نه حذف خودکار",
                ),
                ft.Container(height=16),
                T(
                    "حذف پیام از طریق API ایتایار فعلی در دسترس نیست. "
                    "وقتی موعد برسد، وضعیت یادآوری تغییر می‌کند تا "
                    "پیام را دستی بررسی کنید.",
                    size=12, color=Palette.MUTED_LIGHT,
                ),
                *(cards if cards else [
                    T("یادآوری‌ای ثبت نشده است.", size=13,
                      color=Palette.MUTED_LIGHT)
                ]),
                ft.Container(height=90),
            ], scroll=ft.ScrollMode.AUTO, spacing=12),
            padding=ft.Padding.symmetric(horizontal=16),
            expand=True,
        )

    # ------------------------------------------------------------ AI chat --
    async def v_chat(self):
        stored = await self.api.storage.get("ai_chat_history")
        self.chat_messages = [
            item for item in stored if isinstance(item, dict)
            and item.get("role") in ("user", "assistant")
            and isinstance(item.get("text"), str)
        ][-30:] if isinstance(stored, list) else []
        for item in self.chat_messages:
            if item.get("status") == "waiting":
                item["status"] = "failed"
        state = {"busy": False}
        transcript = ft.Column(
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        prompt = ft.TextField(
            hint_text="ایده یا درخواستت را بنویس…",
            multiline=True,
            min_lines=1,
            max_lines=4,
            shift_enter=True,
            border_radius=22,
            filled=True,
            bgcolor=Palette.CARD_LIGHT,
            expand=True,
        )
        action = ft.Dropdown(
            value="create",
            options=[
                ft.dropdown.Option("create", "ساخت محتوا"),
                ft.dropdown.Option("rewrite", "بازنویسی"),
                ft.dropdown.Option("proofread", "ویرایش نگارشی"),
                ft.dropdown.Option("summarize", "خلاصه‌سازی"),
                ft.dropdown.Option("translate", "ترجمه"),
                ft.dropdown.Option("titles", "عنوان"),
                ft.dropdown.Option("hashtags", "هشتگ"),
                ft.dropdown.Option("variants", "چند نسخه"),
            ],
            border_radius=14,
            text_size=12,
            width=140,
            dense=True,
        )

        async def persist_history():
            self.chat_messages = self.chat_messages[-30:]
            await self.api.storage.set("ai_chat_history", self.chat_messages)

        def render_transcript():
            transcript.controls.clear()
            if not self.chat_messages:
                transcript.controls.append(ft.Container(
                    content=ft.Column([
                        ft.Container(
                            content=ft.Icon(
                                ft.Icons.AUTO_AWESOME,
                                size=34,
                                color="#FFFFFF",
                            ),
                            width=68, height=68, border_radius=24,
                            alignment=ft.Alignment(0, 0),
                            gradient=ft.LinearGradient(
                                colors=["#7C4DFF", "#00B8D9"]
                            ),
                        ),
                        T("دستیار محتوای ایتا", size=19,
                          weight=ft.FontWeight.BOLD),
                        T("ایده‌ات را بگو؛ پیش‌نویس را با هم بسازیم.",
                          size=13, color=Palette.MUTED_LIGHT,
                          text_align=ft.TextAlign.CENTER),
                        T("خروجی فقط پیشنهاد است و خودکار منتشر نمی‌شود.",
                          size=11, color=Palette.MUTED_LIGHT,
                          text_align=ft.TextAlign.CENTER),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                       spacing=12),
                    padding=ft.Padding.symmetric(vertical=36, horizontal=20),
                    alignment=ft.Alignment(0, 0),
                ))
            for index, message in enumerate(self.chat_messages):
                is_user = message["role"] == "user"
                failed = message.get("status") == "failed"
                waiting = message.get("status") == "waiting"
                bubble = ft.Container(
                    content=ft.Column([
                        T(
                            message["text"],
                            size=14,
                            color="#FFFFFF" if is_user else Palette.TEXT_LIGHT,
                            selectable=True,
                        ),
                        *(
                            [ft.Row([
                                ft.ProgressRing(
                                    width=12, height=12, stroke_width=2,
                                    color="#FFFFFF",
                                ),
                                T("در حال دریافت پاسخ…", size=11,
                                  color="#FFFFFF"),
                            ], spacing=8)]
                            if waiting else []
                        ),
                        *(
                            [ft.Row([
                                T("پاسخ آماده نشد؛ متن شما حفظ شده است.",
                                  size=11, color="#FFE1E1"),
                                ft.TextButton(
                                    "تلاش دوباره",
                                    icon=ft.Icons.REFRESH,
                                    on_click=lambda e, i=index:
                                        self.spawn(send_message(
                                            self.chat_messages[i]["text"],
                                            retry_index=i,
                                        )),
                                ),
                            ], wrap=True)]
                            if failed else []
                        ),
                        *(
                            [ft.TextButton(
                                "ذخیره به‌عنوان پیش‌نویس",
                                icon=ft.Icons.BOOKMARK_ADD_OUTLINED,
                                on_click=lambda e, text=message["text"]:
                                    self.spawn(save_draft(text)),
                            )]
                            if not is_user and message.get("status") == "ready"
                            else []
                        ),
                    ], spacing=6, tight=True),
                    padding=14,
                    border_radius=ft.BorderRadius(
                        18, 18, 5 if is_user else 18, 18
                    ),
                    bgcolor=(
                        "#B42335" if failed else
                        "#6246D9" if is_user else Palette.CARD_LIGHT
                    ),
                    shadow=ft.BoxShadow(
                        blur_radius=14,
                        color="#0F172A10",
                        offset=ft.Offset(0, 3),
                    ),
                    width=280,
                )
                transcript.controls.append(ft.Row(
                    [bubble],
                    alignment=(
                        ft.MainAxisAlignment.END if is_user
                        else ft.MainAxisAlignment.START
                    ),
                ))
            self.page.update()

        async def save_draft(text: str):
            try:
                created = await self.api.post("/posts", {
                    "title": "پیشنهاد دستیار هوشمند",
                    "text": text,
                    "mode": "draft",
                })
            except ApiError as exc:
                self.toast(str(exc), error=True)
                return
            if not isinstance(created, dict) or not isinstance(
                created.get("id"), int
            ):
                self.toast("ذخیره پیش‌نویس تأیید نشد؛ دوباره تلاش کنید.",
                           error=True)
                return
            self.toast("پیشنهاد به پیش‌نویس‌ها افزوده شد.")

        async def send_message(text: str, retry_index: int | None = None):
            if state["busy"] or not text.strip():
                return
            state["busy"] = True
            selected_action = action.value or "create"
            if retry_index is None:
                self.chat_messages.append({
                    "role": "user",
                    "text": text.strip(),
                    "status": "waiting",
                    "action": selected_action,
                })
                user_index = len(self.chat_messages) - 1
            else:
                user_index = retry_index
                self.chat_messages[user_index]["status"] = "waiting"
                selected_action = self.chat_messages[user_index].get(
                    "action", selected_action
                )
            await persist_history()
            render_transcript()
            prompt.value = ""
            send_button.disabled = True
            self.page.update()
            try:
                recent_context = self.chat_messages[
                    max(0, user_index - 7):user_index
                ]
                contextual_text = "\n".join(
                    f"{'کاربر' if item['role'] == 'user' else 'دستیار'}: "
                    f"{item['text'][:1500]}"
                    for item in recent_context
                    if item.get("status") == "ready"
                )
                request_text = (
                    f"پیشینه گفت‌وگو:\n{contextual_text}\n\n"
                    f"درخواست فعلی:\n{text.strip()}"
                    if contextual_text else text.strip()
                )
                result = await self.api.ai(
                    selected_action,
                    request_text,
                    language="fa",
                    tone="neutral",
                    length="medium",
                )
                self.chat_messages[user_index]["status"] = "ready"
                self.chat_messages.append({
                    "role": "assistant", "text": result, "status": "ready",
                })
            except ApiError as exc:
                self.chat_messages[user_index]["status"] = "failed"
                prompt.value = text
                self.toast(str(exc), error=True)
            except Exception:
                logger.exception("AI chat request failed unexpectedly")
                self.chat_messages[user_index]["status"] = "failed"
                prompt.value = text
                self.toast(
                    "پاسخ دریافت نشد؛ متن شما حفظ شد. دوباره تلاش کنید.",
                    error=True,
                )
            finally:
                state["busy"] = False
                send_button.disabled = False
                await persist_history()
                render_transcript()
                self.page.update()

        send_button = ft.IconButton(
            icon=ft.Icons.ARROW_UPWARD_ROUNDED,
            icon_color="#FFFFFF",
            bgcolor="#6246D9",
            tooltip="ارسال درخواست",
            on_click=lambda e: self.spawn(send_message(prompt.value or "")),
        )
        prompt.on_submit = lambda e: self.spawn(
            send_message(prompt.value or "")
        )
        render_transcript()
        return ft.Container(
            content=ft.Column([
                self.gradient_header(
                    "دستیار هوشمند",
                    subtitle="ساخت و ویرایش محتوا برای کانال",
                    actions=[self._header_icon(ft.Icons.AUTO_AWESOME)],
                ),
                ft.Container(
                    content=transcript,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                    expand=True,
                ),
                ft.Container(
                    content=ft.Column([
                        ft.Row([action], alignment=ft.MainAxisAlignment.END),
                        ft.Row([prompt, send_button], spacing=8,
                               vertical_alignment=ft.CrossAxisAlignment.END),
                        T("پاسخ‌ها پیش از انتشار قابل ویرایش‌اند.",
                          size=10, color=Palette.MUTED_LIGHT),
                    ], spacing=6),
                    padding=ft.Padding.only(
                        left=14, right=14, top=10, bottom=14
                    ),
                    bgcolor=Palette.SURFACE_LIGHT,
                ),
            ], spacing=0),
            expand=True,
        )

    # -------------------------------------------------------- new post -----
    async def v_new(self, post_id: int | None = None):
        post = None
        if post_id is not None:
            posts = await self.api.get("/posts")
            post = next((p for p in posts if p.get("id") == post_id), None)
            if post is None or post.get("status") != "draft":
                return self._error_view("فقط پیش‌نویس موجود را می‌توان ویرایش کرد.")
        templates = await self._safe_get("/templates", [])
        self.channels = await self._safe_get("/channels", self.channels)
        default_signature = await self.api.storage.get("post_signature") or ""
        ai_preferences = {
            name: await self.api.storage.get(f"ai_preference_{name}")
            for name in ("language", "tone", "length")
        }

        # With a single channel there is nothing to choose: pre-select it.
        draft_channel_ids = {
            delivery.get("channel_id")
            for delivery in (post or {}).get("deliveries", [])
        }
        state = {
            "selected": (
                draft_channel_ids
                or ({c["id"] for c in self.channels}
                    if len(self.channels) == 1 else set())
            ),
            "silent": bool((post or {}).get("silent")),
            "pin": bool((post or {}).get("pin")),
            "busy": False,
        }

        title = tf("عنوان پست", hint_text="اختیاری",
                   value=post.get("title", "") if post else "")
        text = ft.TextField(
            label="متن پست",
            hint_text="متن خود را اینجا بنویسید…",
            value=post.get("text", "") if post else "",
            multiline=True,
            min_lines=8,
            max_lines=20,
            border_radius=14,
            border_color="#E2E8F0",
            focused_border_color=Palette.PRIMARY,
        )
        scheduled_date = ft.TextField(
            label="تاریخ انتشار",
            value=dt.datetime.now().strftime("%Y-%m-%d"),
            hint_text="YYYY-MM-DD",
            expand=True,
        )
        scheduled_time = ft.TextField(
            label="ساعت انتشار",
            value=dt.datetime.now().strftime("%H:%M"),
            hint_text="HH:MM",
            expand=True,
        )
        repeat = ft.Dropdown(
            label="تکرار",
            value="none",
            options=[
                ft.dropdown.Option("none", "بدون تکرار"),
                ft.dropdown.Option("daily", "روزانه"),
                ft.dropdown.Option("weekly", "هفتگی"),
            ],
            expand=True,
            disabled=not self.api.online or post is not None,
        )
        repeat_count = ft.TextField(
            label="تعداد دفعات",
            value="1",
            keyboard_type=ft.KeyboardType.NUMBER,
            width=130,
            disabled=not self.api.online or post is not None,
        )
        view_to_delete = ft.TextField(
            label="حذف پس از بازدید (اختیاری)",
            hint_text="مثلاً ۱۰۰",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=str((post or {}).get("view_to_delete") or ""),
        )
        reply_to = ft.TextField(
            label="شناسه پیام برای پاسخ (اختیاری)",
            hint_text="ID پیام موجود در کانال",
            keyboard_type=ft.KeyboardType.NUMBER,
            value=str((post or {}).get("reply_to_message_id") or ""),
        )
        signature = ft.TextField(
            label="زیرنویس یا امضای این پیام (اختیاری)",
            hint_text="در انتهای متن فرستاده می‌شود",
            value=(
                post.get("signature", "")
                if post is not None else default_signature
            ),
            multiline=True,
            min_lines=2,
            max_lines=5,
            border_radius=14,
        )
        media = {
            "id": (post or {}).get("media_id"),
            "name": "",
        }
        media_status = T(
            f"رسانه پیوست شده (شناسه {media['id']})"
            if media["id"] else "فایلی پیوست نشده است.",
            size=12, color=Palette.MUTED_LIGHT,
        )

        async def pick_media(e=None):
            if not self.api.online:
                return self.toast(
                    "پیوست فایل نیازمند سرور برای آپلود امن فایل است.",
                    error=True,
                )
            try:
                selected = await self.file_picker.pick_files(
                    dialog_title="انتخاب فایل برای پست",
                    allow_multiple=False,
                    with_data=False,
                )
            except (RuntimeError, OSError) as exc:
                self.toast(f"انتخاب فایل ناموفق بود: {exc}", error=True)
                return
            if not selected:
                return
            file = selected[0]
            if file.size > 20 * 1024 * 1024:
                return self.toast("حداکثر اندازه فایل ۲۰ مگابایت است.", error=True)
            try:
                file_data = file.bytes
                if file_data is None and file.path:
                    file_data = await asyncio.to_thread(
                        Path(file.path).read_bytes
                    )
                if not isinstance(file_data, bytes):
                    raise OSError("محتوای فایل در دسترس نیست.")
                uploaded = await self.api.upload(file.name, file_data)
            except (ApiError, OSError) as exc:
                return self.toast(str(exc), error=True)
            if not isinstance(uploaded, dict) or not isinstance(
                uploaded.get("id"), int
            ):
                return self.toast(
                    "پاسخ آپلود معتبر نبود؛ فایل به پست پیوست نشد.",
                    error=True,
                )
            media["id"] = uploaded.get("id")
            media["name"] = uploaded.get("filename") or file.name
            media_status.value = f"پیوست: {media['name']}"
            self.page.update()

        def clear_media(e=None):
            media["id"] = None
            media["name"] = ""
            media_status.value = "فایلی پیوست نشده است."
            self.page.update()

        prompt = tf("موضوع یا ایده", hint_text="مثلاً: معرفی اپلیکیشن جدید")
        ai_language = ft.Dropdown(
            label="زبان",
            value=ai_preferences["language"] or "fa",
            options=[
                ft.dropdown.Option("fa", "فارسی"),
                ft.dropdown.Option("en", "English"),
                ft.dropdown.Option("ar", "العربية"),
            ],
            expand=True,
            on_select=lambda e: self.spawn(self._persist_ai_preference(
                "language", e.control.value
            )),
        )
        ai_tone = ft.Dropdown(
            label="لحن",
            value=ai_preferences["tone"] or "neutral",
            options=[
                ft.dropdown.Option("neutral", "متعادل"),
                ft.dropdown.Option("formal", "رسمی"),
                ft.dropdown.Option("friendly", "دوستانه"),
                ft.dropdown.Option("news", "خبری"),
            ],
            expand=True,
            on_select=lambda e: self.spawn(self._persist_ai_preference(
                "tone", e.control.value
            )),
        )
        ai_length = ft.Dropdown(
            label="طول متن",
            value=ai_preferences["length"] or "medium",
            options=[
                ft.dropdown.Option("short", "کوتاه"),
                ft.dropdown.Option("medium", "متوسط"),
                ft.dropdown.Option("long", "مفصل"),
            ],
            expand=True,
            on_select=lambda e: self.spawn(self._persist_ai_preference(
                "length", e.control.value
            )),
        )

        async def run_ai(action: str):
            if state["busy"]:
                return
            state["busy"] = True
            try:
                result = await self.api.ai(
                    action, text.value or "",
                    (prompt.value or "") if action == "create" else "",
                    language=ai_language.value or "fa",
                    tone=ai_tone.value or "neutral",
                    length=ai_length.value or "medium",
                )
                if action in ("titles", "hashtags"):
                    self.page.show_dialog(ft.AlertDialog(
                        title=T("عنوان‌های پیشنهادی" if action == "titles"
                                else "هشتگ‌های پیشنهادی",
                                weight=ft.FontWeight.BOLD),
                        content=ft.Column([T(result, selectable=True)],
                                          tight=True, scroll=ft.ScrollMode.AUTO),
                        actions=[ft.TextButton(
                            "بستن", on_click=lambda e: self.page.pop_dialog())],
                    ))
                else:
                    text.value = result
                    self.toast("✓ آماده شد")
                    self.page.update()
            except ApiError as e:
                self.toast(str(e), error=True)
            finally:
                state["busy"] = False

        def fill_template(tpl: dict):
            def handler(e):
                body = tpl.get("body", "")
                body = body.replace("{{title}}", (title.value or "").strip())
                body = body.replace("{{date}}", jdate(dt.datetime.now()))
                # Placeholders the user still has to fill are dropped so they
                # are never published literally.
                body = re.sub(r"\{\{\w+\}\}", "", body)
                body = re.sub(r"\n{3,}", "\n\n", body).strip()
                text.value = body
                self.page.update()
            return handler

        def pick_channel(ch):
            def handler(e):
                if e.control.value:
                    state["selected"].add(ch["id"])
                else:
                    state["selected"].discard(ch["id"])
            return ft.Checkbox(
                label=ch["title"],
                value=ch["id"] in state["selected"],
                active_color=Palette.PRIMARY,
                on_change=handler,
            )

        async def save(mode: str):
            if state["busy"]:
                return
            if not (text.value or "").strip():
                return self.toast("متن پست را بنویسید", error=True)
            if mode != "draft" and not state["selected"]:
                return self.toast("حداقل یک کانال انتخاب کنید", error=True)

            scheduled_at = None
            if mode == "schedule":
                try:
                    local_time = dt.datetime.strptime(
                        f"{scheduled_date.value or ''} {scheduled_time.value or ''}",
                        "%Y-%m-%d %H:%M",
                    )
                except ValueError:
                    return self.toast(
                        "تاریخ را به شکل YYYY-MM-DD و ساعت را به شکل HH:MM وارد کنید",
                        error=True,
                    )
                if local_time <= dt.datetime.now():
                    return self.toast("زمان انتشار باید در آینده باشد", error=True)
                if local_time > dt.datetime.now() + dt.timedelta(days=30):
                    return self.toast(
                        "زمان انتشار باید حداکثر تا ۳۰ روز آینده باشد",
                        error=True,
                    )
                scheduled_at = to_utc_iso(local_time)

            raw_views = (view_to_delete.value or "").strip()
            raw_reply = (reply_to.value or "").strip()
            raw_repeats = (repeat_count.value or "1").strip()
            try:
                views = int(raw_views) if raw_views else None
                reply_id = int(raw_reply) if raw_reply else None
                repeat_total = int(raw_repeats)
            except ValueError:
                return self.toast(
                    "تعداد بازدید، شناسه پیام و دفعات تکرار باید عدد صحیح باشند.",
                    error=True,
                )
            if views is not None and views < 1:
                return self.toast("آستانه بازدید باید حداقل ۱ باشد.", error=True)
            if reply_id is not None and reply_id < 1:
                return self.toast("شناسه پیام باید عددی مثبت باشد.", error=True)
            if repeat_total < 1 or repeat_total > 30:
                return self.toast(
                    "تعداد نوبت‌های تکرار باید بین ۱ تا ۳۰ باشد.",
                    error=True,
                )
            if mode == "schedule" and repeat.value != "none":
                interval_days = 1 if repeat.value == "daily" else 7
                utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                utc_schedule = dt.datetime.fromisoformat(scheduled_at)
                last_repeat = utc_schedule + dt.timedelta(
                    days=interval_days * (repeat_total - 1)
                )
                if last_repeat > utc_now + dt.timedelta(days=30):
                    return self.toast(
                        "تمام نوبت‌های تکرار باید در ۳۰ روز آینده باشند.",
                        error=True,
                    )

            state["busy"] = True
            try:
                if mode == "now":
                    self.toast("در حال ارسال…")
                elif mode == "schedule":
                    self.toast("در حال ثبت زمان‌بندی…")
                payload = {
                    "title": title.value or "",
                    "text": text.value,
                    "channel_ids": sorted(state["selected"]),
                    "mode": mode,
                    "silent": state["silent"],
                    "pin": state["pin"],
                    "view_to_delete": views,
                    "reply_to_message_id": reply_id,
                    "signature": (signature.value or "").strip(),
                    "media_id": media["id"],
                }
                if scheduled_at:
                    payload["scheduled_at"] = scheduled_at
                if mode == "schedule":
                    payload["repeat"] = repeat.value or "none"
                    payload["repeat_count"] = repeat_total
                if post is None:
                    result = await self.api.post("/posts", payload)
                else:
                    result = await self.api.put(
                        f"/posts/{post_id}", payload
                    )
                if mode == "now" and isinstance(result, dict) \
                        and result.get("status") == "failed":
                    self.toast(
                        "انتشار ناموفق بود؛ متن در سوابق برنامه حفظ شد.",
                        error=True,
                    )
                    state["busy"] = False
                    await self.go("posts")
                    return
                success = {
                    "now": (
                        "پست منتشر شد"
                        if isinstance(result, dict)
                        and result.get("status") == "published"
                        else "درخواست انتشار ثبت شد"
                    ),
                    "schedule": "زمان‌بندی ثبت شد",
                    "draft": "تغییرات پیش‌نویس ذخیره شد" if post else "پیش‌نویس ذخیره شد",
                }[mode]
                self.toast("✓ " + success)
                state["busy"] = False
                await self.go("posts")
            except ApiError as e:
                self.toast(str(e), error=True)
            finally:
                state["busy"] = False

        # --- AI action chips ---
        ai_chips = ft.Row([
            ft.Container(
                content=T(label, size=12, weight=ft.FontWeight.W_600,
                          color=Palette.PRIMARY),
                padding=ft.Padding.symmetric(horizontal=14, vertical=8),
                border_radius=12,
                bgcolor=ft.Colors.with_opacity(0.10, Palette.PRIMARY),
                ink=True,
                on_click=lambda e, a=action: self.spawn(run_ai(a)),
            )
            for action, label, _tip in AI_ACTIONS
        ], scroll=ft.ScrollMode.AUTO, spacing=8)

        # --- template chips ---
        template_chips = ft.Row([
            ft.Container(
                content=T(t["name"], size=12, weight=ft.FontWeight.W_600,
                          color=Palette.TEXT_LIGHT),
                padding=ft.Padding.symmetric(horizontal=14, vertical=8),
                border_radius=12,
                bgcolor=Palette.CARD_LIGHT,
                ink=True,
                on_click=fill_template(t),
            )
            for t in templates
        ], scroll=ft.ScrollMode.AUTO, spacing=8)

        channel_controls = [pick_channel(c) for c in self.channels]

        content_card = [title, text]
        if templates:
            content_card = [
                T("قالب آماده", size=12, color=Palette.MUTED_LIGHT),
                template_chips,
                title,
                text,
            ]

        form = ft.Column([
            # --- AI section ---
            surface_card(
                ft.Row([
                    ft.Container(
                        content=ft.Icon(ft.Icons.AUTO_AWESOME,
                                        size=20, color=Palette.PRIMARY),
                        width=40, height=40, border_radius=12,
                        alignment=ft.Alignment(0, 0),
                        bgcolor=ft.Colors.with_opacity(0.12, Palette.PRIMARY),
                    ),
                    ft.Column([
                        T("دستیار هوشمند", size=15,
                          weight=ft.FontWeight.BOLD,
                          color=Palette.TEXT_LIGHT),
                        T("متن پست را خودکار بساز یا ویرایش کن",
                          size=12, color=Palette.MUTED_LIGHT),
                    ], spacing=2, expand=True),
                ]),
                ft.Container(height=4),
                prompt,
                ft.Row([ai_language, ai_tone, ai_length], spacing=8),
                ft.Row([
                    btn_primary("✨ بساز",
                                lambda e: self.spawn(run_ai("create"))),
                ]),
                ft.Container(height=4),
                ai_chips,
            ),

            ft.Container(height=12),

            # --- content ---
            surface_card(*content_card),

            ft.Container(height=12),

            # --- schedule ---
            surface_card(
                T("زمان‌بندی انتشار", size=15,
                  weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                T("زمان‌بندی فقط با سرور فعال است و تا ۳۰ روز آینده انجام می‌شود.",
                  size=12, color=Palette.MUTED_LIGHT),
                ft.Row([scheduled_date, scheduled_time], spacing=12),
                ft.Row([repeat, repeat_count], spacing=12),
            ),

            ft.Container(height=12),

            # --- channels ---
            surface_card(
                T("انتشار در کانال‌ها", size=15,
                  weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                *(channel_controls or [
                    T("هنوز کانالی متصل نشده است",
                      size=12, color=Palette.MUTED_LIGHT),
                ]),
            ),

            ft.Container(height=12),

            # --- options ---
            surface_card(
                T("رسانه و پاسخ", size=15,
                  weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                ft.Row([
                    ft.Container(
                        content=T("پیوست فایل", color=Palette.PRIMARY,
                                  weight=ft.FontWeight.W_600, size=14),
                        padding=ft.Padding.symmetric(horizontal=18, vertical=12),
                        alignment=ft.Alignment(0, 0),
                        border_radius=14,
                        border=ft.Border.all(1.5, "#E2E8F0"),
                        ink=self.api.online,
                        on_click=(
                            lambda e: self.spawn(pick_media())
                            if self.api.online else None
                        ),
                        opacity=1 if self.api.online else 0.5,
                        expand=True,
                    ),
                    ft.TextButton(
                        "حذف پیوست",
                        icon=ft.Icons.CLOSE,
                        on_click=clear_media,
                        disabled=not media["id"],
                    ),
                ], spacing=8),
                media_status,
                *([] if self.api.online else [T(
                    "برای آپلود فایل، اتصال به سرور لازم است.",
                    size=11, color=Palette.MUTED_LIGHT,
                )]),
                reply_to,
                signature,
                T("شناسه پیام باید از ایتا معلوم باشد؛ برنامه تاریخچه را واکشی نمی‌کند.",
                  size=11, color=Palette.MUTED_LIGHT),
            ),

            ft.Container(height=12),

            # --- view-based deletion ---
            surface_card(
                T("حذف براساس بازدید", size=15,
                  weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                view_to_delete,
                T("این گزینه با پارامتر ایتایار ارسال می‌شود؛ اجرای آن وابسته به پشتیبانی ایتا است.",
                  size=11, color=Palette.MUTED_LIGHT),
            ),

            ft.Container(height=12),

            # --- options ---
            surface_card(
                T("تنظیمات انتشار", size=15,
                  weight=ft.FontWeight.BOLD,
                  color=Palette.TEXT_LIGHT),
                ft.Switch(
                    label="ارسال بی‌صدا",
                    active_color=Palette.PRIMARY,
                    on_change=lambda e: state.__setitem__(
                        "silent", bool(e.control.value)),
                ),
                ft.Switch(
                    label="پین کردن پس از ارسال",
                    active_color=Palette.PRIMARY,
                    on_change=lambda e: state.__setitem__(
                        "pin", bool(e.control.value)),
                ),
            ),

            ft.Container(height=24),
        ], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

        header = ft.Container(
            content=ft.Row([
                icon_btn(ft.Icons.ARROW_FORWARD,
                         lambda e: self.spawn(self.go("posts")),
                         color="#FFFFFF"),
                T("ویرایش پیش‌نویس" if post else "پست جدید",
                  size=18, weight=ft.FontWeight.BOLD,
                  color="#FFFFFF", expand=True),
            ]),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            gradient=ft.LinearGradient(
                colors=[Palette.PRIMARY, Palette.PRIMARY_DARK]),
        )

        # ---- bottom action bar ----
        action_bar = ft.Container(
            content=(
                ft.Column([
                    ft.Row([
                        btn_ghost("ذخیره پیش‌نویس",
                                  lambda e: self.spawn(save("draft")),
                                  expand=True),
                        btn_primary("انتشار",
                                    lambda e: self.spawn(save("now")),
                                    expand=True),
                    ], spacing=12),
                    *([btn_ghost(
                        "زمان‌بندی",
                        lambda e: self.spawn(save("schedule")),
                        expand=True,
                    )] if self.api.online else []),
                ], spacing=8)
                if post else ft.Column([
                    ft.Row([
                        btn_ghost("ذخیره پیش‌نویس",
                                  lambda e: self.spawn(save("draft")), expand=True),
                        btn_ghost("زمان‌بندی",
                                  lambda e: self.spawn(save("schedule")), expand=True),
                    ], spacing=12),
                    btn_primary("انتشار",
                                lambda e: self.spawn(save("now")), expand=True),
                ], spacing=8)
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            bgcolor=Palette.CARD_LIGHT,
            shadow=ft.BoxShadow(blur_radius=20, color="#0F172A14",
                                offset=ft.Offset(0, -4)),
        )

        return ft.Container(
            content=ft.Column([
                header,
                ft.Container(content=form, padding=16, expand=True),
                action_bar,
            ], spacing=0, expand=True),
            expand=True,
        )

    # ---------------------------------------------------------- settings --
    async def v_settings(self):
        self.channels = await self._safe_get("/channels", self.channels)
        channel_count = len(self.channels)

        return ft.Container(
            content=ft.Column([
                self.gradient_header(
                    "تنظیمات",
                    subtitle=config.APP_NAME,
                ),
                ft.Container(height=24),

                # profile card
                ft.Container(
                    content=surface_card(
                        ft.Row([
                            ft.Container(
                                content=T("م", size=20, color="#FFFFFF",
                                          weight=ft.FontWeight.BOLD),
                                width=56, height=56, border_radius=18,
                                alignment=ft.Alignment(0, 0),
                                gradient=ft.LinearGradient(
                                    colors=[Palette.ACCENT, Palette.PRIMARY]),
                            ),
                            ft.Column([
                                T(
                                    (self.me or {}).get("name")
                                    or (self.me or {}).get("username")
                                    or "حساب کاربری",
                                    size=16,
                                  weight=ft.FontWeight.BOLD,
                                  color=Palette.TEXT_LIGHT),
                                T(
                                    "حساب متصل به سرور"
                                    if self.api.online and self.me
                                    else "حالت محلی — ورود به سرور در دسترس نیست",
                                  size=12, color=Palette.MUTED_LIGHT),
                            ], spacing=2, expand=True),
                        ], spacing=14),
                    ),
                    padding=ft.Padding.symmetric(horizontal=16),
                ),
                ft.Container(height=16),

                # menu items
                ft.Container(
                    content=surface_card(
                        self._menu_row(ft.Icons.CAMPAIGN_OUTLINED, "کانال‌های من",
                                       f"{digits(channel_count)} کانال",
                                       lambda e: self.spawn(self._channels_sheet())),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.AUTO_AWESOME_OUTLINED,
                                       "هوش مصنوعی",
                                       "انتخاب سرویس، مدل و کلید",
                                       lambda e: self.spawn(self.go("ai"))),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.HELP_OUTLINE,
                                       "راهنمای اتصال",
                                       "افزودن کانال و دریافت توکن",
                                       lambda e: self.spawn(self.go("help"))),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.TUNE,
                                       "ظاهر برنامه",
                                       "تم و رنگ تأکیدی",
                                       lambda e: self.spawn(
                                           self._appearance_dialog())),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.FEEDBACK_OUTLINED,
                                       "پیشنهاد و گزارش مشکل",
                                       "ارسال ناشناس و پیگیری با شماره",
                                       lambda e: self.spawn(
                                           self.go("feedback"))),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(
                            ft.Icons.NOTIFICATIONS_OUTLINED,
                            "یادآوری پیام‌ها",
                            "زمان‌بندی بررسی دستی؛ حذف خودکار پشتیبانی نمی‌شود",
                            lambda e: self.spawn(self.go("reminders")),
                            disabled=(
                                not self.api.online
                                or (self.me or {}).get("role")
                                not in ("owner", "editor")
                            ),
                        ),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.SYSTEM_UPDATE_ALT,
                                       "بررسی به‌روزرسانی",
                                       "نسخه پایدار منتشرشده در GitHub",
                                       lambda e: self.spawn(self.go("updates"))),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.FORMAT_QUOTE,
                                       "امضای پیش‌فرض پست",
                                       "زیرنویس ذخیره‌شده روی این دستگاه",
                                       lambda e: self.spawn(
                                           self._signature_dialog())),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(
                            ft.Icons.GROUP_OUTLINED,
                            "اعضای تیم",
                            "نقش‌ها و دسترسی کاربران",
                            lambda e: self.spawn(self.go("team")),
                            disabled=(
                                not self.api.online
                                or (self.me or {}).get("role") != "owner"
                            ),
                        ),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.LOGOUT,
                                       "خروج از حساب",
                                       "پایان نشست سرور",
                                       lambda e: self.spawn(self._logout()),
                                       disabled=not bool(self.api.token)),
                        ft.Divider(height=1, color="#F1F5F9"),
                        self._menu_row(ft.Icons.INFO_OUTLINE, "درباره برنامه",
                                       f"نسخه {digits(config.VERSION)}",
                                       lambda e: self.spawn(self._about())),
                    ),
                    padding=ft.Padding.symmetric(horizontal=16),
                ),

                ft.Container(height=100),
            ], scroll=ft.ScrollMode.AUTO, spacing=0),
            expand=True,
        )

    def _menu_row(self, icon, title, subtitle, on_click, disabled=False):
        return ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Icon(icon, size=20, color=Palette.PRIMARY),
                    width=40, height=40, border_radius=12,
                    alignment=ft.Alignment(0, 0),
                    bgcolor=ft.Colors.with_opacity(0.10, Palette.PRIMARY),
                ),
                ft.Column([
                    T(title, size=14, weight=ft.FontWeight.W_600,
                      color=Palette.TEXT_LIGHT),
                    T(subtitle, size=11, color=Palette.MUTED_LIGHT),
                ], spacing=1, expand=True),
                ft.Icon(ft.Icons.CHEVRON_LEFT, color="#CBD5E1", size=20),
            ], spacing=14),
            padding=ft.Padding.symmetric(vertical=12),
            ink=not disabled,
            on_click=None if disabled else on_click,
            opacity=0.5 if disabled else 1,
        )

    async def v_ai_settings(self):
        if self.api.online:
            async def test_server_ai():
                try:
                    await self.api.ai("proofread", "سلام")
                except ApiError as exc:
                    self.toast(str(exc), error=True)
                else:
                    self.toast("اتصال هوش مصنوعی سرور برقرار است.")

            return ft.Container(
                content=ft.Column([
                    self.gradient_header("هوش مصنوعی",
                                         subtitle="تنظیمات سرویس"),
                    ft.Container(
                        content=surface_card(ft.Column([
                            T("تنظیمات AI در سرور مدیریت می‌شود",
                              size=16, weight=ft.FontWeight.BOLD),
                            T("کلید و ارائه‌دهنده را مدیر سرور تنظیم می‌کند. "
                              "این دستگاه کلید جداگانه‌ای برای درخواست‌های "
                              "سرور ذخیره نمی‌کند.",
                              size=13, color=Palette.MUTED_LIGHT),
                            btn_primary(
                                "آزمایش اتصال",
                                lambda e: self.spawn(test_server_ai()),
                                expand=True,
                            ),
                        ], spacing=14)),
                        padding=16,
                    ),
                ], scroll=ft.ScrollMode.AUTO),
                expand=True,
            )

        settings = await self.api.ai_settings()
        provider = ft.Dropdown(
            label="ارائه‌دهنده",
            value=settings["provider"],
            options=[
                ft.dropdown.Option("groq", "Groq"),
                ft.dropdown.Option("openai", "OpenAI یا سازگار"),
                ft.dropdown.Option("gemini", "Google Gemini"),
                ft.dropdown.Option("anthropic", "Anthropic Claude"),
            ],
            border_radius=14,
        )
        model = tf(
            "مدل‌های چرخشی Groq" if provider.value == "groq" else "مدل",
            value=settings["model"],
            hint_text="برای این سرویس نام مدل را وارد کنید",
            disabled=provider.value == "groq",
        )
        model_defaults = {
            "groq": "چرخش تصادفی بین ۳ مدل پشتیبانی‌شده",
            "openai": "gpt-4o-mini",
            "gemini": "gemini-2.5-flash",
            "anthropic": "claude-sonnet-4-6",
        }
        selected_provider = {"value": provider.value}
        model_values = {provider.value: model.value}
        base_url = tf(
            "نشانی API سازگار",
            value=settings.get("base_url", ""),
            hint_text="فقط برای OpenAI یا سرویس سازگار؛ HTTPS",
            keyboard_type=ft.KeyboardType.URL,
            disabled=provider.value != "openai",
        )

        key_field = ft.TextField(
            label="کلید API جدید",
            hint_text="کلید موجود جایگزین نمی‌شود",
            password=True,
            can_reveal_password=True,
            border_radius=14,
            disabled=settings["has_key"],
        )
        key_status = T(
            "کلید از قبل پیکربندی شده است؛ مقدار آن نمایش یا جایگزین نمی‌شود."
            if settings["has_key"] else "هنوز کلیدی ذخیره نشده است.",
            size=12, color=Palette.MUTED_LIGHT,
        )

        async def provider_changed(e):
            model_values[selected_provider["value"]] = model.value
            selected_provider["value"] = provider.value
            model.value = model_values.get(
                provider.value, model_defaults[provider.value]
            )
            model.label = (
                "مدل‌های چرخشی Groq"
                if provider.value == "groq" else "مدل"
            )
            model.disabled = provider.value == "groq"
            base_url.disabled = provider.value != "openai"
            has_key = await self.api.ai_key_configured(provider.value)
            key_field.disabled = has_key
            key_status.value = (
                "کلید از قبل پیکربندی شده است؛ مقدار آن نمایش یا جایگزین نمی‌شود."
                if has_key else "برای این ارائه‌دهنده هنوز کلیدی ثبت نشده است."
            )
            self.page.update()

        provider.on_select = provider_changed

        async def save_settings(show_toast=True):
            try:
                await self.api.save_ai_settings(
                    provider=provider.value or "groq",
                    model=(model.value or "").strip(),
                    key=key_field.value or "",
                    base_url=(base_url.value or "").strip(),
                )
            except (ApiError, ValueError) as exc:
                self.toast(str(exc), error=True)
                return False
            key_field.value = ""
            has_key = await self.api.ai_key_configured(provider.value)
            key_field.disabled = has_key
            key_status.value = (
                "کلید پیکربندی شده است؛ مقدار آن نمایش داده نمی‌شود."
                if has_key else "تنظیمات ذخیره شد؛ هنوز کلیدی ثبت نشده است."
            )
            self.page.update()
            if show_toast:
                self.toast("تنظیمات هوش مصنوعی ذخیره شد.")
            return True

        async def test_connection():
            if not await save_settings(show_toast=False):
                return
            try:
                await self.api.ai("proofread", "سلام")
            except ApiError as exc:
                self.toast(str(exc), error=True)
            else:
                self.toast("اتصال هوش مصنوعی برقرار است.")

        return ft.Container(
            content=ft.Column([
                self.gradient_header("هوش مصنوعی",
                                     subtitle="مدیریت ارائه‌دهنده و کلید"),
                ft.Container(
                    content=surface_card(ft.Column([
                        T("کلید در تنظیمات محلی دستگاه ذخیره می‌شود؛ "
                          "این ذخیره‌سازی گاوصندوق رمزگذاری‌شده نیست.",
                          size=12, color=Palette.MUTED_LIGHT),
                        *(
                            [T(
                                "در هر درخواست یکی از سه مدل Groq به‌صورت "
                                "تصادفی انتخاب می‌شود؛ در صورت خطا، مدل‌های "
                                "دیگر نیز امتحان می‌شوند.",
                                size=12, color=Palette.MUTED_LIGHT,
                            )]
                            if provider.value == "groq" else []
                        ),
                        provider, model, base_url, key_field, key_status,
                        ft.Row([
                            btn_ghost("ذخیره",
                                      lambda e: self.spawn(save_settings()),
                                      expand=True),
                            btn_primary(
                                "ذخیره و آزمایش",
                                lambda e: self.spawn(test_connection()),
                                expand=True,
                            ),
                        ], spacing=10),
                        ft.TextButton(
                            "دریافت کلید از وب‌سایت رسمی",
                            icon=ft.Icons.OPEN_IN_NEW,
                            on_click=lambda e: self._open_ai_provider(
                                provider.value),
                        ),
                    ], spacing=12)),
                    padding=16,
                ),
            ], scroll=ft.ScrollMode.AUTO),
            expand=True,
        )

    def _open_ai_provider(self, provider):
        urls = {
            "groq": "https://console.groq.com/keys",
            "openai": "https://platform.openai.com/api-keys",
            "gemini": "https://aistudio.google.com/app/apikey",
            "anthropic": "https://console.anthropic.com/settings/keys",
        }
        self.page.launch_url(urls.get(provider, urls["groq"]))

    async def v_help(self):
        return ft.Container(
            content=ft.Column([
                self.gradient_header("راهنمای اتصال",
                                     subtitle="کانال و ایتایار"),
                ft.Container(
                    content=surface_card(ft.Column([
                        T("۱. ساخت توکن ایتایار", size=16,
                          weight=ft.FontWeight.BOLD),
                        T("از وب‌سایت رسمی ایتایار وارد حساب شوید و برای "
                          "ربات خود توکن بسازید. توکن را فقط داخل برنامه "
                          "وارد کنید و برای دیگران نفرستید.",
                          size=13, color=Palette.MUTED_LIGHT),
                        ft.TextButton(
                            "باز کردن ایتایار",
                            icon=ft.Icons.OPEN_IN_NEW,
                            on_click=lambda e: self.page.launch_url(
                                "https://eitaayar.ir/"),
                        ),
                        T("۲. افزودن کانال", size=16,
                          weight=ft.FontWeight.BOLD),
                        T("در تنظیمات ← کانال‌های من، ابتدا «افزودن ربات "
                          "ایتایار» را بزنید و توکن را ثبت کنید؛ سپس «افزودن "
                          "کانال» را انتخاب و شناسه را وارد کنید. ربات را "
                          "طبق دستورهای رسمی به کانال اضافه کنید و دسترسی "
                          "ارسال پیام بدهید. توکن بررسی می‌شود؛ دسترسی کانال "
                          "هنگام ارسال واقعی مشخص خواهد شد.",
                          size=13, color=Palette.MUTED_LIGHT),
                        T("۳. سوابق پیام", size=16,
                          weight=ft.FontWeight.BOLD),
                        T("فهرست پیام‌ها فقط سوابقی را نشان می‌دهد که خود "
                          "برنامه منتشر و ثبت کرده است. دریافت همه پیام‌های "
                          "قدیمی کانال و ویرایش یا حذف پیام منتشرشده با API "
                          "فعلی ایتایار پشتیبانی نمی‌شود.",
                          size=13, color=Palette.MUTED_LIGHT),
                        T("۴. ورود و همگام‌سازی", size=16,
                          weight=ft.FontWeight.BOLD),
                        T("ورود و ثبت‌نام نیازمند سرور فعال است. در حالت "
                          "محلی، پیش‌نویس‌ها و تنظیمات دستگاه در دسترس‌اند؛ "
                          "همگام‌سازی سرور اعلام نمی‌شود.",
                          size=13, color=Palette.MUTED_LIGHT),
                    ], spacing=12)),
                    padding=16,
                ),
            ], scroll=ft.ScrollMode.AUTO),
            expand=True,
        )

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
        role_names = {
            "owner": "مالک",
            "editor": "ویرایشگر",
            "approver": "تأییدکننده",
            "publisher": "ناشر",
            "analyst": "تحلیل‌گر",
        }
        member_cards = []
        for member in members:
            controls = [
                ft.Icon(ft.Icons.PERSON_OUTLINE,
                        color=Palette.PRIMARY, size=22),
                ft.Column([
                    T(member.get("name") or member["username"],
                      size=14, weight=ft.FontWeight.W_600),
                    T(
                        f"@{member['username']} · "
                        f"{role_names.get(member['role'], member['role'])}",
                        size=12, color=Palette.MUTED_LIGHT,
                    ),
                ], spacing=3, expand=True),
            ]
            if member.get("id") != (self.me or {}).get("id"):
                controls.append(ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE,
                    tooltip="حذف عضو",
                    on_click=lambda e, uid=member["id"]: self.spawn(
                        self._remove_member(uid)),
                ))
            member_cards.append(ft.Container(
                content=surface_card(ft.Row(controls, spacing=12)),
                padding=ft.Padding.symmetric(horizontal=16, vertical=5),
            ))
        return ft.Container(
            content=ft.Column([
                self.gradient_header(
                    "اعضای تیم",
                    subtitle=f"{digits(len(members))} کاربر",
                    actions=[ft.IconButton(
                        icon=ft.Icons.PERSON_ADD_ALT_1,
                        icon_color="#FFFFFF",
                        tooltip="افزودن عضو",
                        on_click=lambda e: self._add_member_dialog(),
                    )],
                ),
                ft.Container(
                    content=T(
                        "هر عضو حساب و رمز مستقل دارد. دسترسی‌ها با نقش "
                        "محدود می‌شوند؛ رمز موقت را امن به کاربر برسانید.",
                        size=12, color=Palette.MUTED_LIGHT,
                    ),
                    padding=ft.Padding.symmetric(horizontal=20, vertical=12),
                ),
                *(member_cards or [self._empty_state(
                    "عضوی ثبت نشده", None, ft.Icons.GROUP_OUTLINED)]),
                ft.Container(height=90),
            ], scroll=ft.ScrollMode.AUTO, spacing=0),
            expand=True,
        )

    def _add_member_dialog(self):
        username = tf("نام کاربری", autofocus=True)
        name = tf("نام نمایشی", hint_text="اختیاری")
        password = ft.TextField(
            label="رمز اولیه (حداقل ۸ نویسه)",
            password=True,
            can_reveal_password=True,
            border_radius=14,
        )
        role = ft.Dropdown(
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

        async def add(e=None):
            values = {
                "username": (username.value or "").strip(),
                "password": password.value or "",
                "display_name": (name.value or "").strip(),
                "role": role.value,
            }
            if not values["username"] or len(values["password"]) < 8:
                return self.toast(
                    "نام کاربری را وارد و رمز حداقل ۸ نویسه‌ای تعیین کنید.",
                    error=True,
                )
            try:
                await self.api.post("/team", values)
            except ApiError as exc:
                return self.toast(str(exc), error=True)
            password.value = ""
            self.page.pop_dialog()
            self.toast("عضو تیم اضافه شد.")
            await self.go("team")

        self.page.show_dialog(ft.AlertDialog(
            title=T("افزودن عضو تیم", weight=ft.FontWeight.BOLD),
            content=ft.Column(
                [username, name, password, role],
                tight=True, spacing=10, scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("افزودن",
                                on_click=lambda e: self.spawn(add())),
            ],
        ))

    async def _remove_member(self, user_id: int):
        if not await self.confirm("این عضو از فضای کاری حذف شود؟"):
            return
        try:
            await self.api.delete(f"/team/{user_id}")
        except ApiError as exc:
            return self.toast(str(exc), error=True)
        self.toast("عضو حذف شد.")
        await self.go("team")

    async def _logout(self):
        if not self.api.token:
            self.toast("نشستی برای خروج وجود ندارد.", error=True)
            return
        if not await self.confirm("از حساب سرور خارج شوید؟"):
            return
        self.api.logout()
        self.me = None
        await self.go("auth")

    async def _signature_dialog(self):
        current = await self.api.storage.get("post_signature") or ""
        signature = ft.TextField(
            label="متن امضا یا زیرنویس",
            value=current,
            multiline=True,
            min_lines=3,
            max_lines=6,
            border_radius=14,
        )

        async def save(e=None):
            try:
                await self.api._store_verified(
                    "post_signature", (signature.value or "").strip()
                )
            except ApiError as exc:
                self.toast(str(exc), error=True)
                return
            self.page.pop_dialog()
            self.toast("امضای پیش‌فرض این دستگاه ذخیره شد.")

        self.page.show_dialog(ft.AlertDialog(
            title=T("امضای پیش‌فرض پست", weight=ft.FontWeight.BOLD),
            content=ft.Column([
                T("این امضا هنگام ساخت پست جدید پیشنهاد می‌شود و در پایان "
                  "متن منتشرشده قرار می‌گیرد. امضا فقط در این دستگاه ذخیره "
                  "می‌شود.",
                  size=12, color=Palette.MUTED_LIGHT),
                signature,
            ], tight=True, spacing=10),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("ذخیره", on_click=lambda e: self.spawn(save())),
            ],
        ))

    async def _channels_sheet(self):
        rows = [
            ft.ListTile(
                leading=ft.Container(
                    content=ft.Icon(ft.Icons.CAMPAIGN, color="#FFFFFF", size=18),
                    width=40, height=40, border_radius=12,
                    alignment=ft.Alignment(0, 0),
                    bgcolor=Palette.PRIMARY,
                ),
                title=T(c["title"], size=14, weight=ft.FontWeight.W_600),
                subtitle=T(c["chat_id"], size=11, color=Palette.MUTED_LIGHT),
                trailing=ft.Icon(ft.Icons.HISTORY, color=Palette.MUTED_LIGHT),
                on_click=lambda e, channel_id=c["id"]: self.spawn(
                    self._open_channel_history(channel_id)),
            )
            for c in self.channels
        ] or [T("کانالی متصل نیست", color=Palette.MUTED_LIGHT, size=13)]

        def add_clicked(e):
            self.page.pop_dialog()
            self.spawn(self._add_channel_dialog())

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(
                content=ft.Column([
                    ft.Container(
                        content=ft.Container(
                            width=40, height=4, border_radius=2,
                            bgcolor="#E2E8F0",
                        ),
                        alignment=ft.Alignment(0, 0),
                        padding=ft.Padding.only(bottom=16),
                    ),
                    T("مقصدهای متصل", size=18, weight=ft.FontWeight.BOLD),
                    T("برای دیدن سوابق پیام‌های برنامه، یک کانال را انتخاب کنید.",
                      size=12, color=Palette.MUTED_LIGHT),
                    ft.Container(height=8),
                    *rows,
                    ft.Container(height=8),
                    ft.Row([
                        btn_ghost("＋ افزودن ربات ایتایار",
                                  lambda e: self._add_connection_dialog()),
                        btn_ghost("＋ افزودن کانال", add_clicked),
                    ], alignment=ft.MainAxisAlignment.CENTER, spacing=8),
                    ft.Container(height=20),
                ], spacing=4, tight=True, scroll=ft.ScrollMode.AUTO),
                padding=20,
            ),
        ))

    def _add_connection_dialog(self):
        name = tf("نام اتصال", value="اتصال ایتایار")
        token = ft.TextField(
            label="توکن ربات ایتایار",
            password=True,
            can_reveal_password=True,
            border_radius=14,
            autofocus=True,
        )

        async def save_connection(e=None):
            secret = (token.value or "").strip()
            if not secret and self.api.online:
                return self.toast("توکن ایتایار را وارد کنید.", error=True)
            try:
                connection = {
                    "name": (name.value or "اتصال ایتایار").strip(),
                }
                if secret:
                    connection["token"] = secret
                await self.api.post("/connections", connection)
            except ApiError as exc:
                self.toast(str(exc), error=True)
                return
            token.value = ""
            self.page.pop_dialog()
            self.toast("اتصال ایتایار تأیید شد.")
            await self._channels_sheet()

        self.page.show_dialog(ft.AlertDialog(
            title=T("افزودن اتصال ایتایار", weight=ft.FontWeight.BOLD),
            content=ft.Column([
                T("توکن را فقط از منبع رسمی بگیرید. در حالت محلی اگر توکن "
                  "از قبل تنظیم شده باشد، این صفحه آن را جایگزین نمی‌کند.",
                  size=12, color=Palette.MUTED_LIGHT),
                name,
                token,
            ], tight=True, spacing=10),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("بررسی و ذخیره",
                                on_click=lambda e: self.spawn(save_connection())),
            ],
        ))

    async def _open_channel_history(self, channel_id):
        self.page.pop_dialog()
        await self.go("channel_history", channel_id)

    async def _appearance_dialog(self):
        theme = ft.Dropdown(
            label="تم",
            value=self.theme_mode,
            options=[
                ft.dropdown.Option("system", "پیروی از سیستم"),
                ft.dropdown.Option("light", "روشن"),
                ft.dropdown.Option("dark", "تاریک"),
            ],
        )
        accent = ft.Dropdown(
            label="رنگ تأکیدی",
            value=self.accent_name,
            options=[ft.dropdown.Option(value, value) for value in ACCENTS],
        )

        async def save(e=None):
            try:
                await self.api._store_verified(
                    "appearance_theme", theme.value or "system"
                )
                await self.api._store_verified(
                    "appearance_accent", accent.value or "آبی"
                )
            except ApiError as exc:
                self.toast(str(exc), error=True)
                return
            self.apply_appearance(theme.value or "system",
                                  accent.value or "آبی")
            self.page.pop_dialog()
            await self.go(self.view if self.view in NAV_VIEWS else "settings")

        self.page.show_dialog(ft.AlertDialog(
            title=T("ظاهر برنامه", weight=ft.FontWeight.BOLD),
            content=ft.Column([
                theme,
                accent,
                T("تنظیمات ظاهر فقط روی همین دستگاه ذخیره می‌شود.",
                  size=11, color=Palette.MUTED_LIGHT),
            ], tight=True, spacing=12),
            actions=[
                ft.TextButton("لغو",
                              on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("ذخیره",
                                on_click=lambda e: self.spawn(save())),
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
                        T(
                            "دانلود و نصب از مرورگر Android انجام می‌شود؛ "
                            "این برنامه دانلود پس‌زمینه یا نصب خودکار را "
                            "ادعا نمی‌کند. Android تأیید نصب را از شما می‌خواهد.",
                            size=11, color=Palette.MUTED_LIGHT,
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
                    self.gradient_header("پیشنهاد و گزارش مشکل"),
                    surface_card(
                        T("ارسال و پیگیری بازخورد به سرور نیاز دارد.",
                          size=14, weight=ft.FontWeight.BOLD),
                        T("در حالت محلی، بازخوردی از دستگاه خارج نمی‌شود "
                          "و شماره پیگیری جعلی ساخته نخواهد شد.",
                          size=12, color=Palette.MUTED_LIGHT),
                        btn_ghost(
                            "تلاش برای اتصال سرور",
                            lambda e: self.spawn(self._retry_backend()),
                        ),
                    ),
                ], spacing=16, scroll=ft.ScrollMode.AUTO),
                padding=16,
                expand=True,
            )
        category = ft.Dropdown(
            label="نوع بازخورد",
            value="suggestion",
            options=[
                ft.dropdown.Option("suggestion", "پیشنهاد"),
                ft.dropdown.Option("complaint", "انتقاد"),
                ft.dropdown.Option("bug", "گزارش مشکل"),
            ],
        )
        message = ft.TextField(
            label="پیام",
            multiline=True,
            min_lines=4,
            max_lines=8,
            max_length=4000,
        )
        ticket = tf("شماره پیگیری برای بررسی پاسخ")
        result_text = T("", size=12, color=Palette.MUTED_LIGHT)

        async def submit(e=None):
            content = (message.value or "").strip()
            if len(content) < 5:
                return self.toast("پیام باید دست‌کم ۵ نویسه باشد.", error=True)
            try:
                result = await self.api.post("/feedback", {
                    "category": category.value,
                    "message": content,
                })
            except ApiError as exc:
                self.toast(str(exc), error=True)
                return
            if not isinstance(result, dict) or not result.get("ticket"):
                return self.toast(
                    "پاسخ سرور شماره پیگیری معتبر نداشت.", error=True
                )
            ticket.value = result["ticket"]
            message.value = ""
            result_text.value = (
                f"ثبت شد. شماره پیگیری را نگه دارید: {ticket.value}"
            )
            self.page.update()

        async def lookup(e=None):
            code = (ticket.value or "").strip()
            if not code:
                return self.toast("شماره پیگیری را وارد کنید.", error=True)
            try:
                result = await self.api.get(
                    "/feedback", params={"ticket": code}
                )
            except ApiError as exc:
                result_text.value = str(exc)
                self.page.update()
                return
            statuses = {
                "open": "باز",
                "in_progress": "در حال بررسی",
                "resolved": "پاسخ داده‌شده",
            }
            result_text.value = (
                f"وضعیت: {statuses.get(result.get('status'), 'نامشخص')}\n"
                f"پاسخ: {result.get('reply') or 'هنوز پاسخی ثبت نشده است.'}"
            )
            self.page.update()

        children = [
            self.gradient_header("پیشنهاد و گزارش مشکل"),
            ft.Container(
                content=surface_card(
                    T("ارسال بدون نام و اطلاعات حساب",
                      size=15, weight=ft.FontWeight.BOLD,
                      color=Palette.TEXT_LIGHT),
                    T("نام کاربری، شناسه حساب و IP در رکورد بازخورد ذخیره "
                      "نمی‌شوند. شماره پیگیری را برای دریافت پاسخ نگه دارید.",
                      size=11, color=Palette.MUTED_LIGHT),
                    category,
                    message,
                    btn_primary("ارسال بازخورد", submit, expand=True),
                    ft.Divider(),
                    ticket,
                    btn_ghost("پیگیری", lookup, expand=True),
                    result_text,
                ),
                padding=16,
            ),
        ]
        if (self.me or {}).get("role") == "owner":
            children.append(ft.Container(
                content=btn_ghost(
                    "مدیریت بازخوردهای دریافتی",
                    lambda e: self.spawn(self._show_feedback_admin()),
                    expand=True,
                ),
                padding=16,
            ))
        return ft.Container(
            content=ft.Column(children, scroll=ft.ScrollMode.AUTO),
            expand=True,
        )

    async def _show_feedback_admin(self):
        try:
            feedback = await self.api.get("/feedback/admin")
        except ApiError as exc:
            self.toast(str(exc), error=True)
            return
        items = []
        for entry in feedback:
            status = ft.Dropdown(
                label="وضعیت",
                value=entry.get("status", "open"),
                options=[
                    ft.dropdown.Option("open", "باز"),
                    ft.dropdown.Option("in_progress", "در حال بررسی"),
                    ft.dropdown.Option("resolved", "پاسخ داده‌شده"),
                ],
            )
            reply = ft.TextField(
                label="پاسخ (اختیاری)",
                value=entry.get("reply") or "",
                multiline=True,
                min_lines=1,
                max_lines=3,
            )

            async def save_item(code=entry["ticket"], state=status, answer=reply):
                try:
                    await self.api.patch(
                        f"/feedback/admin/{code}",
                        {"status": state.value, "reply": answer.value or ""},
                    )
                except ApiError as exc:
                    self.toast(str(exc), error=True)
                else:
                    self.toast(f"پاسخ {code} به‌روز شد.")

            items.append(surface_card(
                T(
                    f"{entry['ticket']} · {entry.get('category', '')}",
                    weight=ft.FontWeight.BOLD,
                ),
                T(entry.get("message", ""), size=12, selectable=True),
                status,
                reply,
                ft.TextButton(
                    "ذخیره پاسخ و وضعیت",
                    on_click=lambda e, callback=save_item:
                    self.spawn(callback()),
                ),
            ))
        if not items:
            items.append(T("بازخوردی ثبت نشده است.",
                           color=Palette.MUTED_LIGHT))
        self.page.show_dialog(ft.AlertDialog(
            title=T("مدیریت بازخورد", weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(items, scroll=ft.ScrollMode.AUTO),
                width=360, height=480,
            ),
            actions=[ft.TextButton(
                "بستن", on_click=lambda e: self.page.pop_dialog()
            )],
        ))

    async def _add_channel_dialog(self):
        cid = tf("شناسه یا لینک کانال/گروه",
                 hint_text="مثلاً eitaa.com/mychannel")
        title = tf("نام نمایشی", hint_text="اختیاری")

        async def add(e=None):
            if not (cid.value or "").strip():
                return self.toast("شناسه کانال را وارد کنید", error=True)
            added = await self.guard(
                self._add_channel(cid.value, title.value or ""))
            if added:
                self.page.pop_dialog()
                self.toast("✓ کانال اضافه شد")
                await self.go("settings")

        self.page.show_dialog(ft.AlertDialog(
            title=T("افزودن کانال", weight=ft.FontWeight.BOLD),
            content=ft.Column([cid, title], tight=True, spacing=10),
            actions=[
                ft.TextButton("لغو", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("افزودن",
                                on_click=lambda e: self.spawn(add())),
            ],
        ))

    async def _about(self):
        self.page.show_dialog(ft.AlertDialog(
            content=ft.Column([
                ft.Row([self._logo(72)], alignment=ft.MainAxisAlignment.CENTER),
                ft.Container(height=12),
                T(config.APP_NAME, size=18, weight=ft.FontWeight.BOLD,
                  text_align=ft.TextAlign.CENTER),
                T(config.APP_TAGLINE, size=13, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                ft.Container(height=8),
                T(f"نسخه {digits(config.VERSION)}", size=12,
                  color=Palette.MUTED_LIGHT, text_align=ft.TextAlign.CENTER),
                T("مدیریت محتوای کانال‌های ایتا با ابزارهای انتشار و AI.",
                  size=12, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                T("تغییرات این نسخه: تاریخچه و خروجی، نسخه‌های قبلی، "
                  "پیگیری بازخورد، تنظیم ظاهر و ابزارهای تولید محتوا.",
                  size=11, color=Palette.MUTED_LIGHT,
                  text_align=ft.TextAlign.CENTER),
                ft.TextButton(
                    "مخزن GitHub",
                    icon=ft.Icons.CODE,
                    on_click=lambda e: self.spawn(self._open_external(
                        "https://github.com/Abolfazlsoltan/eitaa_app"
                    )),
                ),
                ft.TextButton(
                    "گزارش اشکال در GitHub",
                    icon=ft.Icons.BUG_REPORT_OUTLINED,
                    on_click=lambda e: self.spawn(self._open_external(
                        "https://github.com/Abolfazlsoltan/eitaa_app/issues"
                    )),
                ),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
               spacing=4, tight=True),
            actions=[ft.TextButton("بستن",
                                   on_click=lambda e: self.page.pop_dialog())],
        ))

    # ----------------------------------------------------------- helpers --
    def _empty_state(self, title, subtitle, icon):
        children = [
            ft.Container(height=60),
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
