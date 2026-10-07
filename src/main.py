"""Eitaa Channel Manager — Android/desktop client.

This module is the entire UI. It never talks to a backend directly; all
data goes through `api.Api`, which routes to either a local on-device
backend (`local.py`) or a remote FastAPI server, depending on
`config.MODE`.

Local mode (`config.MODE == "local"`):
    * No login, no registration, no server address.
    * AI calls go straight to Groq, publishing goes straight to EitaaYar.
    * The credentials are baked into `config.py` and never shown.
    * Features that require a 24/7 worker (scheduling, automations,
      media library, team, advanced analytics) are hidden from the UI.

Server mode (`config.MODE == "server"`):
    * The app behaves as before: it talks to FastAPI at `config.SERVER_URL`.
    * Full login flow, roles, scheduling, media library, analytics.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import flet as ft

import config
from api import Api, ApiError
from fa import digits, jdate, jfull_utc, to_local, to_utc_iso


# ================================================================ constants ==

PERMS = {
    "owner": {"*"},
    "editor": {"edit"},
    "approver": {"approve"},
    "publisher": {"publish"},
    "analyst": {"analytics"},
}


def can(role: str, perm: str) -> bool:
    perms = PERMS.get(role, set())
    return "*" in perms or perm in perms


ROLE_FA = {
    "owner": "مالک",
    "editor": "ویرایشگر",
    "approver": "تأییدکننده",
    "publisher": "منتشرکننده",
    "analyst": "تحلیلگر",
}

ST = {
    "draft": "پیش‌نویس",
    "pending": "در انتظار تأیید",
    "scheduled": "زمان‌بندی‌شده",
    "queued": "در صف",
    "sending": "در حال ارسال",
    "published": "ارسال‌شده",
    "failed": "ناموفق",
    "cancelled": "لغوشده",
}

AI_ACTIONS = [
    ("create", "✨ ساخت پست"),
    ("rewrite", "✏ بازنویسی"),
    ("shorter", "↔ کوتاه‌تر"),
    ("longer", "↕ کامل‌تر"),
    ("formal", "رسمی"),
    ("casual", "خودمانی"),
    ("news", "خبری"),
    ("educational", "آموزشی"),
    ("engaging", "جذاب‌تر"),
    ("titles", "عنوان"),
    ("hashtags", "هشتگ"),
    ("translate", "ترجمه"),
    ("summarize", "خلاصه"),
    ("proofread", "اصلاح نگارشی"),
]

WD = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


# ============================================================== small widgets

T = lambda s, **k: ft.Text(str(s), **k)


def tf(label, **k):
    return ft.TextField(label=label, **k)


def btn(text, fn, primary=False, **k):
    cls = ft.FilledButton if primary else ft.OutlinedButton
    return cls(content=text, on_click=fn, **k)


def card(*c, **k):
    return ft.Container(
        content=ft.Column(list(c), spacing=6),
        padding=12,
        border_radius=12,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        **k,
    )


def dd(label, opts, value=None, on_select=None):
    return ft.Dropdown(
        label=label,
        value=value,
        options=[ft.DropdownOption(key=str(k), text=t) for k, t in opts],
        on_select=on_select,
    )


def hscroll(*c):
    return ft.Row(list(c), scroll=ft.ScrollMode.AUTO, spacing=6)


def header(title, back=None, app=None):
    row: list = [T(title, size=20, weight=ft.FontWeight.BOLD, expand=True)]
    if back and app:
        row.insert(
            0,
            ft.IconButton(
                icon=ft.Icons.ARROW_FORWARD,
                on_click=lambda e: app.spawn(app.go(back)),
            ),
        )
    return ft.Row(row)


# ==================================================================== Store ==

class Store:
    """Thin async wrapper over Flet's client_storage."""

    def __init__(self, page: ft.Page):
        self.page = page

    async def get(self, key):
        try:
            return await self.page.client_storage.get_async(key)
        except Exception:
            return None

    async def set(self, key, value):
        try:
            await self.page.client_storage.set_async(key, value)
        except Exception:
            pass

    async def remove(self, key):
        try:
            await self.page.client_storage.remove_async(key)
        except Exception:
            pass


# ====================================================================== App ==

