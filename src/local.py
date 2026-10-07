"""Local (offline) backend for the Eitaa Channel Manager client.

When `config.MODE == "local"`, the client does not talk to a FastAPI
server. Instead, `api.py` routes every request to the `LocalBackend`
class defined here.

What this module supports
-------------------------
    * Auto-connecting the default channel and EitaaYar bot on first run,
      using the credentials baked into `config.py`.
    * Direct AI calls to Groq (with the API key from `config.py`).
    * Direct publishing to EitaaYar (sendMessage only — no media upload,
      since that requires a persistent storage layer).
    * Local persistence of posts, templates, activity log and simple
      statistics using `page.client_storage`.

What this module deliberately does NOT support
----------------------------------------------
Operations that strictly require a 24/7 worker or a shared database:

    * Scheduling and the publish queue
    * Calendar view and rescheduling
    * Media library
    * Automations
    * Team / roles
    * Advanced analytics (per-channel, daily charts)

These raise `LocalError` with a clear Persian message. `api.py` catches
it and turns it into an `ApiError`, so `main.py` never needs to know
which backend answered the call.

`LocalBackend.handle()` speaks the same `(method, path, body)` shape as
the real HTTP client. This keeps the two backends interchangeable.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import httpx

import config


# ================================================================ constants ==

EITAAYAR_BASE = "https://eitaayar.ir/api"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Storage keys are prefixed so they never collide with the app's own keys
# (theme, server, token, intro_done, ...).
_STORE_PREFIX = "local:"


class LocalError(Exception):
    """Raised when an operation cannot be done in local mode.

    The message is user-facing and already in Persian.
    """


# =========================================================== AI prompt table ==

AI_SYSTEM = (
    "تو دستیار تولید محتوا برای کانال‌های ایتا هستی. "
    "فقط متن نهایی را به فارسی روان بنویس، بدون توضیح اضافه."
)

AI_ACTIONS: dict[str, str] = {
    "create": "از اطلاعات زیر یک پست مناسب کانال بساز:",
    "rewrite": "متن زیر را بازنویسی کن:",
    "shorter": "متن زیر را کوتاه‌تر کن و نکات اصلی را نگه دار:",
    "longer": "متن زیر را کامل‌تر و با جزئیات بیشتر بنویس:",
    "formal": "لحن متن زیر را رسمی کن:",
    "casual": "لحن متن زیر را خودمانی کن:",
    "news": "متن زیر را به سبک خبری بنویس:",
    "educational": "متن زیر را به سبک آموزشی بنویس:",
    "engaging": "متن زیر را جذاب‌تر کن:",
    "titles": "پنج عنوان پیشنهادی برای متن زیر بنویس (هر عنوان در یک خط):",
    "hashtags": "هشتگ‌های مناسب برای متن زیر پیشنهاد بده (در یک خط):",
    "translate": "متن زیر را ترجمه کن (فارسی به انگلیسی و غیرفارسی به فارسی):",
    "summarize": "متن زیر را خلاصه کن:",
    "proofread": "غلط‌های نگارشی و املایی متن زیر را اصلاح کن و معنا را تغییر نده:",
}


# ============================================================ channel utils ==

_CHANNEL_RE = re.compile(r"^(?:https?://)?(?:www\.)?eitaa\.com/([A-Za-z0-9_]+)/?$")


def normalize_channel(raw: str) -> str:
    """Convert an eitaa.com link or @username to a bare chat id."""
    raw = (raw or "").strip()
    m = _CHANNEL_RE.match(raw)
    if m:
        return m.group(1)
    return raw.lstrip("@")


# ============================================================== LocalBackend ==

class LocalBackend:
    """A tiny offline backend backed by the device's client storage."""

    # ---------------------------------------------------------------- init --
    def __init__(self, storage):
        # `storage` is the Store wrapper App passes around; it forwards to
        # page.client_storage and exposes async get/set/remove.
        self.storage = storage
        self.me = {
            "id": 0,
            "username": "local",
            "name": "کاربر محلی",
            "role": "owner",
        }

    # ----------------------------------------------------------- secrets ----
    def credentials_present(self) -> bool:
        """Return True when the hard-coded test credentials are usable."""
        return bool(config.EITAA_TOKEN and config.GROQ_API_KEY)

    # ------------------------------------------------------ storage helpers -
    async def _get(self, key: str, default=None):
        raw = await self.storage.get(_STORE_PREFIX + key)
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    async def _set(self, key: str, value) -> None:
        await self.storage.set(
            _STORE_PREFIX + key,
            json.dumps(value, ensure_ascii=False, default=str),
        )

    # ----------------------------------------------------- activity logging --
    async def _log(self, kind: str, message: str) -> None:
        log = await self._get("activity", []) or []
        log.insert(0, {
            "at": dt.datetime.utcnow().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
        })
        await self._set("activity", log[:60])

    # ------------------------------------------------------------- secrets --
    async def token(self) -> str:
        """Return the EitaaYar token currently in use."""
        return await self.storage.get(_STORE_PREFIX + "eitaa_token") or ""

    async def groq_key(self) -> str:
        """Return the Groq API key currently in use."""
        return await self.storage.get(_STORE_PREFIX + "groq_key") or ""

    async def forget_credentials(self) -> None:
        """Forget cached credentials (forces re-provisioning on next boot)."""
        await self.storage.remove(_STORE_PREFIX + "eitaa_token")
        await self.storage.remove(_STORE_PREFIX + "groq_key")

    # ---------------------------------------------------- first-run setup ---
    async def ensure_provisioned(self) -> None:
        """On first run, install the test credentials and the default channel.

        Idempotent — safe to call on every boot.
        """
        if not self.credentials_present():
            return

        # --- credentials ---
        if not await self.token():
            await self.storage.set(_STORE_PREFIX + "eitaa_token",
                                   config.EITAA_TOKEN)
        if not await self.groq_key():
            await self.storage.set(_STORE_PREFIX + "groq_key",
                                   config.GROQ_API_KEY)

        # --- default channel ---
        chans = await self._get("channels", []) or []
        if not chans and config.DEFAULT_CHANNEL_ID:
            cid = normalize_channel(config.DEFAULT_CHANNEL_ID)
            chans = [{"id": 1, "title": cid, "chat_id": cid}]
            await self._set("channels", chans)
            await self._log("channel", f"کانال پیش‌فرض {cid} اضافه شد")

        # --- default connection record (for UI display) ---
        conns = await self._get("connections", []) or []
        if not conns:
            conns = [{"id": 1, "name": "اتصال اصلی", "bot": "ربات ایتایار"}]
            await self._set("connections", conns)

        # --- default templates (only once) ---
        if await self._get("templates") is None:
            await self._set("templates", _DEFAULT_TEMPLATES.copy())

    # ============================================================ dispatcher =
    async def handle(self, method: str, path: str, body: Any = None,
                     files: dict | None = None) -> Any:
        """Route a `(method, path)` call the same way the HTTP client would.

        Raises `LocalError` for anything the local mode cannot do; the
        caller (`api.py`) turns it into an `ApiError` with the message.
        """
        path = path.split("?", 1)[0]  # strip query string; handled per-route

        # ---------------------------------------------------------- auth ---
        if path == "/auth/state":
            return {"has_users": True, "local": True}
        if path == "/auth/me":
            return self.me
        if path.startswith("/auth/"):
            raise LocalError("در حالت محلی نیازی به ورود نیست.")

        # --------------------------------------------------- connections ---
        if path == "/connections":
            if method == "GET":
                return await self._get("connections", []) or []
            if method == "POST":
                # In local mode the token is provisioned from config, not
                # from user input. Accept the call and confirm the bot.
                return await self._confirm_connection(body or {})

        # ------------------------------------------------------ channels ---
        if path == "/channels":
            if method == "GET":
                return await self._get("channels", []) or []
            if method == "POST":
                return await self._add_channel(body or {})

        # --------------------------------------------------------- posts ---
        if path == "/posts":
            if method == "GET":
                return await self._get("posts", []) or []
            if method == "POST":
                return await self._create_post(body or {})

        m = re.match(r"^/posts/(\d+)/cancel$", path)
        if m and method == "POST":
            return await self._cancel_post(int(m.group(1)))

        if re.match(r"^/posts/\d+/(approve|reject|reschedule)$", path):
            raise LocalError("تأیید، رد و زمان‌بندی پست فقط با سرور امکان‌پذیر است.")
        if re.match(r"^/deliveries/\d+/retry$", path):
            raise LocalError("تلاش دوباره فقط با سرور امکان‌پذیر است.")

        # ----------------------------------------------------- templates ---
        if path == "/templates":
            if method == "GET":
                return await self._get("templates", []) or []
            if method == "POST":
                return await self._add_template(body or {})

        m = re.match(r"^/templates/(\d+)$", path)
        if m and method == "DELETE":
            return await self._delete_template(int(m.group(1)))

        # --------------------------------------------------------- stats ---
        if path == "/stats":
            return await self._stats()
        if path == "/activity":
            return await self._get("activity", []) or []
        if path.startswith("/analytics"):
            raise LocalError("صفحه‌ی آمار کامل فقط با سرور در دسترس است.")

        # ------------------------------------------------------------ AI ---
        if path == "/ai" and method == "POST":
            return await self._ai(body or {})

        # --------------------------------------------------------- media ---
        if path.startswith("/media"):
            raise LocalError("کتابخانه رسانه فقط با سرور در دسترس است.")

        # -------------------------------------------- team / automations ---
        if path.startswith("/team"):
            raise LocalError("مدیریت تیم فقط با سرور در دسترس است.")
        if path.startswith("/automations"):
            raise LocalError("اتوماسیون فقط با سرور در دسترس است.")

        # -------------------------------------------------------- unknown --
        raise LocalError("این قابلیت در حالت محلی پشتیبانی نمی‌شود.")

    # ============================================================ handlers ==
    # ------------------------------------------------------ connections ----
    async def _confirm_connection(self, body: dict) -> dict:
        """Validate the config-provided token against EitaaYar and store it."""
        token = await self.token()
        if not token:
            raise LocalError("توکن ایتایار در برنامه تنظیم نشده است.")

        bot_name = "ربات ایتایار"
        try:
            async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as c:
                r = await c.post(f"{EITAAYAR_BASE}/{token}/getMe")
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise LocalError("ارتباط با ایتایار برقرار نشد. اینترنت را بررسی کن.")

        if r.status_code != 200 or not data.get("ok", True):
            raise LocalError("توکن ایتایار معتبر نیست.")

        result = data.get("result") or {}
        bot_name = str(result.get("first_name") or result.get("username") or bot_name)

        conns = [{"id": 1, "name": body.get("name", "اتصال اصلی"), "bot": bot_name}]
        await self._set("connections", conns)
        await self._log("connect", "اتصال ایتایار برقرار شد")
        return conns[0]

    # --------------------------------------------------------- channels ----
    async def _add_channel(self, body: dict) -> dict:
        raw = body.get("value") or ""
        cid = normalize_channel(raw)
        if not cid:
            raise LocalError("شناسه کانال معتبر نیست.")

        chans = await self._get("channels", []) or []
        if any(c.get("chat_id") == cid for c in chans):
            raise LocalError("این کانال قبلاً اضافه شده است.")

        new_id = max((c.get("id", 0) for c in chans), default=0) + 1
        ch = {
            "id": new_id,
            "title": (body.get("title") or cid).strip() or cid,
            "chat_id": cid,
        }
        chans.append(ch)
        await self._set("channels", chans)
        await self._log("channel", f"کانال {ch['title']} اضافه شد")
        return ch

    # ------------------------------------------------------------ posts ----
    async def _create_post(self, body: dict) -> dict:
        text = (body.get("text") or "").strip()
        if not text:
            raise LocalError("متن پست خالی است.")

        mode = body.get("mode", "draft")
        channel_ids = list(body.get("channel_ids") or [])

        if mode == "schedule":
            raise LocalError(
                "زمان‌بندی بدون سرور ممکن نیست؛ از «ارسال فوری» استفاده کن."
            )
        if mode != "draft" and not channel_ids:
            raise LocalError("حداقل یک کانال انتخاب کن.")

        posts = await self._get("posts", []) or []
        new_id = max((p.get("id", 0) for p in posts), default=0) + 1

        post = {
            "id": new_id,
            "title": (body.get("title") or "").strip(),
            "text": text,
            "status": "draft" if mode == "draft" else "queued",
            "scheduled_at": None,
            "silent": bool(body.get("silent")),
            "pin": bool(body.get("pin")),
            "view_to_delete": body.get("view_to_delete"),
            "media_id": None,
            "deliveries": [
                {
                    "id": new_id * 1000 + i,
                    "channel_id": cid,
                    "status": "draft" if mode == "draft" else "queued",
                    "error": "",
                }
                for i, cid in enumerate(channel_ids)
            ],
        }

        if mode == "now":
            await self._send_post_now(post)

        posts.insert(0, post)
        await self._set("posts", posts[:200])
        await self._log(
            "post",
            f"پست «{post['title'] or post['id']}» "
            + ("ذخیره شد" if mode == "draft" else "ارسال شد"),
        )
        return {"id": new_id, "status": post["status"]}

    async def _send_post_now(self, post: dict) -> None:
        token = await self.token()
        if not token:
            post["status"] = "failed"
            for d in post["deliveries"]:
                d["status"] = "failed"
                d["error"] = "توکن ایتایار تنظیم نشده است."
            return

        chans = {c["id"]: c for c in (await self._get("channels", []) or [])}
        any_ok = False

        async with httpx.AsyncClient(timeout=60) as c:
            for d in post["deliveries"]:
                ch = chans.get(d["channel_id"])
                if not ch:
                    d["status"] = "failed"
                    d["error"] = "کانال پیدا نشد."
                    continue

                payload: dict[str, Any] = {
                    "chat_id": ch["chat_id"],
                    "text": post["text"],
                }
                if post["title"]:
                    payload["title"] = post["title"]
                if post["silent"]:
                    payload["disable_notification"] = 1
                if post["pin"]:
                    payload["pin"] = 1
                if post["view_to_delete"]:
                    payload["view_to_delete"] = post["view_to_delete"]

                try:
                    r = await c.post(f"{EITAAYAR_BASE}/{token}/sendMessage",
                                     data=payload)
                    j = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    d["status"] = "failed"
                    d["error"] = f"ارتباط برقرار نشد ({type(e).__name__})"
                    continue

                if r.status_code == 200 and j.get("ok", True):
                    d["status"] = "published"
                    any_ok = True
                else:
                    d["status"] = "failed"
                    d["error"] = str(
                        j.get("description")
                        or j.get("message")
                        or "خطای نامشخص از ایتایار"
                    )

        all_ok = all(d["status"] == "published" for d in post["deliveries"])
        if all_ok:
            post["status"] = "published"
        elif any_ok:
            post["status"] = "published"  # partial success treated as published
        else:
            post["status"] = "failed"

        await self._log(
            "publish" if post["status"] == "published" else "error",
            f"«{post['title'] or post['id']}» → {post['status']}",
        )

    async def _cancel_post(self, pid: int) -> dict:
        posts = await self._get("posts", []) or []
        for p in posts:
            if p.get("id") == pid:
                p["status"] = "cancelled"
                for d in p.get("deliveries", []):
                    if d.get("status") == "queued":
                        d["status"] = "cancelled"
                break
        await self._set("posts", posts)
        await self._log("cancel", f"پست {pid} لغو شد")
        return {"ok": True}

    # -------------------------------------------------------- templates ----
    async def _add_template(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()
        text = (body.get("body") or "").strip()
        if not name or not text:
            raise LocalError("نام و متن قالب الزامی است.")

        tpls = await self._get("templates", []) or []
        new_id = max((t.get("id", 0) for t in tpls), default=0) + 1
        tpls.append({
            "id": new_id,
            "name": name,
            "category": "سفارشی",
            "body": text,
            "builtin": False,
        })
        await self._set("templates", tpls)
        return {"id": new_id}

    async def _delete_template(self, tid: int) -> dict:
        tpls = await self._get("templates", []) or []
        tpls = [t for t in tpls if t.get("id") != tid]
        await self._set("templates", tpls)
        return {"ok": True}

    # ------------------------------------------------------------ stats ----
    async def _stats(self) -> dict:
        posts = await self._get("posts", []) or []
        count = lambda *st: sum(1 for p in posts if p.get("status") in st)
        ai_usage = await self._get("ai_usage", []) or []
        return {
            "published": count("published"),
            "failed": count("failed"),
            "scheduled": 0,
            "queued": count("queued"),
            "ai_calls": len(ai_usage),
        }

    # --------------------------------------------------------------- AI ----
    async def _ai(self, body: dict) -> dict:
        key = await self.groq_key()
        if not key:
            raise LocalError("کلید Groq تنظیم نشده است.")

        action = body.get("action") or ""
        text = body.get("text") or ""
        prompt = body.get("prompt") or ""

        instruction = prompt.strip() or AI_ACTIONS.get(action)
        if not instruction:
            raise LocalError("عملیات هوش مصنوعی نامعتبر است.")
        if not (text.strip() or prompt.strip()):
            raise LocalError("ابتدا متنی بنویس.")

        user_msg = f"{instruction}\n\n{text}" if text else instruction

        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(
                    GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": config.GROQ_MODEL,
                        "messages": [
                            {"role": "system", "content": AI_SYSTEM},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": 0.7,
                        "max_tokens": 1500,
                    },
                )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise LocalError("ارتباط با سرویس هوش مصنوعی برقرار نشد.")

        if r.status_code != 200:
            msg = "سرویس هوش مصنوعی خطا داد."
            try:
                err = (data.get("error") or {}).get("message")
                if err:
                    msg += " " + str(err)
            except Exception:
                pass
            raise LocalError(msg)

        try:
            out = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError, TypeError):
            raise LocalError("پاسخ سرویس هوش مصنوعی نامعتبر بود.")

        usage = await self._get("ai_usage", []) or []
        usage.append({
            "at": dt.datetime.utcnow().isoformat(timespec="seconds"),
            "action": action,
            "chars_in": len(text),
            "chars_out": len(out),
        })
        await self._set("ai_usage", usage[-200:])
        return {"text": out}


