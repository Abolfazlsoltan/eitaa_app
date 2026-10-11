"""Eitaa Manager — a modern client for managing Eitaa channels.

The app handles content creation with AI assistance, direct publishing to
EitaaYar, and lightweight analytics — all through a single, cohesive UI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from pathlib import Path

import flet as ft

import config
from api import Api, ApiError
from fa import digits, jdate, to_utc_iso


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
]

# Tabs shown in the bottom navigation bar, in order.
NAV_VIEWS = ["home", "posts", "templates", "settings"]


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

        self.me: dict = {"id": 0, "name": "کاربر", "role": "owner"}
        self.channels: list = []
        self.view = "home"
        self.theme_mode = "light"

        self.body = ft.Container(expand=True)
        self.api = Api(Storage(prefs))

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
        except Exception:
            # A storage hiccup must never leave the user on the splash screen.
            pass
        await self.enter()

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
        try:
            self.channels = await self.api.get("/channels")
        except ApiError:
            self.channels = []

        if not self.channels:
            self.page.navigation_bar = None
            self.page.floating_action_button = None
            return await self.onboarding()

        await self.go("home")

    def _nav_bar(self, selected: int):
        return ft.NavigationBar(
            selected_index=selected,
            bgcolor="#FFFFFF",
            indicator_color=ft.Colors.with_opacity(0.15, Palette.PRIMARY),
            on_change=lambda e: self.spawn(self.go_nav(int(e.control.selected_index))),
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.HOME_OUTLINED,
                                            selected_icon=ft.Icons.HOME,
                                            label="خانه"),
                ft.NavigationBarDestination(icon=ft.Icons.ARTICLE_OUTLINED,
                                            selected_icon=ft.Icons.ARTICLE,
                                            label="پست‌ها"),
                ft.NavigationBarDestination(icon=ft.Icons.FOLDER_OUTLINED,
                                            selected_icon=ft.Icons.FOLDER,
                                            label="قالب‌ها"),
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
            content = self._error_view("مشکلی پیش آمد. دوباره تلاش کنید.", name)

        self.body.content = content
        self.page.update()

    def _fab_for(self, view):
        if view in ("home", "posts"):
            return ft.FloatingActionButton(
                icon=ft.Icons.ADD,
                bgcolor=Palette.PRIMARY,
                foreground_color="#FFFFFF",
                on_click=lambda e: self.spawn(self.go("new")),
            )
        return None

    def _error_view(self, message, back_to="home"):
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
        cid = tf("شناسه یا لینک کانال", hint_text="مثلاً eitaa.com/mychannel")
        title = tf("نام نمایشی", hint_text="اختیاری")

        async def connect(e=None):
            if not cid.value or not cid.value.strip():
                return self.toast("شناسه کانال را وارد کنید", error=True)
            added = await self.guard(
                self._add_channel(cid.value, title.value or ""))
            if added:
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
                ft.Container(height=24),
                cid,
                title,
                ft.Container(height=16),
                ft.Row([btn_primary("اتصال", lambda e: self.spawn(connect()))],
                       alignment=ft.MainAxisAlignment.CENTER),
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
                    self._stat_tile("منتشرشده", stats.get("published", 0),
                                    ft.Icons.CHECK_CIRCLE, Palette.SUCCESS),
                    self._stat_tile("در صف", stats.get("queued", 0),
                                    ft.Icons.SCHEDULE, Palette.WARNING),
                ], spacing=12),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(height=12),
            ft.Container(
                content=ft.Row([
                    self._stat_tile("خطا", stats.get("failed", 0),
                                    ft.Icons.ERROR_OUTLINE, Palette.DANGER),
                    self._stat_tile("هوش مصنوعی", stats.get("ai_calls", 0),
                                    ft.Icons.AUTO_AWESOME, Palette.PRIMARY),
                ], spacing=12),
                padding=ft.Padding.symmetric(horizontal=16),
            ),
            ft.Container(height=24),

            # ---- quick action ----
            ft.Container(
                content=ft.Container(
                    content=ft.Row([
                        ft.Column([
                            T("ایده داری؟", size=16, weight=ft.FontWeight.BOLD,
                              color="#FFFFFF"),
                            T("با هوش مصنوعی پست بساز", size=12,
                              color="#FFFFFFC0"),
                        ], spacing=2, expand=True),
                        ft.Container(
                            content=ft.Icon(ft.Icons.ARROW_BACK, color="#FFFFFF",
                                            size=22),
                            padding=12, border_radius=12,
                            bgcolor="#FFFFFF30",
                        ),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    padding=20,
                    border_radius=20,
                    gradient=ft.LinearGradient(
                        begin=ft.Alignment.TOP_LEFT,
                        end=ft.Alignment.BOTTOM_RIGHT,
                        colors=[Palette.ACCENT, Palette.PRIMARY],
                    ),
                ),
                padding=ft.Padding.symmetric(horizontal=16),
                on_click=lambda e: self.spawn(self.go("new")),
            ),
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

        if status in CANCELLABLE:
            body.append(ft.Row([
                ft.TextButton(
                    "لغو",
                    icon=ft.Icons.CLOSE,
                    on_click=lambda e, i=post["id"]: self.spawn(
                        self._cancel_post(i)),
                ),
            ], alignment=ft.MainAxisAlignment.END))

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

    async def _cancel_post(self, post_id: int):
        if not await self.confirm("این پست لغو شود؟"):
            return
        if await self.guard(self.api.post(f"/posts/{post_id}/cancel")) is not None:
            self.toast("✓ لغو شد")
            await self.go("posts")

    # -------------------------------------------------------- new post -----
    async def v_new(self):
        templates = await self._safe_get("/templates", [])
        self.channels = await self._safe_get("/channels", self.channels)

        # With a single channel there is nothing to choose: pre-select it.
        state = {
            "selected": {c["id"] for c in self.channels} if len(self.channels) == 1 else set(),
            "silent": False,
            "pin": False,
            "busy": False,
        }

        title = tf("عنوان پست", hint_text="اختیاری")
        text = ft.TextField(
            label="متن پست",
            hint_text="متن خود را اینجا بنویسید…",
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
        prompt = tf("موضوع یا ایده", hint_text="مثلاً: معرفی اپلیکیشن جدید")

        async def run_ai(action: str):
            if state["busy"]:
                return
            state["busy"] = True
            try:
                result = await self.api.ai(
                    action, text.value or "",
                    (prompt.value or "") if action == "create" else "",
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
                scheduled_at = to_utc_iso(local_time)

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
                }
                if scheduled_at:
                    payload["scheduled_at"] = scheduled_at
                await self.api.post("/posts", payload)
                success = {
                    "now": "درخواست انتشار ثبت شد",
                    "schedule": "زمان‌بندی ثبت شد",
                    "draft": "پیش‌نویس ذخیره شد",
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
                bgcolor="#F1F5F9",
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
                T("برای انتشار خودکار باید به سرور متصل باشید.",
                  size=12, color=Palette.MUTED_LIGHT),
                ft.Row([scheduled_date, scheduled_time], spacing=12),
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
                T("پست جدید", size=18, weight=ft.FontWeight.BOLD,
                  color="#FFFFFF", expand=True),
            ]),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            gradient=ft.LinearGradient(
                colors=[Palette.PRIMARY, Palette.PRIMARY_DARK]),
        )

        # ---- bottom action bar ----
        action_bar = ft.Container(
            content=ft.Column([
                ft.Row([
                    btn_ghost("ذخیره پیش‌نویس",
                              lambda e: self.spawn(save("draft")), expand=True),
                    btn_ghost("زمان‌بندی",
                              lambda e: self.spawn(save("schedule")), expand=True),
                ], spacing=12),
                btn_primary("انتشار",
                            lambda e: self.spawn(save("now")), expand=True),
            ], spacing=8),
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

    # --------------------------------------------------------- templates --
    async def v_templates(self):
        templates = await self._safe_get("/templates", [])

        children = [
            self.gradient_header(
                "قالب‌ها",
                subtitle="متن‌های آماده برای استفاده سریع",
            ),
            ft.Container(height=24),
        ]
        if templates:
            children.extend(self._template_card(t) for t in templates)
        else:
            children.append(self._empty_state(
                "قالبی وجود ندارد", None, ft.Icons.FOLDER_OUTLINED))
        children.append(ft.Container(height=100))

        return ft.Container(
            content=ft.Column(children, scroll=ft.ScrollMode.AUTO, spacing=0),
            expand=True,
        )

    def _template_card(self, template: dict):
        return ft.Container(
            content=ft.Container(
                content=ft.Column([
                    T(template["name"], size=15, weight=ft.FontWeight.BOLD,
                      color=Palette.TEXT_LIGHT),
                    T(template["body"], size=12, color=Palette.MUTED_LIGHT,
                      max_lines=3, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=6, tight=True),
                padding=16,
                border_radius=18,
                bgcolor=Palette.CARD_LIGHT,
                shadow=ft.BoxShadow(blur_radius=14, color="#0F172A08",
                                    offset=ft.Offset(0, 3)),
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=6),
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
                                T("کاربر", size=16,
                                  weight=ft.FontWeight.BOLD,
                                  color=Palette.TEXT_LIGHT),
                                T(f"{digits(channel_count)} کانال متصل",
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

    def _menu_row(self, icon, title, subtitle, on_click):
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
            ink=True,
            on_click=on_click,
        )

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
                    T("کانال‌های متصل", size=18, weight=ft.FontWeight.BOLD),
                    ft.Container(height=8),
                    *rows,
                    ft.Container(height=8),
                    ft.Row([btn_ghost("＋ افزودن کانال", add_clicked)],
                           alignment=ft.MainAxisAlignment.CENTER),
                    ft.Container(height=20),
                ], spacing=4, tight=True, scroll=ft.ScrollMode.AUTO),
                padding=20,
            ),
        ))

    async def _add_channel_dialog(self):
        cid = tf("شناسه یا لینک کانال", hint_text="مثلاً eitaa.com/mychannel")
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
    page.theme_mode = ft.ThemeMode.LIGHT
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
    page.add(ft.SafeArea(content=app.body, expand=True))
    await app.boot()


if __name__ == "__main__":
    ft.run(main)