class App:
    def __init__(
        self,
        page: ft.Page,
        loop: asyncio.AbstractEventLoop,
        store: Store | None = None,
    ):
        self.page = page
        self.loop = loop
        self.store = store or Store(page)
        self.api = Api(storage=self.store)

        self.me: dict | None = None
        self.chs: list[dict] = []
        self.view = "boot"
        self.theme_pref = "system"

        self._logging_out = False
        self._fp: ft.FilePicker | None = None

        self.body = ft.Container(expand=True, padding=ft.Padding.all(12))

    # ------------------------------------------------------------- helpers --
    def spawn(self, coro):
        """Schedule a coroutine on the Flet main loop (thread-safe)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def toast(self, message: str):
        self.page.show_dialog(ft.SnackBar(content=T(message)))

    async def guard(self, coro):
        """Await a coroutine; on ApiError show a toast and return None."""
        try:
            return await coro
        except ApiError as e:
            self.toast("⚠ " + str(e))
            return None

    async def confirm(self, message: str) -> bool:
        fut = self.loop.create_future()

        def finish(value):
            def handler(e):
                if not fut.done():
                    fut.set_result(value)
                self.page.pop_dialog()
            return handler

        self.page.show_dialog(
            ft.AlertDialog(
                content=T(message),
                actions=[
                    ft.TextButton(content="بله", on_click=finish(True)),
                    ft.TextButton(content="خیر", on_click=finish(False)),
                ],
            )
        )
        return await fut

    async def pick_date(self):
        fut = self.loop.create_future()
        now = dt.datetime.now()

        def on_change(e):
            if not fut.done():
                fut.set_result(e.control.value)

        def on_dismiss(e):
            if not fut.done():
                fut.set_result(None)

        self.page.show_dialog(
            ft.DatePicker(
                first_date=now - dt.timedelta(days=1),
                last_date=now + dt.timedelta(days=730),
                on_change=on_change,
                on_dismiss=on_dismiss,
            )
        )
        return await fut

    async def pick_time(self):
        fut = self.loop.create_future()

        def on_change(e):
            if not fut.done():
                fut.set_result(e.control.value)

        def on_dismiss(e):
            if not fut.done():
                fut.set_result(None)

        self.page.show_dialog(
            ft.TimePicker(on_change=on_change, on_dismiss=on_dismiss)
        )
        return await fut

    async def open_url(self, url: str):
        await self.page.launch_url(url)

    # ------------------------------------------------------------- layout --
    def center(self, *controls):
        return ft.Container(
            content=ft.Column(
                list(controls),
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=14,
                scroll=ft.ScrollMode.AUTO,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
            padding=24,
        )

    def logo(self, size=84, icon=ft.Icons.CAMPAIGN):
        return ft.Container(
            content=ft.Icon(icon, size=size // 2, color=ft.Colors.WHITE),
            width=size,
            height=size,
            border_radius=size // 3.5,
            alignment=ft.Alignment(0, 0),
            gradient=ft.LinearGradient(
                colors=[ft.Colors.CYAN_400, ft.Colors.BLUE_600]
            ),
        )

    def show(self, content):
        self.body.content = content
        self.page.update()

    def apply_theme(self, mode: str):
        self.theme_pref = mode if mode in ("light", "dark") else "system"
        self.page.theme_mode = {
            "light": ft.ThemeMode.LIGHT,
            "dark": ft.ThemeMode.DARK,
        }.get(mode, ft.ThemeMode.SYSTEM)

    # ----------------------------------------------------------------- FAB --
    def _desired_fab(self):
        """Return the FAB that should be shown for the current view."""
        if not self.me:
            return None
        if self.view not in ("home", "posts", "calendar"):
            return None
        role = self.me.get("role", "")
        if not (can(role, "edit") or can(role, "publish")):
            return None
        return ft.FloatingActionButton(
            icon=ft.Icons.ADD,
            on_click=lambda e: self.spawn(self.go("new")),
        )

    def _sync_fab(self):
        self.page.floating_action_button = self._desired_fab()

    # ----------------------------------------------------------- status bar
    def _local_badge(self):
        if not self.api.is_local:
            return None
        return ft.Container(
            content=T("حالت محلی", size=10, color=ft.Colors.WHITE),
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border_radius=8,
            bgcolor=ft.Colors.ORANGE_700,
        )

    # ============================================================ boot flow =
    async def boot(self):
        self.apply_theme(await self.store.get("theme") or "system")
        self.view = "boot"
        self.page.navigation_bar = None
        self.page.floating_action_button = None

        self.show(self.center(
            self.logo(96),
            T(config.APP_NAME, size=22, weight=ft.FontWeight.BOLD),
            ft.ProgressRing(width=24, height=24, stroke_width=3),
        ))

        # --- provision the backend (local: install test credentials) -----
        try:
            await self.api.bootstrap()
        except ApiError as e:
            return self.show_error(str(e))

        # --- local mode: no login, go straight to the dashboard ----------
        if self.api.is_local:
            try:
                self.me = await self.api.get("/auth/me")
            except ApiError as e:
                return self.show_error(str(e))

            if not await self.store.get("intro_done"):
                return self.show_intro(None)
            return await self.enter()

        # --- server mode: original behaviour -----------------------------
        server = await self.store.get("server")
        self.api.base = (server or config.SERVER_URL or "").rstrip("/")
        if not self.api.base:
            return self.show_unconfigured()

        self.api.token = await self.store.get("token")
        state = None
        try:
            if self.api.token:
                try:
                    self.me = await self.api.get("/auth/me")
                    return await self.enter()
                except ApiError as e:
                    if e.network:
                        raise
                    await self.store.remove("token")
                    self.api.token = None
            state = await self.api.get("/auth/state")
        except ApiError as e:
            return self.show_offline(str(e))

        if not await self.store.get("intro_done"):
            return self.show_intro(state)
        await self.show_auth(state)

    # --------------------------------------------------------- error screens
    def show_error(self, message: str):
        self.view = "error"
        self.show(self.center(
            ft.Icon(ft.Icons.WARNING_AMBER, size=64, color=ft.Colors.ERROR),
            T("خطا", size=20, weight=ft.FontWeight.BOLD),
            T(message, text_align=ft.TextAlign.CENTER, color=ft.Colors.OUTLINE),
            btn("تلاش دوباره", lambda e: self.spawn(self.boot()), True),
        ))

    def show_offline(self, message: str):
        self.view = "offline"
        self.show(self.center(
            ft.Icon(ft.Icons.CLOUD_OFF, size=64, color=ft.Colors.OUTLINE),
            T("اتصال برقرار نشد", size=20, weight=ft.FontWeight.BOLD),
            T(message, text_align=ft.TextAlign.CENTER, color=ft.Colors.OUTLINE),
            btn("تلاش دوباره", lambda e: self.spawn(self.boot()), True),
            ft.TextButton(
                content="تنظیمات اتصال",
                on_click=lambda e: self.conn_dialog(),
            ),
        ))

    def show_unconfigured(self):
        self.view = "unconfigured"
        self.show(self.center(
            self.logo(),
            T("سرور تنظیم نشده است", size=20, weight=ft.FontWeight.BOLD),
            T("آدرس سرور در config.py وارد نشده است.",
              text_align=ft.TextAlign.CENTER, color=ft.Colors.OUTLINE),
            btn("تنظیمات اتصال", lambda e: self.conn_dialog(), True),
        ))

    # --------------------------------------------------------- conn dialog --
    def conn_dialog(self):
        """Advanced: override the server address at runtime.

        Only useful in server mode; hidden in local mode.
        """
        if self.api.is_local:
            self.toast("در حالت محلی نیازی به آدرس سرور نیست.")
            return

        v = tf(
            "آدرس سرور",
            value=self.api.base or config.SERVER_URL,
            keyboard_type=ft.KeyboardType.URL,
            hint_text="https://…",
        )

        async def save(e):
            self.page.pop_dialog()
            await self.store.set("server", v.value.strip().rstrip("/"))
            await self.boot()

        async def reset(e):
            self.page.pop_dialog()
            await self.store.remove("server")
            await self.boot()

        self.page.show_dialog(ft.AlertDialog(
            title=T("تنظیمات اتصال"),
            content=v,
            actions=[
                ft.TextButton(
                    content="ذخیره",
                    on_click=lambda e: self.spawn(save(e)),
                ),
                ft.TextButton(
                    content="پیش‌فرض",
                    on_click=lambda e: self.spawn(reset(e)),
                ),
                ft.TextButton(
                    content="بستن",
                    on_click=lambda e: self.page.pop_dialog(),
                ),
            ],
        ))

    # ---------------------------------------------------------------- intro
    def show_intro(self, state):
        slides = [
            (ft.Icons.AUTO_AWESOME, "محتوا را با هوش مصنوعی بساز",
             "ایده را بده؛ پست آماده می‌شود. بازنویسی، خلاصه، عنوان و هشتگ هم با یک ضربه."),
            (ft.Icons.SEND, "ارسال مستقیم به کانال",
             "پست را همان لحظه به کانال‌های متصل بفرست و نتیجه را ببین."),
            (ft.Icons.CAMPAIGN, "چند کانال، یک مرکز فرماندهی",
             "یک پست را هم‌زمان برای چند کانال بفرست و وضعیت هر کدام را جدا ببین."),
        ]
        i = {"v": 0}
        is_local = self.api.is_local

        async def done(e=None):
            await self.store.set("intro_done", "1")
            if is_local:
                await self.enter()
                return
            if state is None:
                try:
                    state2 = await self.api.get("/auth/state")
                except ApiError as ex:
                    return self.show_offline(str(ex))
                await self.show_auth(state2)
            else:
                await self.show_auth(state)

        def render():
            ic, title, desc = slides[i["v"]]
            last = i["v"] == len(slides) - 1
            dots = ft.Row(
                [
                    ft.Container(
                        width=22 if k == i["v"] else 8,
                        height=8,
                        border_radius=4,
                        bgcolor=ft.Colors.PRIMARY if k == i["v"] else ft.Colors.OUTLINE_VARIANT,
                    )
                    for k in range(len(slides))
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=6,
            )

            def nxt(e):
                if last:
                    self.spawn(done())
                else:
                    i["v"] += 1
                    render()

            self.show(ft.Column([
                ft.Row(
                    [ft.TextButton(content="رد کردن",
                                   on_click=lambda e: self.spawn(done()))],
                    alignment=ft.MainAxisAlignment.END,
                ),
                self.center(
                    self.logo(120, ic),
                    T(title, size=22, weight=ft.FontWeight.BOLD,
                      text_align=ft.TextAlign.CENTER),
                    T(desc, text_align=ft.TextAlign.CENTER,
                      color=ft.Colors.OUTLINE),
                    dots,
                ),
                ft.Row([ft.FilledButton(
                    content="شروع کنیم" if last else "بعدی",
                    on_click=nxt, height=48, expand=True,
                )]),
            ], expand=True))

        render()

    # ============================================================ auth flow =
    async def show_auth(self, state=None):
        if state is None:
            try:
                state = await self.api.get("/auth/state")
            except ApiError as e:
                return self.show_offline(str(e))

        has_users = state["has_users"]
        mode = {"v": "login" if has_users else "signup"}
        busy = {"v": False}

        user = tf("نام کاربری", prefix_icon=ft.Icons.PERSON_OUTLINE,
                  autofocus=True, border_radius=12, autocorrect=False)
        pw = tf("رمز عبور", prefix_icon=ft.Icons.LOCK_OUTLINE,
                password=True, can_reveal_password=True, border_radius=12)
        pw2 = tf("تکرار رمز عبور", prefix_icon=ft.Icons.LOCK_OUTLINE,
                 password=True, can_reveal_password=True, border_radius=12)
        name = tf("نام نمایشی", prefix_icon=ft.Icons.BADGE, border_radius=12)

        msg = T("", color=ft.Colors.ERROR, size=13,
                text_align=ft.TextAlign.CENTER)
        info = T("", size=13, color=ft.Colors.OUTLINE,
                 text_align=ft.TextAlign.CENTER)
        go_btn = ft.FilledButton(content="ورود", height=48, expand=True)
        tabs = ft.Row(spacing=8)

        def render():
            login = mode["v"] == "login"
            pw2.visible = name.visible = not login
            msg.value = ""
            pw.error = pw2.error = user.error = None
            if login:
                info.value = ""
            else:
                info.value = (
                    "ثبت‌نام آزاد بسته است؛ از مالک فضای کاری بخواهید شما را اضافه کند."
                    if has_users
                    else "اولین حساب، مالک فضای کاری می‌شود و می‌تواند اعضای تیم را اضافه کند."
                )
            go_btn.disabled = busy["v"] or (not login and has_users)
            go_btn.content = (
                ft.ProgressRing(width=20, height=20, stroke_width=2,
                                color=ft.Colors.WHITE)
                if busy["v"] else ("ورود" if login else "ساخت حساب")
            )
            tabs.controls = [
                (ft.FilledButton if mode["v"] == k else ft.OutlinedButton)(
                    content=l, expand=True,
                    on_click=lambda e, k=k: (mode.__setitem__("v", k), render()),
                )
                for k, l in (("login", "ورود"), ("signup", "ساخت حساب"))
            ]
            self.page.update()

        async def submit(e=None):
            if busy["v"] or go_btn.disabled:
                return
            login = mode["v"] == "login"
            ok = True

            if not login and len(user.value.strip()) < 3:
                user.error = "نام کاربری حداقل ۳ نویسه باشد"
                ok = False
            if not login and len(pw.value) < 8:
                pw.error = "رمز عبور حداقل ۸ نویسه باشد"
                ok = False
            if login and not pw.value:
                pw.error = "رمز عبور را وارد کن"
                ok = False
            if not login and pw.value != pw2.value:
                pw2.error = "تکرار رمز عبور یکسان نیست"
                ok = False
            if not ok:
                self.page.update()
                return

            busy["v"] = True
            render()
            try:
                r = await self.api.post(
                    "/auth/login" if login else "/auth/register",
                    {
                        "username": user.value,
                        "password": pw.value,
                        "display_name": name.value,
                    },
                )
                await self.signed_in(r)
            except ApiError as ex:
                busy["v"] = False
                render()
                msg.value = "⚠ " + str(ex)
                self.page.update()

        go_btn.on_click = lambda e: self.spawn(submit())
        pw.on_submit = lambda e: self.spawn(submit())
        pw2.on_submit = pw.on_submit

        self.view = "auth"
        self.show(ft.Column([
            ft.Row(
                [ft.IconButton(
                    icon=ft.Icons.TUNE,
                    tooltip="تنظیمات اتصال",
                    icon_color=ft.Colors.OUTLINE,
                    on_click=lambda e: self.conn_dialog(),
                )],
                alignment=ft.MainAxisAlignment.END,
            ),
            ft.Column([
                ft.Row([self.logo(72)], alignment=ft.MainAxisAlignment.CENTER),
                T(config.APP_NAME, size=22, weight=ft.FontWeight.BOLD,
                  text_align=ft.TextAlign.CENTER),
                T("کانال‌هایت را از یکجا مدیریت کن", color=ft.Colors.OUTLINE,
                  text_align=ft.TextAlign.CENTER),
                tabs, info, user, name, pw, pw2,
                ft.Row([go_btn]), msg,
            ], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True),
        ], expand=True))
        render()

    async def signed_in(self, r):
        self.api.token = r["token"]
        self.me = r["user"]
        await self.store.set("token", r["token"])
        await self.enter()

    async def logout(self, ask=False, expired=False):
        if ask and not await self.confirm("از حساب خارج می‌شوی؟"):
            return
        if self._logging_out:
            return
        self._logging_out = True
        try:
            await self.store.remove("token")
            self.api.token = None
            self.me = None
            self.api.on_401 = lambda: None
            self.page.navigation_bar = None
            self.page.floating_action_button = None
            await self.boot()
            if expired:
                self.toast("نشست تو منقضی شد؛ دوباره وارد شو")
        finally:
            self._logging_out = False

    def _handle_401(self):
        if self._logging_out:
            return
        self.spawn(self.logout(expired=True))

    # ========================================================== main shell ==
    async def enter(self):
        self.api.on_401 = self._handle_401
        try:
            self.chs = await self.api.get("/channels")
        except ApiError as e:
            return self.show_error(str(e))

        if not self.chs:
            if self.api.is_local:
                # Local mode without channels: offer to add one.
                self.view = "empty"
                self.page.floating_action_button = None
                return self.show(self.center(
                    self.logo(),
                    T("هنوز کانالی اضافه نشده است.", size=18,
                      weight=ft.FontWeight.BOLD,
                      text_align=ft.TextAlign.CENTER),
                    T("برای شروع، یک کانال اضافه کن.",
                      text_align=ft.TextAlign.CENTER, color=ft.Colors.OUTLINE),
                    btn("افزودن کانال",
                        lambda e: self.spawn(self.go("channels")), True),
                ))

            if self.me and self.me.get("role") == "owner":
                return await self.show_onboarding()

            self.view = "empty"
            self.page.floating_action_button = None
            return self.show(self.center(
                T("هنوز کانالی متصل نشده است. از مالک بخواهید کانالی اضافه کند.",
                  text_align=ft.TextAlign.CENTER),
                btn("خروج", lambda e: self.spawn(self.logout())),
            ))

        # Build the navigation bar. In local mode the entries that map to
        # unsupported features are hidden.
        self.page.navigation_bar = ft.NavigationBar(
            selected_index=0,
            on_change=lambda e: self.spawn(self.nav(int(e.control.selected_index))),
            destinations=self._nav_destinations(),
        )
        await self.go("home")

    def _nav_destinations(self):
        dests = [
            ft.NavigationBarDestination(icon=ft.Icons.HOME, label="خانه"),
            ft.NavigationBarDestination(icon=ft.Icons.EDIT_NOTE, label="پست‌ها"),
        ]
        if not self.api.is_local:
            dests.append(
                ft.NavigationBarDestination(icon=ft.Icons.CALENDAR_MONTH, label="تقویم")
            )
        dests.append(
            ft.NavigationBarDestination(icon=ft.Icons.MORE_HORIZ, label="بیشتر")
        )
        return dests

    async def nav(self, i: int):
        if self.api.is_local:
            self.go_targets = ["home", "posts", "more"]
        else:
            self.go_targets = ["home", "posts", "calendar", "more"]
        if 0 <= i < len(self.go_targets):
            await self.go(self.go_targets[i])

    async def go(self, name: str):
        self.view = name
        self.body.content = ft.ProgressRing()
        self.page.floating_action_button = None
        self.page.update()

        try:
            self.body.content = await getattr(self, "v_" + name)()
        except ApiError as e:
            self.body.content = card(
                T("⚠ " + str(e)),
                btn("تلاش دوباره", lambda e: self.spawn(self.go(name))),
            )
        except Exception as e:
            self.body.content = card(
                T("⚠ خطای نامشخص رخ داد."),
                T(str(e), size=11, color=ft.Colors.OUTLINE),
                btn("بازگشت", lambda e: self.spawn(self.go("home"))),
            )

        self._sync_fab()
        self.page.update()

    # ====================================================== onboarding (server)
    async def show_onboarding(self):
        """Server-mode onboarding. Not used in local mode."""
        s = {"step": 0, "conn": None}
        msg = T("", color=ft.Colors.ERROR)
        token = tf("توکن ایتایار", password=True, can_reveal_password=True)
        chan = tf("لینک، نام کاربری یا شناسه کانال")

        def render():
            st = s["step"]
            c: list = []
            if st == 0:
                c = [
                    T("مدیریت کانالت، ساده‌تر از همیشه", size=22,
                      weight=ft.FontWeight.BOLD),
                    T("ساخت محتوا، زمان‌بندی و انتشار چند کانال از یکجا"),
                    btn("شروع راه‌اندازی", lambda e: nxt(1), True),
                ]
            if st == 1:
                c = [T("چند کانال می‌خواهی مدیریت کنی؟", size=20)] + [
                    btn(l, lambda e: nxt(2))
                    for l in ["فقط ۱ کانال", "۲ تا ۵ کانال", "بیشتر از ۵ کانال"]
                ]
            if st == 2:
                c = [
                    T("برای اینکه ایتایار بتواند برای کانالت ارسال انجام دهد، "
                      "ابتدا @sender را به عنوان مدیر کانال اضافه کن."),
                    btn("باز کردن @sender در ایتا",
                        lambda e: self.spawn(self.open_url("https://eitaa.com/sender"))),
                    btn("انجام شد", lambda e: nxt(3), True),
                ]
            if st == 3:
                c = [
                    T("حالا کانالت را در ایتایار اضافه کن."),
                    btn("باز کردن ایتایار",
                        lambda e: self.spawn(self.open_url("https://eitaayar.ir"))),
                    token,
                ]
                if s["conn"]:
                    c.append(T("✓ اتصال برقرار شد — " + s["conn"]["bot"],
                               color=ft.Colors.GREEN))
                    c.append(btn("ادامه", lambda e: nxt(4), True))
                else:
                    c.append(btn("بررسی اتصال",
                                 lambda e: self.spawn(connect()), True))
            if st == 4:
                c = [
                    T("افزودن کانال", size=20),
                    T("اگر شناسه کانال را از ایتایار داری، می‌توانی آن را مستقیم وارد کنی."),
                    chan,
                    btn("اتصال کانال", lambda e: self.spawn(add()), True),
                ]
            if st == 5:
                c = [
                    T("✓ کانال با موفقیت متصل شد", size=20,
                      color=ft.Colors.GREEN),
                    btn("افزودن کانال دیگر", lambda e: nxt(4)),
                    btn("ورود به داشبورد",
                        lambda e: self.spawn(self.enter()), True),
                ]
            self.body.content = ft.Column(
                [ft.ProgressBar(value=min(st, 5) / 5, border_radius=4)] + c + [msg],
                spacing=10, scroll=ft.ScrollMode.AUTO,
            )
            self.page.update()

        def nxt(i):
            s["step"] = i
            msg.value = ""
            render()

        async def connect():
            try:
                s["conn"] = await self.api.post("/connections",
                                                {"token": token.value.strip()})
                token.value = ""
                msg.value = ""
            except ApiError as e:
                msg.value = "⚠ " + str(e)
            render()

        async def add():
            try:
                await self.api.post("/channels", {
                    "connection_id": s["conn"]["id"],
                    "value": chan.value,
                })
                chan.value = ""
                nxt(5)
            except ApiError as e:
                msg.value = "⚠ " + str(e)
                render()

        if not s["conn"]:
            cs = await self.api.get("/connections")
            if cs:
                s["conn"] = cs[0]
        render()

    # =============================================================== views ==
    async def v_more(self):
        items = [
            ("media", "کتابخانه", ft.Icons.FOLDER),
            ("automation", "اتوماسیون", ft.Icons.BOLT),
            ("analytics", "آمار", ft.Icons.BAR_CHART),
            ("channels", "کانال‌ها", ft.Icons.CAMPAIGN),
            ("templates", "قالب‌ها", ft.Icons.ARTICLE),
            ("settings", "تنظیمات", ft.Icons.SETTINGS),
        ]
        # Local mode: hide everything that needs a server.
        if self.api.is_local:
            items = [i for i in items if i[0] not in ("media", "automation", "analytics")]
        # Non-analyst roles: hide analytics.
        if not can(self.me["role"], "analytics"):
            items = [i for i in items if i[0] != "analytics"]

        title_row: list = [T("بیشتر", size=20, weight=ft.FontWeight.BOLD, expand=True)]
        badge = self._local_badge()
        if badge:
            title_row.append(badge)

        return ft.Column([
            ft.Row(title_row),
        ] + [
            ft.ListTile(
                leading=ft.Icon(icon),
                title=T(label),
                on_click=lambda e, k=key: self.spawn(self.go(k)),
            )
            for key, label, icon in items
        ], spacing=4)

    async def v_home(self):
        s = await self.api.get("/stats")
        act = await self.api.get("/activity")
        posts = await self.api.get("/posts")

        me = self.me
        role = me["role"]
        comp = can(role, "edit") or can(role, "publish")
        names = {c["id"]: c["title"] for c in self.chs}

        def stat(icon, color, label, value):
            return ft.Container(
                content=ft.Row([
                    ft.Container(
                        ft.Icon(icon, color=color),
                        padding=8, border_radius=10,
                        bgcolor=ft.Colors.with_opacity(0.12, color),
                    ),
                    ft.Column([
                        T(digits(value), size=20, weight=ft.FontWeight.BOLD),
                        T(label, size=11, color=ft.Colors.OUTLINE),
                    ], spacing=0),
                ], spacing=10),
                padding=12, border_radius=14,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                width=168,
            )

        hero_buttons: list = []
        if comp:
            hero_buttons.append(ft.FilledButton(
                content="+ ساخت پست",
                on_click=lambda e: self.spawn(self.go("new")),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.WHITE,
                    color=ft.Colors.BLUE_700,
                ),
            ))

        hero = ft.Container(
            content=ft.Column([
                T("امروز چه چیزی منتشر کنیم؟", size=18,
                  weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
            ] + hero_buttons, spacing=12),
            padding=18, border_radius=18,
            gradient=ft.LinearGradient(
                colors=[ft.Colors.CYAN_500, ft.Colors.BLUE_700]
            ),
        )

        # Upcoming: only meaningful when scheduling exists (server mode).
        upcoming_ui: list = []
        if not self.api.is_local:
            nxt = sorted(
                [p for p in posts
                 if p["status"] in ("scheduled", "queued") and p["scheduled_at"]],
                key=lambda p: p["scheduled_at"],
            )[:3]
            upcoming_ui = [
                card(
                    T(p["title"] or p["text"][:40], weight=ft.FontWeight.BOLD),
                    T(
                        jfull_utc(p["scheduled_at"]) + " · " + "، ".join(
                            names.get(d["channel_id"], "") for d in p["deliveries"]
                        ),
                        size=12, color=ft.Colors.OUTLINE,
                    ),
                )
                for p in nxt
            ] or [card(
                T("برنامه انتشار تو خالی است.", color=ft.Colors.OUTLINE),
                *([btn("زمان‌بندی پست",
                       lambda e: self.spawn(self.go("new")))] if comp else []),
            )]

        activity_rows = [
            ft.ListTile(
                title=T(a["message"], size=13),
                subtitle=T(jfull_utc(a["at"]), size=11),
            )
            for a in act[:6]
        ] or [T("هنوز فعالیتی ثبت نشده است.", color=ft.Colors.OUTLINE)]

        greeting_row: list = [
            T(f'سلام {me["name"]} 👋', size=22, weight=ft.FontWeight.BOLD,
              expand=True),
        ]
        badge = self._local_badge()
        if badge:
            greeting_row.append(badge)

        content: list = [
            ft.Row(greeting_row),
            T(jdate(dt.datetime.now()), size=12, color=ft.Colors.OUTLINE),
            hero,
            ft.Row([
                stat(ft.Icons.CHECK_CIRCLE, ft.Colors.GREEN,
                     "ارسال‌شده", s["published"]),
                stat(ft.Icons.SCHEDULE, ft.Colors.BLUE,
                     "زمان‌بندی‌شده", s["scheduled"]),
                stat(ft.Icons.HOURGLASS_TOP, ft.Colors.AMBER,
                     "در صف ارسال", s["queued"]),
                stat(ft.Icons.ERROR, ft.Colors.RED,
                     "خطاها", s["failed"]),
            ], wrap=True, spacing=8, run_spacing=8),
        ]

        if upcoming_ui:
            content.append(T("پست‌های بعدی", size=16, weight=ft.FontWeight.BOLD))
            content.extend(upcoming_ui)

        content.append(T("فعالیت اخیر", size=16, weight=ft.FontWeight.BOLD))
        content.extend(activity_rows)

        return ft.Column(content, scroll=ft.ScrollMode.AUTO, spacing=12)

    async def v_posts(self, flt: str = "all", q: str = ""):
        rows = await self.api.get("/posts")
        role = self.me["role"]
        names = {c["id"]: c["title"] for c in self.chs}

        search = tf(
            "جستجو در عنوان و متن",
            value=q,
            on_submit=lambda e: self.spawn(
                self.reload_posts(flt, e.control.value or "")
            ),
        )

        filter_keys = ["all", "draft", "queued", "published", "failed"]
        if not self.api.is_local:
            filter_keys = ["all", "draft", "pending", "scheduled",
                           "queued", "published", "failed"]

        tabs = hscroll(*[
            (ft.FilledButton if flt == k else ft.OutlinedButton)(
                content=("همه" if k == "all" else ST[k]),
                on_click=lambda e, k=k: self.spawn(self.reload_posts(k, q)),
            )
            for k in filter_keys
        ])

        out: list = []
        for p in [
            r for r in rows
            if (flt == "all" or r["status"] == flt)
            and q in (r["title"] + r["text"])
        ]:
            rows_in_card: list = [
                ft.Row([
                    T(p["title"] or p["text"][:40],
                      weight=ft.FontWeight.BOLD, expand=True),
                    T(ST.get(p["status"], p["status"]), size=12),
                ]),
            ]
            if p.get("scheduled_at"):
                rows_in_card.append(T(
                    jfull_utc(p["scheduled_at"]),
                    size=12, color=ft.Colors.OUTLINE,
                ))
            for d in p["deliveries"]:
                label = (
                    "✓ موفق" if d["status"] == "published"
                    else "✕ ناموفق" if d["status"] == "failed"
                    else ST.get(d["status"], d["status"])
                )
                line: list = [
                    T(f'{names.get(d["channel_id"], "")}: {label}', expand=True)
                ]
                if d["status"] == "failed" and can(role, "publish") and not self.api.is_local:
                    line.append(btn(
                        "تلاش دوباره",
                        lambda e, i=d["id"]: self.spawn(self.act(
                            f"/deliveries/{i}/retry", "posts",
                            "✓ در صف تلاش دوباره قرار گرفت",
                        )),
                    ))
                rows_in_card.append(ft.Row(line))
                if d.get("error"):
                    rows_in_card.append(
                        T(d["error"], size=11, color=ft.Colors.ERROR)
                    )
            if p["status"] == "pending" and can(role, "approve"):
                rows_in_card.append(ft.Row([
                    btn("تأیید",
                        lambda e, i=p["id"]: self.spawn(self.act(
                            f"/posts/{i}/approve", "posts", "✓ تأیید شد")),
                        True),
                    btn("رد",
                        lambda e, i=p["id"]: self.spawn(self.act(
                            f"/posts/{i}/reject", "posts"))),
                ]))
            if p["status"] in ("scheduled", "queued") and can(role, "publish"):
                rows_in_card.append(btn(
                    "لغو",
                    lambda e, i=p["id"]: self.spawn(self.cancel(i)),
                ))
            out.append(card(*rows_in_card))

        return ft.Column([
            header("پست‌ها"),
            tabs,
            search,
        ] + (out or [T("هنوز پستی در این بخش نیست.", color=ft.Colors.OUTLINE)]),
            scroll=ft.ScrollMode.AUTO, spacing=8)

    async def reload_posts(self, flt: str, q: str):
        self.body.content = await self.v_posts(flt, q)
        self._sync_fab()
        self.page.update()

    async def act(self, path: str, view: str, ok: str | None = None):
        await self.guard(self.api.post(path))
        if ok:
            self.toast(ok)
        await self.go(view)

    async def cancel(self, post_id: int):
        if await self.confirm("این پست لغو شود؟"):
            await self.act(f"/posts/{post_id}/cancel", "posts")

    async def v_new(self):
        role = self.me["role"]
        is_local = self.api.is_local

        tpls = await self.api.get("/templates")
        media = [] if is_local else await self.api.get("/media")

        st = {
            "sel": set(),
            "when": None,
            "silent": False,
            "pin": False,
            "repeat": "none",
            "busy": False,
        }

        title = tf("عنوان داخلی (نمایش داده نمی‌شود)")
        text = tf("متن پست…", multiline=True, min_lines=8)
        prompt = tf("از این اطلاعات یک پست مناسب برای کانال بساز…")
        views = tf("حذف پس از … بازدید (اختیاری)",
                   keyboard_type=ft.KeyboardType.NUMBER)
        count = tf("تعداد تکرار", value="4",
                   keyboard_type=ft.KeyboardType.NUMBER)
        when_lbl = T("زمان انتشار انتخاب نشده")

        def use_tpl(e):
            t = next((x for x in tpls if str(x["id"]) == e.control.value), None)
            if t:
                text.value = (
                    t["body"]
                    .replace("{{title}}", title.value)
                    .replace("{{date}}", jdate(dt.datetime.now()))
                )
                self.page.update()

        async def ai(action: str, pr: str = ""):
            try:
                result = await self.api.ai(action, text.value, pr)
                if action == "titles":
                    self.page.show_dialog(ft.AlertDialog(
                        content=T(result),
                        actions=[ft.TextButton(
                            content="بستن",
                            on_click=lambda e: self.page.pop_dialog(),
                        )],
                    ))
                else:
                    text.value = result
                    self.toast("✓ نتیجه آمد؛ بررسی و ویرایش کن")
                    self.page.update()
            except ApiError as e:
                self.toast("⚠ " + str(e))

        async def pick(e):
            d = await self.pick_date()
            if not d:
                return
            t = await self.pick_time()
            if not t:
                return
            st["when"] = dt.datetime.combine(d.date(), t)
            when_lbl.value = jdate(st["when"]) + digits(f" — {st['when']:%H:%M}")
            self.page.update()

        async def send(mode: str):
            if st["busy"]:
                return
            if mode != "draft" and not st["sel"]:
                return self.toast("⚠ حداقل یک کانال انتخاب کن")
            if mode == "schedule" and not st["when"]:
                return self.toast("⚠ ابتدا زمان انتشار را انتخاب کن")

            try:
                view_int = int(views.value) if (views.value or "").strip() else None
            except ValueError:
                return self.toast("⚠ «حذف پس از بازدید» باید عدد باشد")
            try:
                count_int = int(count.value) if (count.value or "").strip() else 1
            except ValueError:
                return self.toast("⚠ تعداد تکرار باید عدد باشد")

            body = {
                "title": title.value,
                "text": text.value,
                "channel_ids": sorted(st["sel"]),
                "mode": mode,
                "silent": st["silent"],
                "pin": st["pin"],
                "view_to_delete": view_int,
                "media_id": int(mediadd.value) if mediadd.value else None,
                "repeat": st["repeat"],
                "repeat_count": count_int,
                "scheduled_at": (
                    to_utc_iso(st["when"])
                    if mode == "schedule" and st["when"] else None
                ),
            }

            st["busy"] = True
            try:
                await self.api.post("/posts", body)
            except ApiError as e:
                st["busy"] = False
                return self.toast("⚠ " + str(e))
            st["busy"] = False

            self.toast({
                "draft": "✓ پیش‌نویس ذخیره شد",
                "now": "✓ پست در صف ارسال قرار گرفت",
                "schedule": "✓ پست زمان‌بندی شد",
            }[mode])
            await self.go("posts")

        def preview(e):
            self.page.show_dialog(ft.BottomSheet(content=ft.Container(
                content=ft.Column([
                    T("پیش‌نمایش", weight=ft.FontWeight.BOLD),
                    card(T(text.value or "متن پست اینجا نمایش داده می‌شود.")),
                ]),
                padding=16,
            )))

        def toggle(key):
            return lambda e: st.__setitem__(key, e.control.value)

        def chan(c):
            def f(e):
                (st["sel"].add if e.control.value else st["sel"].discard)(c["id"])
            return ft.Checkbox(label=c["title"], on_change=f)

        mediadd = dd(
            "پیوست از کتابخانه",
            [("", "بدون پیوست")] + [(m["id"], m["filename"]) for m in media],
            "",
        )

        # Channel selector
        channel_controls: list = [chan(c) for c in self.chs]
        if not channel_controls:
            channel_controls = [
                T("هنوز کانالی اضافه نکرده‌ای.",
                  size=12, color=ft.Colors.OUTLINE)
            ]

        # Publish options — hide the schedule-related pieces in local mode.
        publish_options: list = [
            ft.Switch(label="ارسال بی‌صدا", on_change=toggle("silent")),
            ft.Switch(label="پین بعد از ارسال", on_change=toggle("pin")),
            views,
        ]
        if not is_local:
            publish_options.append(
                ft.Row([
                    btn("انتخاب زمان", lambda e: self.spawn(pick(e))),
                    when_lbl,
                ])
            )
            publish_options.append(
                dd("تکرار",
                   [("none", "بدون تکرار"), ("daily", "هر روز"), ("weekly", "هر هفته")],
                   "none",
                   lambda e: st.__setitem__("repeat", e.control.value))
            )
            publish_options.append(count)

        fields: list = [
            header("پست جدید", "posts", self),
            title,
            dd("قالب", [(t["id"], t["name"]) for t in tpls], None, use_tpl),
            hscroll(*[
                btn(label, lambda e, a=action: self.spawn(ai(a)))
                for action, label in AI_ACTIONS
            ]),
            ft.Row([
                ft.Container(prompt, expand=True),
                btn("اجرا", lambda e: self.spawn(ai("create", prompt.value))),
            ]),
            text,
        ]
        if not is_local:
            fields.append(mediadd)
        fields.extend([
            T("کانال‌ها", weight=ft.FontWeight.BOLD),
            *channel_controls,
            T("گزینه‌های انتشار", weight=ft.FontWeight.BOLD),
            *publish_options,
        ])

        bar_controls: list = [
            btn("ذخیره پیش‌نویس", lambda e: self.spawn(send("draft")),
                disabled=not can(role, "edit")),
            btn("پیش‌نمایش", preview),
        ]
        if not is_local:
            bar_controls.append(
                btn("زمان‌بندی", lambda e: self.spawn(send("schedule")),
                    disabled=not can(role, "publish"))
            )
        bar_controls.append(
            btn("ارسال", lambda e: self.spawn(send("now")), True,
                disabled=not can(role, "publish"))
        )

        bar = ft.Row(bar_controls, scroll=ft.ScrollMode.AUTO)

        # No FAB on this view.
        self.page.floating_action_button = None

        return ft.Column([
            ft.Column(fields, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8),
            bar,
        ], expand=True)

    async def v_calendar(self):
        rows = await self.api.get("/posts")
        role = self.me["role"]
        by: dict = {}
        for p in rows:
            if p.get("scheduled_at"):
                by.setdefault(to_local(p["scheduled_at"]).date(), []).append(p)

        out: list = [
            header("تقویم"),
            T("برای تغییر روز یک پست زمان‌بندی‌شده، روی آن بزن.",
              size=12, color=ft.Colors.OUTLINE),
        ]
        for day in sorted(by):
            out.append(T(jdate(dt.datetime.combine(day, dt.time())),
                         weight=ft.FontWeight.BOLD))
            for p in by[day]:
                local_dt = to_local(p["scheduled_at"])
                time_str = digits(local_dt.strftime("%H:%M"))
                status_fa = ST.get(p["status"], p["status"])
                on_click = None
                if p["status"] == "scheduled" and can(role, "publish"):
                    on_click = lambda e, p=p: self.spawn(self.move(p))
                out.append(ft.ListTile(
                    title=T(p["title"] or p["text"][:30]),
                    subtitle=T(f"{time_str} · {status_fa}"),
                    on_click=on_click,
                ))
        if len(out) <= 2:
            out.append(T("برنامه انتشار تو خالی است.",
                         color=ft.Colors.OUTLINE))
        return ft.Column(out, scroll=ft.ScrollMode.AUTO)

    async def move(self, post: dict):
        d = await self.pick_date()
        if not d:
            return
        t = to_local(post["scheduled_at"])
        new = t.replace(year=d.year, month=d.month, day=d.day)
        try:
            await self.api.post(f'/posts/{post["id"]}/reschedule',
                                {"scheduled_at": to_utc_iso(new)})
            self.toast("✓ زمان انتشار تغییر کرد")
        except ApiError as e:
            self.toast("⚠ " + str(e))
        await self.go("calendar")

    async def v_media(self):
        # Local mode: this view should not be reachable, but guard anyway.
        if self.api.is_local:
            return ft.Column([
                header("کتابخانه", "more", self),
                card(
                    T("کتابخانه رسانه فقط با سرور در دسترس است.",
                      color=ft.Colors.OUTLINE),
                ),
            ], scroll=ft.ScrollMode.AUTO, spacing=8)

        items = await self.api.get("/media", params={"q": "", "category": ""})

        if not self._fp:
            self._fp = ft.FilePicker()
            try:
                self.page.services.append(self._fp)
            except Exception:
                try:
                    self.page.overlay.append(self._fp)
                except Exception:
                    pass
        fp = self._fp

        async def up(e):
            try:
                picked = await fp.pick_files()
            except Exception as ex:
                return self.toast("⚠ " + str(ex))
            if not picked:
                return
            for f in picked:
                try:
                    data = f.bytes or Path(f.path).read_bytes()
                except Exception as ex:
                    self.toast("⚠ " + str(ex))
                    continue
                await self.guard(self.api.upload(f.name, data))
            self.toast("✓ فایل آپلود شد")
            await self.go("media")

        async def tags(m):
            v = tf("برچسب‌ها (با ویرگول جدا کن)", value=",".join(m["tags"]))

            async def ok(e):
                self.page.pop_dialog()
                await self.guard(self.api.post(
                    f'/media/{m["id"]}/tags',
                    {"tags": v.value.split(",")},
                ))
                await self.go("media")

            self.page.show_dialog(ft.AlertDialog(
                content=v,
                actions=[ft.TextButton(
                    content="ذخیره",
                    on_click=lambda e: self.spawn(ok(e)),
                )],
            ))

        async def rm(m):
            if await self.confirm("فایل حذف شود؟"):
                await self.guard(self.api.delete(f'/media/{m["id"]}'))
                await self.go("media")

        CAT = {"images": "تصویر", "videos": "ویدیو",
               "files": "فایل", "other": "سایر"}

        cards: list = []
        for m in items:
            cards.append(card(
                T(m["filename"], weight=ft.FontWeight.BOLD),
                T(
                    f'{CAT.get(m["category"], m["category"])} · '
                    f'{digits(m["size"] // 1024)} کیلوبایت · '
                    f'{digits(m["used"])} بار استفاده',
                    size=12,
                ),
                T(" ".join("#" + t for t in m["tags"]), size=12),
                ft.Row([
                    btn("برچسب", lambda e, m=m: self.spawn(tags(m))),
                    btn("حذف", lambda e, m=m: self.spawn(rm(m))),
                ]),
            ))

        return ft.Column([
            header("کتابخانه", "more", self),
            btn("آپلود فایل", lambda e: self.spawn(up(e)), True),
        ] + (cards or [T("هنوز فایلی آپلود نکرده‌ای.",
                         color=ft.Colors.OUTLINE)]),
            scroll=ft.ScrollMode.AUTO, spacing=8)

    async def v_automation(self):
        if self.api.is_local:
            return ft.Column([
                header("اتوماسیون", "more", self),
                card(T("اتوماسیون فقط با سرور در دسترس است.",
                       color=ft.Colors.OUTLINE)),
            ], scroll=ft.ScrollMode.AUTO, spacing=8)

        autos = await self.api.get("/automations")
        logs = await self.api.get("/automations/logs")
        tpls = await self.api.get("/templates")

        f = {"weekdays": set(), "channels": set(), "approval": True}

        name = tf("نام")
        at = tf("ساعت (مثلاً 09:00)", value="09:00")
        interval = tf("هر چند دقیقه", value="60",
                      keyboard_type=ft.KeyboardType.NUMBER)
        body = tf("متن پست ({{title}} و {{date}})",
                  multiline=True, min_lines=3)

        at_row = ft.Row([at])
        interval_row = ft.Row([interval])
        at_row.visible = True
        interval_row.visible = False

        def on_trigger(e):
            t = e.control.value
            at_row.visible = t in ("daily", "weekly")
            interval_row.visible = t == "interval"
            self.page.update()

        trig = dd("محرک",
                  [("daily", "هر روز"), ("weekly", "هفتگی"),
                   ("interval", "هر چند دقیقه")],
                  "daily", on_trigger)

        tpl = dd("قالب",
                 [("", "متن دلخواه")] + [(t["id"], t["name"]) for t in tpls],
                 "")
        aia = dd("هوش مصنوعی",
                 [("", "بدون AI"), ("rewrite", "بازنویسی"),
                  ("engaging", "جذاب‌تر"), ("shorter", "کوتاه‌تر"),
                  ("proofread", "اصلاح نگارشی")],
                 "")

        def cb(s, k):
            return lambda e: (s.add if e.control.value else s.discard)(k)

        async def save(e):
            try:
                await self.api.post("/automations", {
                    "name": name.value,
                    "trigger": trig.value,
                    "at_time": at.value,
                    "weekdays": sorted(f["weekdays"]),
                    "interval_min": int(interval.value or 60),
                    "template_id": int(tpl.value) if tpl.value else None,
                    "body": body.value,
                    "use_ai": bool(aia.value),
                    "ai_action": aia.value or "",
                    "channel_ids": sorted(f["channels"]),
                    "require_approval": f["approval"],
                })
                self.toast("✓ اتوماسیون ذخیره شد")
                await self.go("automation")
            except (ApiError, ValueError) as ex:
                self.toast("⚠ " + (
                    str(ex) if isinstance(ex, ApiError)
                    else "مقادیر عددی معتبر نیست."
                ))

        items: list = []
        for a in autos:
            label = (
                "هر روز " if a["trigger"] == "daily"
                else "هفتگی " if a["trigger"] == "weekly"
                else "هر چند دقیقه "
            )
            value = a["at_time"] if a["trigger"] != "interval" else a["interval_min"]
            suffix = " · با تأیید" if a["require_approval"] else " · بدون تأیید"
            items.append(card(
                ft.Row([
                    T(a["name"], weight=ft.FontWeight.BOLD, expand=True),
                    T("فعال" if a["status"] == "active" else "متوقف", size=12),
                ]),
                T(label + digits(value) + suffix, size=12),
                ft.Row([
                    btn("توقف/فعال",
                        lambda e, i=a["id"]: self.spawn(self.act(
                            f"/automations/{i}/toggle", "automation"))),
                    btn("اجرا",
                        lambda e, i=a["id"]: self.spawn(self.act(
                            f"/automations/{i}/run", "automation",
                            "✓ اجرا شد"))),
                    btn("حذف",
                        lambda e, i=a["id"]: self.spawn(self.del_auto(i))),
                ], scroll=ft.ScrollMode.AUTO),
            ))

        form: list = [
            T("اتوماسیون جدید", size=16, weight=ft.FontWeight.BOLD),
            name, trig, at_row, interval_row,
            hscroll(*[
                ft.Checkbox(label=w, on_change=cb(f["weekdays"], i))
                for i, w in enumerate(WD)
            ]),
            tpl, body, aia,
            *[ft.Checkbox(label=c["title"],
                          on_change=cb(f["channels"], c["id"]))
              for c in self.chs],
            ft.Switch(
                label="نیاز به تأیید قبل از انتشار",
                value=True,
                on_change=lambda e: f.__setitem__("approval", e.control.value),
            ),
            btn("ذخیره اتوماسیون", lambda e: self.spawn(save(e)), True),
        ]

        log_rows: list = [
            T("گزارش اتوماسیون", size=16, weight=ft.FontWeight.BOLD)
        ] + [
            ft.ListTile(
                title=T(a["message"], size=13),
                subtitle=T(jfull_utc(a["at"]), size=11),
            )
            for a in logs[:10]
        ]

        return ft.Column([
            header("اتوماسیون", "more", self),
        ] + (items or [T("هنوز اتوماسیونی نساخته‌ای.",
                         color=ft.Colors.OUTLINE)])
          + form + log_rows,
            scroll=ft.ScrollMode.AUTO, spacing=8)

    async def del_auto(self, auto_id: int):
        if await self.confirm("حذف شود؟"):
            await self.guard(self.api.delete(f"/automations/{auto_id}"))
            await self.go("automation")

    async def v_analytics(self, days: int = 30):
        if self.api.is_local:
            return ft.Column([
                header("آمار", "more", self),
                card(T("آمار کامل فقط با سرور در دسترس است.",
                       color=ft.Colors.OUTLINE)),
            ], scroll=ft.ScrollMode.AUTO, spacing=8)

        d = await self.api.get(f"/analytics?days={days}")
        t = d["totals"]
        mx = max([1] + [x["published"] + x["failed"] for x in d["days"]])

        def kpi(label, value):
            return ft.Container(
                content=ft.Column([
                    T(digits(value), size=22, weight=ft.FontWeight.BOLD),
                    T(label, size=12),
                ], spacing=0),
                padding=10, border_radius=12,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                width=150,
            )

        bars = ft.Row(
            [
                ft.Container(
                    width=max(2, 300 // max(len(d["days"]), 1) - 2),
                    height=max(4, 100 * (x["published"] + x["failed"]) / mx),
                    bgcolor=(
                        ft.Colors.ERROR if x["failed"] else ft.Colors.GREEN
                    ),
                    tooltip=f'{x["date"]}: {x["published"]}/{x["failed"]}',
                )
                for x in d["days"]
            ],
            vertical_alignment=ft.CrossAxisAlignment.END,
            height=110, spacing=2,
        )

        per_channel = [
            card(
                T(c["title"], weight=ft.FontWeight.BOLD),
                T(
                    f'{digits(c["published"])} موفق · '
                    f'{digits(c["failed"])} ناموفق',
                    size=12,
                ),
            )
            for c in d["channels"]
        ]

        return ft.Column([
            header("آمار", "more", self),
            hscroll(*[
                (ft.FilledButton if days == n else ft.OutlinedButton)(
                    content=f"{digits(n)} روز",
                    on_click=lambda e, n=n: self.spawn(self.set_days(n)),
                )
                for n in (7, 30, 90)
            ]),
            ft.Row([
                kpi("ارسال موفق", t["published"]),
                kpi("ارسال ناموفق", t["failed"]),
                kpi("نرخ خطا", f'{round(t["failure_rate"] * 100)}٪'),
                kpi("زمان‌بندی‌شده", t["scheduled"]),
                kpi("اندازه صف", t["queue"]),
            ], wrap=True, spacing=8, run_spacing=8),
            T("انتشار روزانه", weight=ft.FontWeight.BOLD),
            bars,
            T("به تفکیک کانال", weight=ft.FontWeight.BOLD),
        ] + (per_channel or [T("هنوز ارسالی ثبت نشده است.")]) + [
            T("ماهانه", weight=ft.FontWeight.BOLD),
        ] + [
            T(f'{m["month"]}: {digits(m["published"])} پست')
            for m in d["monthly"]
        ] + [
            T(
                "این آمار فقط از عملیات خود برنامه به دست می‌آید؛ "
                "تعداد اعضا و بازدید کانال در API عمومی ایتایار در دسترس نیست.",
                size=12, color=ft.Colors.OUTLINE,
            ),
        ], scroll=ft.ScrollMode.AUTO, spacing=8)

    async def set_days(self, n: int):
        self.body.content = await self.v_analytics(n)
        self.page.update()

    async def v_channels(self):
        v = tf("لینک یا شناسه کانال جدید")

        async def add(e):
            conns = await self.guard(self.api.get("/connections"))
            if not conns:
                return self.toast("⚠ ابتدا اتصال ایتایار برقرار کن.")
            if await self.guard(self.api.post("/channels", {
                "connection_id": conns[0]["id"],
                "value": v.value,
            })) is not None:
                self.chs = await self.api.get("/channels")
                await self.go("channels")

        rows: list = []
        for c in self.chs:
            rows.append(card(
                T(c["title"], weight=ft.FontWeight.BOLD),
                T(c["chat_id"], size=12, color=ft.Colors.OUTLINE),
            ))

        return ft.Column([
            header("کانال‌ها", "more", self),
        ] + (rows or [T("هنوز کانالی اضافه نشده است.",
                        color=ft.Colors.OUTLINE)]) + [
            v,
            btn("افزودن", lambda e: self.spawn(add(e)), True),
            T(
                "ویرایش و حذف پیام‌های قبلی، تاریخچه کانال و آمار رسمی: "
                "این عملیات در API عمومی فعلی ایتایار در دسترس نیست.",
                size=12, color=ft.Colors.OUTLINE,
            ),
        ], scroll=ft.ScrollMode.AUTO, spacing=8)

    async def v_templates(self):
        tp = await self.api.get("/templates")
        n = tf("نام قالب")
        b = tf("متن ({{title}} {{summary}} {{link}} {{date}} {{hashtags}})",
               multiline=True, min_lines=4)

        async def add(e):
            await self.guard(self.api.post(
                "/templates",
                {"name": n.value, "body": b.value},
            ))
            await self.go("templates")

        async def rm(tid):
            if await self.confirm("قالب حذف شود؟"):
                await self.guard(self.api.delete(f"/templates/{tid}"))
                await self.go("templates")

        rows: list = []
        for t in tp:
            row: list = [T(t["name"], weight=ft.FontWeight.BOLD, expand=True)]
            if not t.get("builtin"):
                row.append(btn("حذف",
                               lambda e, i=t["id"]: self.spawn(rm(i))))
            rows.append(card(
                ft.Row(row),
                T(t["body"], size=12, color=ft.Colors.OUTLINE),
            ))

        return ft.Column([
            header("قالب‌ها", "more", self),
        ] + rows + [
            T("قالب اختصاصی", weight=ft.FontWeight.BOLD),
            n, b,
            btn("ذخیره قالب", lambda e: self.spawn(add(e)), True),
        ], scroll=ft.ScrollMode.AUTO, spacing=8)

    async def v_settings(self):
        me = self.me
        is_local = self.api.is_local

        out: list = [
            header("تنظیمات", "more", self),
            card(ft.Row([
                ft.Container(
                    T((me["name"] or "؟")[:1], size=18, color=ft.Colors.WHITE),
                    width=44, height=44, border_radius=22,
                    alignment=ft.Alignment(0, 0),
                    bgcolor=ft.Colors.PRIMARY,
                ),
                ft.Column([
                    T(me["name"], weight=ft.FontWeight.BOLD),
                    T(ROLE_FA.get(me["role"], me["role"]),
                      size=12, color=ft.Colors.OUTLINE),
                ], spacing=0),
            ], spacing=12)),
            dd("ظاهر برنامه",
               [("system", "مطابق دستگاه"),
                ("light", "روشن"), ("dark", "تیره")],
               self.theme_pref, self.set_theme),
        ]

        if is_local:
            # Local mode info card.
            out.append(card(
                T("حالت اجرا: محلی", weight=ft.FontWeight.BOLD),
                T(
                    "این نسخه بدون سرور کار می‌کند. هوش مصنوعی و ارسال "
                    "مستقیم از روی گوشی انجام می‌شوند. زمان‌بندی، "
                    "اتوماسیون، کتابخانه رسانه و مدیریت تیم در این حالت "
                    "در دسترس نیستند.",
                    size=12, color=ft.Colors.OUTLINE,
                ),
            ))
        else:
            out.append(btn("تنظیمات اتصال",
                           lambda e: self.conn_dialog()))

        if not is_local:
            out.append(btn("خروج از حساب",
                           lambda e: self.spawn(self.logout(ask=True))))

        out.append(T(f"نسخه {digits(config.VERSION)}",
                     size=11, color=ft.Colors.OUTLINE))

        # --- team management (server mode, owner only) --------------------
        if (not is_local) and me["role"] == "owner":
            team = await self.api.get("/team")
            f = {"role": "editor"}
            u = tf("نام کاربری")
            p = tf("رمز عبور (حداقل ۸ نویسه)", password=True)
            nm = tf("نام نمایشی")
            eid = tf("شناسه ایتا (اختیاری)")

            async def add_member(e):
                r = await self.guard(self.api.post("/team", {
                    "username": u.value,
                    "password": p.value,
                    "display_name": nm.value,
                    "role": f["role"],
                    "eitaa_id": eid.value or None,
                }))
                if r is not None:
                    self.toast("✓ عضو اضافه شد")
                    await self.go("settings")

            async def rm_member(uid):
                if await self.confirm("عضو حذف شود؟"):
                    await self.guard(self.api.delete(f"/team/{uid}"))
                    await self.go("settings")

            member_rows: list = []
            for m in team:
                row: list = [
                    T(f'{m["name"]} — {ROLE_FA.get(m["role"], m["role"])}',
                      expand=True)
                ]
                if m["id"] != me["id"]:
                    row.append(btn("حذف",
                                   lambda e, i=m["id"]: self.spawn(rm_member(i))))
                member_rows.append(card(ft.Row(row)))

            out.append(T("اعضای تیم", size=16, weight=ft.FontWeight.BOLD))
            out.extend(member_rows)
            out.extend([
                u, nm, p,
                dd("نقش",
                   [(k, v) for k, v in ROLE_FA.items() if k != "owner"],
                   "editor",
                   lambda e: f.__setitem__("role", e.control.value)),
                eid,
                btn("افزودن عضو",
                    lambda e: self.spawn(add_member(e)), True),
                T("توکن ایتایار هرگز به اعضای تیم نمایش داده نمی‌شود.",
                  size=12, color=ft.Colors.OUTLINE),
            ])

        return ft.Column(out, scroll=ft.ScrollMode.AUTO, spacing=8)

    def set_theme(self, e):
        self.apply_theme(e.control.value)
        self.spawn(self.store.set("theme", e.control.value))
        self.page.update()


# ==================================================================== main ==

async def main(page: ft.Page):
    loop = asyncio.get_running_loop()

    page.title = config.APP_NAME
    page.rtl = True
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.CYAN)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.CYAN)
    page.padding = 0

    # Desktop window icon; Android uses the launcher icon.
    try:
        page.window.icon = "assets/icon.png"
    except Exception:
        pass

    app = App(page, loop)
    page.add(ft.SafeArea(content=app.body, expand=True))
    await app.boot()


if __name__ == "__main__":
    ft.run(main)