# ============================================================ default data ==

_DEFAULT_TEMPLATES = [
    {"id": 1, "name": "خبر فوری", "category": "خبر",
     "body": "🔴 فوری | {{title}}\n\n{{summary}}\n\n{{link}}\n{{hashtags}}",
     "builtin": True},
    {"id": 2, "name": "خبر", "category": "خبر",
     "body": "📰 {{title}}\n\n{{summary}}\n\n📅 {{date}}\n{{link}}\n{{hashtags}}",
     "builtin": True},
    {"id": 3, "name": "معرفی محصول", "category": "معرفی",
     "body": "✨ {{title}}\n\n{{summary}}\n\n🔗 {{link}}\n{{hashtags}}",
     "builtin": True},
    {"id": 4, "name": "معرفی اپلیکیشن", "category": "معرفی",
     "body": "📱 {{title}}\n\n{{summary}}\n\n⬇️ دانلود: {{link}}\n{{hashtags}}",
     "builtin": True},
    {"id": 5, "name": "آموزشی", "category": "آموزشی",
     "body": "💡 {{title}}\n\n{{summary}}\n\n{{hashtags}}",
     "builtin": True},
    {"id": 6, "name": "اطلاعیه", "category": "اطلاعیه",
     "body": "📢 اطلاعیه\n\n{{title}}\n{{summary}}\n\n📅 {{date}}",
     "builtin": True},
    {"id": 7, "name": "تبلیغ", "category": "تبلیغ",
     "body": "🔥 {{title}}\n\n{{summary}}\n\n👉 {{link}}\n{{hashtags}}",
     "builtin": True},
    {"id": 8, "name": "معرفی بازی", "category": "معرفی",
     "body": "🎮 {{title}}\n\n{{summary}}\n\n⬇️ {{link}}\n{{hashtags}}",
     "builtin": True},
]
