import asyncio, datetime as dt, os
import flet as ft
from api import Api, ApiError
from fa import digits, jdate, jfull_utc, to_local, to_utc_iso

PERMS = {"owner": ["*"], "editor": ["edit"], "approver": ["approve"], "publisher": ["publish"], "analyst": ["analytics"]}
can = lambda role, p: any(x in ("*", p) for x in PERMS.get(role, []))
ROLE_FA = {"owner": "مالک", "editor": "ویرایشگر", "approver": "تأییدکننده", "publisher": "منتشرکننده", "analyst": "تحلیلگر"}
ST = {"draft": "پیش‌نویس", "pending": "در انتظار تأیید", "scheduled": "زمان‌بندی‌شده", "queued": "در صف", "sending": "در حال ارسال", "published": "ارسال‌شده", "failed": "ناموفق", "cancelled": "لغوشده"}
AI_ACTIONS = [("create", "✨ ساخت پست"), ("rewrite", "✏ بازنویسی"), ("shorter", "↔ کوتاه‌تر"), ("longer", "↕ کامل‌تر"), ("formal", "رسمی"), ("casual", "خودمانی"), ("news", "خبری"),
              ("educational", "آموزشی"), ("engaging", "جذاب‌تر"), ("titles", "عنوان"), ("hashtags", "هشتگ"), ("translate", "ترجمه"), ("summarize", "خلاصه"), ("proofread", "اصلاح نگارشی")]
WD = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]

# ---------- small widgets ----------
T = lambda s, **k: ft.Text(str(s), **k)
def tf(label, **k): return ft.TextField(label=label, **k)
def btn(text, fn, primary=False, **k): return (ft.FilledButton if primary else ft.OutlinedButton)(content=text, on_click=fn, **k)
def card(*c, **k): return ft.Container(content=ft.Column(list(c), spacing=6), padding=12, border_radius=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), **k)
def dd(label, opts, value=None, on_select=None): return ft.Dropdown(label=label, value=value, options=[ft.DropdownOption(key=str(k), text=t) for k, t in opts], on_select=on_select)
def hscroll(*c): return ft.Row(list(c), scroll=ft.ScrollMode.AUTO, spacing=6)
def header(title, back=None, app=None):
    r = [T(title, size=20, weight=ft.FontWeight.BOLD, expand=True)]
    if back: r.insert(0, ft.IconButton(icon=ft.Icons.ARROW_FORWARD, on_click=lambda e: app.spawn(app.go(back))))
    return ft.Row(r)

class Store:  # session token + server URL only (never the EitaaYar token)
    def __init__(self): self.p = ft.SharedPreferences()
    async def get(self, k): return await self.p.get(k)
    async def set(self, k, v): await self.p.set(k, v)
    async def remove(self, k): await self.p.remove(k)

class App:
    def __init__(self, page, store=None):
        self.page, self.store, self.api = page, store or Store(), Api()
        self.me, self.chs, self.view, self.dark = None, [], "home", False
        self.body = ft.Container(expand=True, padding=ft.Padding.all(12))
        self.loop = asyncio.get_event_loop(); self.fp = None
        self.api.on_401 = lambda: self.spawn(self.logout())

    def spawn(self, coro):  # safe from any thread (Flet may run sync handlers in a worker thread)
        return asyncio.run_coroutine_threadsafe(coro, self.loop)
    def toast(self, m): self.page.show_dialog(ft.SnackBar(content=T(m)))
    async def guard(self, coro):
        try: return await coro
        except ApiError as e: self.toast("⚠ " + str(e))
    async def confirm(self, msg):
        fut = asyncio.get_event_loop().create_future()
        def res(v):
            def h(e): self.page.pop_dialog(); fut.done() or fut.set_result(v)
            return h
        self.page.show_dialog(ft.AlertDialog(content=T(msg), actions=[ft.TextButton(content="بله", on_click=res(True)), ft.TextButton(content="خیر", on_click=res(False))]))
        return await fut
    async def pick_date(self):
        fut = asyncio.get_event_loop().create_future(); now = dt.datetime.now()
        self.page.show_dialog(ft.DatePicker(first_date=now - dt.timedelta(days=1), last_date=now + dt.timedelta(days=730),
            on_change=lambda e: fut.done() or fut.set_result(e.control.value), on_dismiss=lambda e: fut.done() or fut.set_result(None)))
        return await fut
    async def pick_time(self):
        fut = asyncio.get_event_loop().create_future()
        self.page.show_dialog(ft.TimePicker(on_change=lambda e: fut.done() or fut.set_result(e.control.value), on_dismiss=lambda e: fut.done() or fut.set_result(None)))
        return await fut
    async def open_url(self, url): await self.page.launch_url(url)

    # ---------- boot / auth ----------
    async def boot(self):
        self.api.base = await self.store.get("server") or ""; self.api.token = await self.store.get("token")
        if self.api.base and self.api.token:
            try: self.me = await self.api.get("/auth/me"); return await self.enter()
            except ApiError: pass
        await self.show_auth()
    async def logout(self):
        await self.store.remove("token"); self.api.token = self.me = None; self.page.navigation_bar = None; self.page.floating_action_button = None
        await self.show_auth()
    async def signed_in(self, r):
        self.api.token, self.me = r["token"], r["user"]; await self.store.set("token", r["token"]); await self.store.set("server", self.api.base); await self.enter()
    async def enter(self):
        self.chs = await self.api.get("/channels")
        if not self.chs and self.me["role"] == "owner": return await self.show_onboarding()
        if not self.chs: self.body.content = card(T("هنوز کانالی متصل نشده است. از مالک بخواهید کانالی اضافه کند."), btn("خروج", lambda e: self.spawn(self.logout()))); return self.page.update()
        self.page.navigation_bar = ft.NavigationBar(selected_index=0, on_change=lambda e: self.spawn(self.nav(int(e.control.selected_index))), destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.HOME, label="خانه"), ft.NavigationBarDestination(icon=ft.Icons.EDIT_NOTE, label="پست‌ها"),
            ft.NavigationBarDestination(icon=ft.Icons.CALENDAR_MONTH, label="تقویم"), ft.NavigationBarDestination(icon=ft.Icons.MORE_HORIZ, label="بیشتر")])
        if can(self.me["role"], "edit") or can(self.me["role"], "publish"):
            self.page.floating_action_button = ft.FloatingActionButton(icon=ft.Icons.ADD, on_click=lambda e: self.spawn(self.go("new")))
        await self.go("home")
    async def nav(self, i): await self.go(["home", "posts", "calendar", "more"][i])
    async def go(self, name):
        self.view = name; self.body.content = ft.ProgressRing(); self.page.update()
        try: self.body.content = await getattr(self, "v_" + name)()
        except ApiError as e: self.body.content = card(T("⚠ " + str(e)), btn("تلاش دوباره", lambda e: self.spawn(self.go(name))))
        self.page.update()

    async def show_auth(self):
        has = {"has_users": True}
        server = tf("آدرس سرور (https://…)", value=self.api.base or await self.store.get("server") or "", keyboard_type=ft.KeyboardType.URL)
        u, p, n = tf("نام کاربری"), tf("رمز عبور", password=True, can_reveal_password=True), tf("نام نمایشی")
        title, msg = T("ورود", size=22, weight=ft.FontWeight.BOLD), T("", color=ft.Colors.ERROR)
        async def check(e=None):
            self.api.base = server.value.strip()
            try: has.update(await self.api.get("/auth/state")); msg.value = ""
            except ApiError as ex: msg.value = "⚠ " + str(ex)
            n.visible = not has["has_users"]; title.value = "ساخت حساب مالک" if not has["has_users"] else "ورود"; self.page.update()
        async def go_(e):
            self.api.base = server.value.strip()
            try:
                r = await self.api.post("/auth/login" if has["has_users"] else "/auth/register", {"username": u.value, "password": p.value, "display_name": n.value})
                await self.signed_in(r)
            except ApiError as ex: msg.value = "⚠ " + str(ex); self.page.update()
        server.on_blur = lambda e: self.spawn(check()); n.visible = False
        self.body.content = ft.Column([title, server, u, p, n, btn("ورود / ساخت حساب", lambda e: self.spawn(go_(e)), True), msg], spacing=10, scroll=ft.ScrollMode.AUTO)
        self.page.update()
        if server.value: await check()

    # ---------- onboarding (6 steps) ----------
    async def show_onboarding(self):
        s = {"step": 0, "conn": None}; msg = T("", color=ft.Colors.ERROR)
        token = tf("توکن ایتایار", password=True, can_reveal_password=True); chan = tf("لینک، نام کاربری یا شناسه کانال")
        def render():
            st = s["step"]; c = []
            if st == 0: c = [T("مدیریت کانالت، ساده‌تر از همیشه", size=22, weight=ft.FontWeight.BOLD), T("ساخت محتوا، زمان‌بندی و انتشار چند کانال از یکجا"), btn("شروع راه‌اندازی", lambda e: nxt(1), True)]
            if st == 1: c = [T("چند کانال می‌خواهی مدیریت کنی؟", size=20)] + [btn(l, lambda e: nxt(2)) for l in ["فقط ۱ کانال", "۲ تا ۵ کانال", "بیشتر از ۵ کانال"]]
            if st == 2: c = [T("برای اینکه ایتایار بتواند برای کانالت ارسال انجام دهد، ابتدا @sender را به عنوان مدیر کانال اضافه کن."), btn("باز کردن @sender در ایتا", lambda e: self.spawn(self.open_url("https://eitaa.com/sender"))), btn("انجام شد", lambda e: nxt(3), True)]
            if st == 3: c = [T("حالا کانالت را در ایتایار اضافه کن."), btn("باز کردن ایتایار", lambda e: self.spawn(self.open_url("https://eitaayar.ir"))), token,
                             T("✓ اتصال برقرار شد — " + s["conn"]["bot"], color=ft.Colors.GREEN) if s["conn"] else btn("بررسی اتصال", lambda e: self.spawn(connect()), True)]
            if s["conn"] and st == 3: c.append(btn("ادامه", lambda e: nxt(4), True))
            if st == 4: c = [T("افزودن کانال", size=20), T("اگر شناسه کانال را از ایتایار داری، می‌توانی آن را مستقیم وارد کنی."), chan, btn("اتصال کانال", lambda e: self.spawn(add()), True)]
            if st == 5: c = [T("✓ کانال با موفقیت متصل شد", size=20, color=ft.Colors.GREEN), btn("افزودن کانال دیگر", lambda e: nxt(4)), btn("ورود به داشبورد", lambda e: self.spawn(self.enter()), True)]
            self.body.content = ft.Column(c + [msg], spacing=10, scroll=ft.ScrollMode.AUTO); self.page.update()
        def nxt(i): s["step"] = i; msg.value = ""; render()
        async def connect():
            try: s["conn"] = await self.api.post("/connections", {"token": token.value.strip()}); token.value = ""; msg.value = ""
            except ApiError as e: msg.value = "⚠ " + str(e)
            render()
        async def add():
            try: await self.api.post("/channels", {"connection_id": s["conn"]["id"], "value": chan.value}); chan.value = ""; nxt(5)
            except ApiError as e: msg.value = "⚠ " + str(e); render()
        if not s["conn"]:
            cs = await self.api.get("/connections")
            if cs: s["conn"] = cs[0]
        render()

    # ---------- views ----------
    async def v_more(self):
        items = [("media", "کتابخانه", ft.Icons.FOLDER), ("automation", "اتوماسیون", ft.Icons.BOLT), ("analytics", "آمار", ft.Icons.BAR_CHART), ("channels", "کانال‌ها", ft.Icons.CAMPAIGN),
                 ("templates", "قالب‌ها", ft.Icons.ARTICLE), ("settings", "تنظیمات", ft.Icons.SETTINGS)]
        if not can(self.me["role"], "analytics"): items = [i for i in items if i[0] != "analytics"]
        return ft.Column([header("بیشتر")] + [ft.ListTile(leading=ft.Icon(i), title=T(t), on_click=lambda e, k=k: self.spawn(self.go(k))) for k, t, i in items])

    async def v_home(self):
        s, act = await self.api.get("/stats"), await self.api.get("/activity")
        k = lambda l, v: ft.Container(content=ft.Column([T(digits(v), size=24, weight=ft.FontWeight.BOLD), T(l, size=12)], spacing=0), padding=12, border_radius=12, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, width=150)
        rows = [ft.ListTile(title=T(a["message"], size=13), subtitle=T(jfull_utc(a["at"]), size=11)) for a in act[:8]] or [T("هنوز فعالیتی ثبت نشده است.", color=ft.Colors.OUTLINE)]
        return ft.Column([header("امروز چه چیزی منتشر کنیم؟"), ft.Row([k("ارسال‌شده", s["published"]), k("زمان‌بندی‌شده", s["scheduled"]), k("در صف ارسال", s["queued"]), k("خطاها", s["failed"]), k("استفاده از AI", s["ai_calls"])], wrap=True, spacing=8, run_spacing=8),
                          T("فعالیت اخیر", size=16, weight=ft.FontWeight.BOLD)] + rows, scroll=ft.ScrollMode.AUTO, spacing=10)

    async def v_posts(self, flt="all", q=""):
        rows, role = await self.api.get("/posts"), self.me["role"]; names = {c["id"]: c["title"] for c in self.chs}
        search = tf("جستجو در عنوان و متن", value=q, on_submit=lambda e: self.spawn(self.reload_posts(flt, e.control.value)))
        tabs = hscroll(*[(ft.FilledButton if flt == k else ft.OutlinedButton)(content=("همه" if k == "all" else ST[k]), on_click=lambda e, k=k: self.spawn(self.reload_posts(k, q))) for k in ["all", "draft", "pending", "scheduled", "queued", "published", "failed"]])
        out = []
        for p in [r for r in rows if (flt == "all" or r["status"] == flt) and q in (r["title"] + r["text"])]:
            c = [ft.Row([T(p["title"] or p["text"][:40], weight=ft.FontWeight.BOLD, expand=True), T(ST.get(p["status"], p["status"]), size=12)])]
            if p["scheduled_at"]: c.append(T(jfull_utc(p["scheduled_at"]), size=12, color=ft.Colors.OUTLINE))
            for d in p["deliveries"]:
                line = [T(f'{names.get(d["channel_id"], "")}: ' + ("✓ موفق" if d["status"] == "published" else "✕ ناموفق" if d["status"] == "failed" else ST.get(d["status"], d["status"])), expand=True)]
                if d["status"] == "failed" and can(role, "publish"): line.append(btn("تلاش دوباره", lambda e, i=d["id"]: self.spawn(self.act(f"/deliveries/{i}/retry", "posts", "✓ در صف تلاش دوباره قرار گرفت"))))
                c.append(ft.Row(line))
                if d["error"]: c.append(T(d["error"], size=11, color=ft.Colors.ERROR))
            if p["status"] == "pending" and can(role, "approve"):
                c.append(ft.Row([btn("تأیید", lambda e, i=p["id"]: self.spawn(self.act(f"/posts/{i}/approve", "posts", "✓ تأیید شد")), True), btn("رد", lambda e, i=p["id"]: self.spawn(self.act(f"/posts/{i}/reject", "posts")))]))
            if p["status"] in ("scheduled", "queued") and can(role, "publish"): c.append(btn("لغو", lambda e, i=p["id"]: self.spawn(self.cancel(i))))
            out.append(card(*c))
        return ft.Column([header("پست‌ها"), tabs, search] + (out or [T("هنوز پستی در این بخش نیست.", color=ft.Colors.OUTLINE)]), scroll=ft.ScrollMode.AUTO, spacing=8)
    async def reload_posts(self, flt, q): self.body.content = await self.v_posts(flt, q); self.page.update()
    async def act(self, path, view, ok=None):
        await self.guard(self.api.post(path)); ok and self.toast(ok); await self.go(view)
    async def cancel(self, i):
        if await self.confirm("این پست لغو شود؟"): await self.act(f"/posts/{i}/cancel", "posts")

    async def v_new(self):
        role = self.me["role"]; tpls, media = await self.api.get("/templates"), await self.api.get("/media")
        st = {"sel": set(), "when": None, "silent": False, "pin": False, "repeat": "none", "busy": False}
        title, text = tf("عنوان داخلی (نمایش داده نمی‌شود)"), tf("متن پست…", multiline=True, min_lines=8)
        prompt, views, count = tf("از این اطلاعات یک پست مناسب برای کانال بساز…"), tf("حذف پس از … بازدید (اختیاری)", keyboard_type=ft.KeyboardType.NUMBER), tf("تعداد تکرار", value="4", keyboard_type=ft.KeyboardType.NUMBER)
        when_lbl = T("زمان انتشار انتخاب نشده")
        def use_tpl(e):
            t = next((x for x in tpls if str(x["id"]) == e.control.value), None)
            if t: text.value = t["body"].replace("{{title}}", title.value).replace("{{date}}", jdate(dt.datetime.now())); self.page.update()
        async def ai(action, pr=""):
            try:
                r = await self.api.post("/ai", {"action": action, "text": text.value, "prompt": pr})
                if action == "titles": self.page.show_dialog(ft.AlertDialog(content=T(r["text"]), actions=[ft.TextButton(content="بستن", on_click=lambda e: self.page.pop_dialog())]))
                else: text.value = r["text"]; self.toast("✓ نتیجه آمد؛ بررسی و ویرایش کن"); self.page.update()
            except ApiError as e: self.toast("⚠ " + str(e))
        async def pick(e):
            d = await self.pick_date(); t = d and await self.pick_time()
            if d and t: st["when"] = dt.datetime.combine(d.date(), t); when_lbl.value = jdate(st["when"]) + digits(f" — {st['when']:%H:%M}"); self.page.update()
        async def send(mode):
            body = {"title": title.value, "text": text.value, "channel_ids": sorted(st["sel"]), "mode": mode, "silent": st["silent"], "pin": st["pin"], "view_to_delete": int(views.value) if views.value.isdigit() else None,
                    "media_id": int(mediadd.value) if mediadd.value else None, "repeat": st["repeat"], "repeat_count": int(count.value or 1) if count.value.isdigit() else 1,
                    "scheduled_at": to_utc_iso(st["when"]) if mode == "schedule" and st["when"] else None}
            try: await self.api.post("/posts", body)
            except ApiError as e: return self.toast("⚠ " + str(e))
            self.toast({"draft": "✓ پیش‌نویس ذخیره شد", "now": "✓ پست در صف ارسال قرار گرفت", "schedule": "✓ پست زمان‌بندی شد"}[mode]); await self.go("posts")
        def preview(e): self.page.show_dialog(ft.BottomSheet(content=ft.Container(content=ft.Column([T("پیش‌نمایش", weight=ft.FontWeight.BOLD), card(T(text.value or "متن پست اینجا نمایش داده می‌شود."))]), padding=16)))
        def toggle(key): return lambda e: st.__setitem__(key, e.control.value)
        def chan(c):
            def f(e): (st["sel"].add if e.control.value else st["sel"].discard)(c["id"])
            return ft.Checkbox(label=c["title"], on_change=f)
        mediadd = dd("پیوست از کتابخانه", [("", "بدون پیوست")] + [(m["id"], m["filename"]) for m in media], "")
        fields = [header("پست جدید", "posts", self), title, dd("قالب", [(t["id"], t["name"]) for t in tpls], None, use_tpl),
                  hscroll(*[btn(l, lambda e, a=a: self.spawn(ai(a))) for a, l in AI_ACTIONS]), ft.Row([ft.Container(prompt, expand=True), btn("اجرا", lambda e: self.spawn(ai("create", prompt.value)))]),
                  text, mediadd, T("کانال‌ها", weight=ft.FontWeight.BOLD), *[chan(c) for c in self.chs], T("گزینه‌های انتشار", weight=ft.FontWeight.BOLD),
                  ft.Switch(label="ارسال بی‌صدا", on_change=toggle("silent")), ft.Switch(label="پین بعد از ارسال", on_change=toggle("pin")), views,
                  ft.Row([btn("انتخاب زمان", lambda e: self.spawn(pick(e))), when_lbl]),
                  dd("تکرار", [("none", "بدون تکرار"), ("daily", "هر روز"), ("weekly", "هر هفته")], "none", lambda e: st.__setitem__("repeat", e.control.value)), count]
        bar = ft.Row([btn("ذخیره پیش‌نویس", lambda e: self.spawn(send("draft")), disabled=not can(role, "edit")), btn("پیش‌نمایش", preview),
                      btn("زمان‌بندی", lambda e: self.spawn(send("schedule")), disabled=not can(role, "publish")), btn("ارسال", lambda e: self.spawn(send("now")), True, disabled=not can(role, "publish"))], scroll=ft.ScrollMode.AUTO)
        self.page.floating_action_button = None
        return ft.Column([ft.Column(fields, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8), bar], expand=True)

    async def v_calendar(self):  # agenda view: phones have no room for a month grid / drag & drop
        rows, role = await self.api.get("/posts"), self.me["role"]; by = {}
        for p in rows:
            if p["scheduled_at"]: by.setdefault(to_local(p["scheduled_at"]).date(), []).append(p)
        out = [header("تقویم"), T("برای تغییر روز یک پست زمان‌بندی‌شده، روی آن بزن.", size=12, color=ft.Colors.OUTLINE)]
        for day in sorted(by):
            out.append(T(jdate(dt.datetime.combine(day, dt.time())), weight=ft.FontWeight.BOLD))
            for p in by[day]:
                out.append(ft.ListTile(title=T(p["title"] or p["text"][:30]), subtitle=T(f'{digits(f"{to_local(p["scheduled_at"]):%H:%M}")} · {ST[p["status"]]}'),
                    on_click=(lambda e, p=p: self.spawn(self.move(p))) if p["status"] == "scheduled" and can(role, "publish") else None))
        return ft.Column(out if len(out) > 2 else out + [T("برنامه انتشار تو خالی است.", color=ft.Colors.OUTLINE)], scroll=ft.ScrollMode.AUTO)
    async def move(self, p):
        d = await self.pick_date()
        if not d: return
        t = to_local(p["scheduled_at"]); new = t.replace(year=d.year, month=d.month, day=d.day)
        try: await self.api.post(f'/posts/{p["id"]}/reschedule', {"scheduled_at": to_utc_iso(new)}); self.toast("✓ زمان انتشار تغییر کرد")
        except ApiError as e: self.toast("⚠ " + str(e))
        await self.go("calendar")

    async def v_media(self):
        items = await self.api.get("/media?q=&category=")
        if not self.fp: self.fp = ft.FilePicker(); self.page.services.append(self.fp)
        fp = self.fp
        async def up(e):
            for f in await fp.pick_files():
                data = f.bytes or open(f.path, "rb").read()
                await self.guard(self.api.upload(f.name, data))
            self.toast("✓ فایل آپلود شد"); await self.go("media")
        async def tags(m):
            v = tf("برچسب‌ها (با ویرگول جدا کن)", value=",".join(m["tags"]))
            async def ok(e): self.page.pop_dialog(); await self.guard(self.api.post(f'/media/{m["id"]}/tags', {"tags": v.value.split(",")})); await self.go("media")
            self.page.show_dialog(ft.AlertDialog(content=v, actions=[ft.TextButton(content="ذخیره", on_click=lambda e: self.spawn(ok(e)))]))
        async def rm(m):
            if await self.confirm("فایل حذف شود؟"): await self.guard(self.api.delete(f'/media/{m["id"]}')); await self.go("media")
        CAT = {"images": "تصویر", "videos": "ویدیو", "files": "فایل", "other": "سایر"}
        cards = [card(T(m["filename"], weight=ft.FontWeight.BOLD), T(f'{CAT[m["category"]]} · {digits(m["size"] // 1024)} کیلوبایت · {digits(m["used"])} بار استفاده', size=12), T(" ".join("#" + t for t in m["tags"]), size=12),
                      ft.Row([btn("برچسب", lambda e, m=m: self.spawn(tags(m))), btn("حذف", lambda e, m=m: self.spawn(rm(m)))])) for m in items]
        return ft.Column([header("کتابخانه", "more", self), btn("آپلود فایل", lambda e: self.spawn(up(e)), True)] + (cards or [T("هنوز فایلی آپلود نکرده‌ای.", color=ft.Colors.OUTLINE)]), scroll=ft.ScrollMode.AUTO, spacing=8)

    async def v_automation(self):
        autos, logs, tpls = await self.api.get("/automations"), await self.api.get("/automations/logs"), await self.api.get("/templates")
        f = {"weekdays": set(), "channels": set(), "approval": True}
        name, at, interval, body = tf("نام"), tf("ساعت (مثلاً 09:00)", value="09:00"), tf("هر چند دقیقه", value="60", keyboard_type=ft.KeyboardType.NUMBER), tf("متن پست ({{title}} و {{date}})", multiline=True, min_lines=3)
        trig = dd("محرک", [("daily", "هر روز"), ("weekly", "هفتگی"), ("interval", "هر چند دقیقه")], "daily")
        tpl = dd("قالب", [("", "متن دلخواه")] + [(t["id"], t["name"]) for t in tpls], "")
        aia = dd("هوش مصنوعی", [("", "بدون AI"), ("rewrite", "بازنویسی"), ("engaging", "جذاب‌تر"), ("shorter", "کوتاه‌تر"), ("proofread", "اصلاح نگارشی")], "")
        def cb(s, k): return lambda e: (s.add if e.control.value else s.discard)(k)
        async def save(e):
            try: await self.api.post("/automations", {"name": name.value, "trigger": trig.value, "at_time": at.value, "weekdays": sorted(f["weekdays"]), "interval_min": int(interval.value or 60), "template_id": int(tpl.value) if tpl.value else None,
                                                     "body": body.value, "use_ai": bool(aia.value), "ai_action": aia.value or "", "channel_ids": sorted(f["channels"]), "require_approval": f["approval"]}); self.toast("✓ اتوماسیون ذخیره شد"); await self.go("automation")
            except (ApiError, ValueError) as ex: self.toast("⚠ " + (str(ex) if isinstance(ex, ApiError) else "مقادیر عددی معتبر نیست."))
        items = [card(ft.Row([T(a["name"], weight=ft.FontWeight.BOLD, expand=True), T("فعال" if a["status"] == "active" else "متوقف", size=12)]), T(("هر روز " if a["trigger"] == "daily" else "هفتگی " if a["trigger"] == "weekly" else "هر چند دقیقه ") + digits(a["at_time"] if a["trigger"] != "interval" else a["interval_min"]) + (" · با تأیید" if a["require_approval"] else " · بدون تأیید"), size=12),
                      ft.Row([btn("توقف/فعال", lambda e, i=a["id"]: self.spawn(self.act(f"/automations/{i}/toggle", "automation"))), btn("اجرا", lambda e, i=a["id"]: self.spawn(self.act(f"/automations/{i}/run", "automation", "✓ اجرا شد"))),
                              btn("حذف", lambda e, i=a["id"]: self.spawn(self.del_auto(i)))], scroll=ft.ScrollMode.AUTO)) for a in autos]
        form = [T("اتوماسیون جدید", size=16, weight=ft.FontWeight.BOLD), name, trig, at, interval, hscroll(*[ft.Checkbox(label=w, on_change=cb(f["weekdays"], i)) for i, w in enumerate(WD)]), tpl, body, aia,
                *[ft.Checkbox(label=c["title"], on_change=cb(f["channels"], c["id"])) for c in self.chs], ft.Switch(label="نیاز به تأیید قبل از انتشار", value=True, on_change=lambda e: f.__setitem__("approval", e.control.value)), btn("ذخیره اتوماسیون", lambda e: self.spawn(save(e)), True)]
        lg = [T("گزارش اتوماسیون", size=16, weight=ft.FontWeight.BOLD)] + [ft.ListTile(title=T(a["message"], size=13), subtitle=T(jfull_utc(a["at"]), size=11)) for a in logs[:10]]
        return ft.Column([header("اتوماسیون", "more", self)] + (items or [T("هنوز اتوماسیونی نساخته‌ای.", color=ft.Colors.OUTLINE)]) + form + lg, scroll=ft.ScrollMode.AUTO, spacing=8)
    async def del_auto(self, i):
        if await self.confirm("حذف شود؟"): await self.guard(self.api.delete(f"/automations/{i}")); await self.go("automation")

    async def v_analytics(self, days=30):
        d = await self.api.get(f"/analytics?days={days}"); t = d["totals"]; mx = max([1] + [x["published"] + x["failed"] for x in d["days"]])
        k = lambda l, v: ft.Container(content=ft.Column([T(digits(v), size=22, weight=ft.FontWeight.BOLD), T(l, size=12)], spacing=0), padding=10, border_radius=12, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, width=150)
        bars = ft.Row([ft.Container(width=max(2, 300 // max(len(d["days"]), 1) - 2), height=max(2, 100 * (x["published"] + x["failed"]) / mx), bgcolor=ft.Colors.ERROR if x["failed"] else ft.Colors.GREEN, tooltip=f'{x["date"]}: {x["published"]}/{x["failed"]}') for x in d["days"]],
                      vertical_alignment=ft.CrossAxisAlignment.END, height=110, spacing=2)
        per = [card(T(c["title"], weight=ft.FontWeight.BOLD), T(f'{digits(c["published"])} موفق · {digits(c["failed"])} ناموفق', size=12)) for c in d["channels"]]
        return ft.Column([header("آمار", "more", self), hscroll(*[(ft.FilledButton if days == n else ft.OutlinedButton)(content=f"{digits(n)} روز", on_click=lambda e, n=n: self.spawn(self.set_days(n))) for n in (7, 30, 90)]),
                          ft.Row([k("ارسال موفق", t["published"]), k("ارسال ناموفق", t["failed"]), k("نرخ خطا", f'{round(t["failure_rate"] * 100)}٪'), k("زمان‌بندی‌شده", t["scheduled"]), k("اندازه صف", t["queue"])], wrap=True, spacing=8, run_spacing=8),
                          T("انتشار روزانه", weight=ft.FontWeight.BOLD), bars, T("به تفکیک کانال", weight=ft.FontWeight.BOLD)] + (per or [T("هنوز ارسالی ثبت نشده است.")]) + [T("ماهانه", weight=ft.FontWeight.BOLD)] + [T(f'{m["month"]}: {digits(m["published"])} پست') for m in d["monthly"]] +
                         [T("این آمار فقط از عملیات خود برنامه به دست می‌آید؛ تعداد اعضا و بازدید کانال در API عمومی ایتایار در دسترس نیست.", size=12, color=ft.Colors.OUTLINE)], scroll=ft.ScrollMode.AUTO, spacing=8)
    async def set_days(self, n): self.body.content = await self.v_analytics(n); self.page.update()

    async def v_channels(self):
        v = tf("لینک یا شناسه کانال جدید")
        async def add(e):
            cs = await self.guard(self.api.get("/connections"))
            if cs and await self.guard(self.api.post("/channels", {"connection_id": cs[0]["id"], "value": v.value})):
                self.chs = await self.api.get("/channels"); await self.go("channels")
        return ft.Column([header("کانال‌ها", "more", self)] + [card(T(c["title"], weight=ft.FontWeight.BOLD), T(c["chat_id"], size=12)) for c in self.chs] + [v, btn("افزودن", lambda e: self.spawn(add(e)), True),
                          T("ویرایش و حذف پیام‌های قبلی، تاریخچه کانال و آمار رسمی: این عملیات در API عمومی فعلی ایتایار در دسترس نیست.", size=12, color=ft.Colors.OUTLINE)], scroll=ft.ScrollMode.AUTO, spacing=8)

    async def v_templates(self):
        tp = await self.api.get("/templates"); n, b = tf("نام قالب"), tf("متن ({{title}} {{summary}} {{link}} {{date}} {{hashtags}})", multiline=True, min_lines=4)
        async def add(e): await self.guard(self.api.post("/templates", {"name": n.value, "body": b.value})); await self.go("templates")
        async def rm(i):
            if await self.confirm("قالب حذف شود؟"): await self.guard(self.api.delete(f"/templates/{i}")); await self.go("templates")
        return ft.Column([header("قالب‌ها", "more", self)] + [card(ft.Row([T(t["name"], weight=ft.FontWeight.BOLD, expand=True)] + ([] if t["builtin"] else [btn("حذف", lambda e, i=t["id"]: self.spawn(rm(i)))])), T(t["body"], size=12, color=ft.Colors.OUTLINE)) for t in tp]
                         + [T("قالب اختصاصی", weight=ft.FontWeight.BOLD), n, b, btn("ذخیره قالب", lambda e: self.spawn(add(e)), True)], scroll=ft.ScrollMode.AUTO, spacing=8)

    async def v_settings(self):
        me = self.me; out = [header("تنظیمات", "more", self), T(f'{me["name"]} — {ROLE_FA[me["role"]]}'), T("سرور: " + self.api.base, size=12),
                             ft.Switch(label="تم تیره", value=self.dark, on_change=self.theme), btn("خروج", lambda e: self.spawn(self.logout()))]
        if me["role"] == "owner":
            team = await self.api.get("/team"); f = {"role": "editor"}; u, p, nm, eid = tf("نام کاربری"), tf("رمز عبور (حداقل ۸ نویسه)", password=True), tf("نام نمایشی"), tf("شناسه ایتا (اختیاری)")
            async def add(e):
                if await self.guard(self.api.post("/team", {"username": u.value, "password": p.value, "display_name": nm.value, "role": f["role"], "eitaa_id": eid.value or None})) is not None: self.toast("✓ عضو اضافه شد"); await self.go("settings")
            async def rm(i):
                if await self.confirm("عضو حذف شود؟"): await self.guard(self.api.delete(f"/team/{i}")); await self.go("settings")
            out += [T("اعضای تیم", size=16, weight=ft.FontWeight.BOLD)] + [card(ft.Row([T(f'{m["name"]} — {ROLE_FA[m["role"]]}', expand=True)] + ([btn("حذف", lambda e, i=m["id"]: self.spawn(rm(i)))] if m["id"] != me["id"] else []))) for m in team] + [u, nm, p,
                    dd("نقش", [(k, v) for k, v in ROLE_FA.items() if k != "owner"], "editor", lambda e: f.__setitem__("role", e.control.value)), eid, btn("افزودن عضو", lambda e: self.spawn(add(e)), True),
                    T("توکن ایتایار هرگز به اعضای تیم نمایش داده نمی‌شود.", size=12, color=ft.Colors.OUTLINE)]
        return ft.Column(out, scroll=ft.ScrollMode.AUTO, spacing=8)
    def theme(self, e): self.dark = e.control.value; self.page.theme_mode = ft.ThemeMode.DARK if self.dark else ft.ThemeMode.LIGHT; self.page.update()

async def main(page: ft.Page):
    asyncio.get_event_loop()
    page.title = "مدیریت کانال ایتا"; page.rtl = True; page.theme = ft.Theme(color_scheme_seed=ft.Colors.CYAN); page.padding = 0
    app = App(page); page.add(ft.SafeArea(content=app.body, expand=True)); await app.boot()

if __name__ == "__main__": ft.run(main)
