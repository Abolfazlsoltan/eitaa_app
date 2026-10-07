"""Local-only backend for the client when no FastAPI server is reachable.

Provides a subset of the server's operations using only what is available
on the device:

    * Data (posts, templates, channels, connections, activity log) stored in
      page.client_storage as JSON blobs.
    * AI calls go directly to Groq using the user's own API key.
    * Post sending goes directly to EitaaYar using the user's own token.

Not supported locally (handlers raise LocalError with a Persian message):
    * Multi-user, roles, team management
    * Server-side scheduling / queue / worker
    * Media library (large files are not persisted on-device)
    * Advanced analytics, automations

The dispatcher `LocalBackend.handle()` speaks the same (method, path, body)
shape as the real HTTP client, so `api.py` can route to it transparently.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Any

import httpx

# --------------------------------------------------------------------- constants
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
EITAAYAR_BASE = "https://eitaayar.ir/api"
REQUEST_TIMEOUT = 30.0


class LocalError(Exception):
    """Raised when an operation cannot be performed in local mode."""


# --------------------------------------------------------------- AI prompt table
# Mirrors backend/ai.py — kept here so the client can work without a server.
AI_SYSTEM = (
    "تو دستیار تولید محتوا برای کانال‌های ایتا هستی. "
    "فقط متن نهایی را به فارسی روان بنویس، بدون توضیح اضافه."
)
AI_ACTIONS = {
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

# --------------------------------------------------------------- channel utils
_CHANNEL_RE = re.compile(r"^(?:https?://)?(?:www\.)?eitaa\.com/([A-Za-z0-9_]+)/?$")


def normalize_channel(raw: str) -> str:
    raw = (raw or "").strip()
    m = _CHANNEL_RE.match(raw)
    if m:
        return m.group(1)
    return raw.lstrip("@")


# =============================================================== LocalBackend ==
class LocalBackend:
    """A tiny offline backend backed by client_storage."""

    # ------------------------------------------------------------------ init
    def __init__(self, storage):
        # `storage` is the same Store wrapper App uses (page.client_storage).
        self.storage = storage
        self.me = {
            "id": 0,
            "username": "local",
            "name": "کاربر محلی",
            "role": "owner",
        }

    # ---------------------------------------------------------- storage helpers
    async def _get(self, key: str, default=None):
        raw = await self.storage.get("local:" + key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    async def _set(self, key: str, value):
        await self.storage.set(
            "local:" + key,
            json.dumps(value, ensure_ascii=False, default=str),
        )

    async def _append_activity(self, kind: str, message: str):
        log = await self._get("activity", [])
        log.insert(0, {
            "at": dt.datetime.utcnow().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
        })
        await self._set("activity", log[:50])

    # ------------------------------------------------------------ credentials
    async def get_eitaa_token(self) -> str:
        return (await self.storage.get("local:eitaa_token")) or ""

    async def set_eitaa_token(self, token: str):
        await self.storage.set("local:eitaa_token", (token or "").strip())

    async def get_groq_key(self) -> str:
        return (await self.storage.get("local:groq_key")) or ""

    async def set_groq_key(self, key: str):
        await self.storage.set("local:groq_key", (key or "").strip())

    # ================================================================ dispatcher
    async def handle(self, method: str, path: str, body: Any = None, files: dict | None = None):
        """Route a (method, path) pair to a local handler.

        Raises `LocalError` for endpoints that make no sense without a server;
        `api.py` catches that and turns it into a friendly ApiError message.
        """
        # ------------------------------------------------------------- auth
        if path == "/auth/state":
            return {"has_users": True, "local": True}
        if path == "/auth/me":
            return self.me
        if path.startswith("/auth/"):
            raise LocalError("ورود و ثبت‌نام در حالت محلی لازم نیست؛ وارد شده‌ای.")

        # ------------------------------------------------------- connections
        if path == "/connections" and method == "GET":
            return await self._get("connections", [])
        if path == "/connections" and method == "POST":
            return await self._add_connection(body or {})

        # ---------------------------------------------------------- channels
        if path == "/channels" and method == "GET":
            return await self._get("channels", [])
        if path == "/channels" and method == "POST":
            return await self._add_channel(body or {})

        # ------------------------------------------------------------- posts
        if path == "/posts" and method == "GET":
            return await self._get("posts", [])
        if path == "/posts" and method == "POST":
            return await self._create_post(body or {})
        m = re.match(r"^/posts/(\d+)/cancel$", path)
        if m and method == "POST":
            return await self._cancel_post(int(m.group(1)))
        if re.match(r"^/posts/\d+/(approve|reject|reschedule)$", path):
            raise LocalError("تأیید، رد و زمان‌بندی پست فقط با سرور امکان‌پذیر است.")
        if re.match(r"^/deliveries/\d+/retry$", path):
            raise LocalError("تلاش دوباره فقط با سرور امکان‌پذیر است.")

        # --------------------------------------------------------- templates
        if path == "/templates" and method == "GET":
            return await self._get("templates", [])
        if path == "/templates" and method == "POST":
            return await self._add_template(body or {})
        m = re.match(r"^/templates/(\d+)$", path)
        if m and method == "DELETE":
            return await self._delete_template(int(m.group(1)))

        # ------------------------------------------------------------- stats
        if path == "/stats":
            return await self._stats()
        if path == "/activity":
            return await self._get("activity", [])
        if path.startswith("/analytics"):
            raise LocalError("صفحه‌ی آمار فقط با سرور در دسترس است.")

        # ---------------------------------------------------------------- AI
        if path == "/ai" and method == "POST":
            return await self._ai(body or {})

        # ------------------------------------------------------------- media
        if path.startswith("/media"):
            raise LocalError("کتابخانه رسانه فقط با سرور در دسترس است.")

        # ------------------------------------------------ team / automation
        if path.startswith("/team"):
            raise LocalError("مدیریت تیم فقط با سرور در دسترس است.")
        if path.startswith("/automations"):
            raise LocalError("اتوماسیون فقط با سرور در دسترس است.")

        # --------------------------------------------------------- fallback
        raise LocalError("این قابلیت در حالت محلی پشتیبانی نمی‌شود.")

    # ================================================================ handlers

    # ------------------------------------------------------- connections ------
    async def _add_connection(self, body: dict):
        token = (body.get("token") or "").strip()
        if not token:
            raise LocalError("توکن ایتایار خالی است.")
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
                r = await c.post(f"{EITAAYAR_BASE}/{token}/getMe")
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise LocalError("ارتباط با ایتایار برقرار نشد. اینترنت را بررسی کن.")
        if r.status_code != 200 or not data.get("ok", True):
            raise LocalError("توکن ایتایار معتبر نیست.")

        await self.set_eitaa_token(token)
        conns = await self._get("connections", [])
        bot_name = str(data.get("result", {}).get("first_name", "ربات"))
        conn = {"id": 1, "name": body.get("name", "اتصال محلی"), "bot": bot_name}
        if not conns:
            conns = [conn]
        else:
            conns[0] = conn
        await self._set("connections", conns)
        await self._append_activity("connect", "اتصال ایتایار برقرار شد")
        return conn

    # ---------------------------------------------------------- channels ------
    async def _add_channel(self, body: dict):
        raw = body.get("value") or ""
        cid = normalize_channel(raw)
        if not cid:
            raise LocalError("شناسه کانال معتبر نیست.")

        conns = await self._get("connections", [])
        if not conns:
            raise LocalError("ابتدا توکن ایتایار را در تنظیمات وارد کن.")

        chans = await self._get("channels", [])
        if any(c["chat_id"] == cid for c in chans):
            raise LocalError("این کانال قبلاً اضافه شده است.")

        new_id = (max((c["id"] for c in chans), default=0) + 1)
        ch = {
            "id": new_id,
            "title": body.get("title") or cid,
            "chat_id": cid,
        }
        chans.append(ch)
        await self._set("channels", chans)
        await self._append_activity("channel", f"کانال {ch['title']} اضافه شد")
        return ch

    # ------------------------------------------------------------- posts ------
    async def _create_post(self, body: dict):
        text = (body.get("text") or "").strip()
        if not text:
            raise LocalError("متن پست خالی است.")

        mode = body.get("mode", "draft")
        channel_ids = body.get("channel_ids") or []
        if mode != "draft" and not channel_ids:
            raise LocalError("حداقل یک کانال انتخاب کن.")
        if mode == "schedule":
            raise LocalError("زمان‌بندی بدون سرور ممکن نیست. از «ارسال فوری» استفاده کن.")

        posts = await self._get("posts", [])
        new_id = (max((p["id"] for p in posts), default=0) + 1)
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
                {"id": new_id * 1000 + i, "channel_id": cid,
                 "status": "draft" if mode == "draft" else "queued",
                 "error": ""}
                for i, cid in enumerate(channel_ids)
            ],
        }

        if mode == "now":
            await self._send_post_now(post)

        posts.insert(0, post)
        await self._set("posts", posts)
        await self._append_activity(
            "post",
            f"پست «{post['title'] or post['id']}» "
            f"{'ذخیره شد' if mode == 'draft' else 'ارسال شد'}"
        )
        return {"id": new_id, "status": post["status"]}

    async def _send_post_now(self, post: dict):
        token = await self.get_eitaa_token()
        if not token:
            post["status"] = "failed"
            for d in post["deliveries"]:
                d["status"] = "failed"
                d["error"] = "توکن ایتایار تنظیم نشده است."
            return

        chans = {c["id"]: c for c in await self._get("channels", [])}
        any_ok = False
        async with httpx.AsyncClient(timeout=60) as c:
            for d in post["deliveries"]:
                ch = chans.get(d["channel_id"])
                if not ch:
                    d["status"] = "failed"
                    d["error"] = "کانال پیدا نشد."
                    continue
                try:
                    payload = {
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

                    r = await c.post(f"{EITAAYAR_BASE}/{token}/sendMessage", data=payload)
                    j = r.json()
                    if r.status_code == 200 and j.get("ok", True):
                        d["status"] = "published"
                        any_ok = True
                    else:
                        d["status"] = "failed"
                        d["error"] = str(
                            j.get("description") or j.get("message") or "خطای ایتایار"
                        )
                except (httpx.HTTPError, ValueError) as e:
                    d["status"] = "failed"
                    d["error"] = f"ارتباط برقرار نشد ({type(e).__name__})"

        if all(d["status"] == "published" for d in post["deliveries"]):
            post["status"] = "published"
        elif any_ok:
            post["status"] = "published"  # partial success treated as published
        else:
            post["status"] = "failed"

        await self._append_activity(
            "publish" if post["status"] == "published" else "error",
            f"«{post['title'] or post['id']}»: {post['status']}"
        )

    async def _cancel_post(self, pid: int):
        posts = await self._get("posts", [])
        for p in posts:
            if p["id"] == pid:
                p["status"] = "cancelled"
                for d in p["deliveries"]:
                    if d["status"] == "queued":
                        d["status"] = "cancelled"
                break
        await self._set("posts", posts)
        await self._append_activity("cancel", f"پست {pid} لغو شد")
        return {"ok": True}

    # --------------------------------------------------------- templates ------
    async def _add_template(self, body: dict):
        name = (body.get("name") or "").strip()
        text = (body.get("body") or "").strip()
        if not name or not text:
            raise LocalError("نام و متن قالب الزامی است.")
        tpls = await self._get("templates", [])
        new_id = (max((t["id"] for t in tpls), default=0) + 1)
        tpls.append({
            "id": new_id,
            "name": name,
            "category": "سفارشی",
            "body": text,
            "builtin": False,
        })
        await self._set("templates", tpls)
        return {"id": new_id}

    async def _delete_template(self, tid: int):
        tpls = await self._get("templates", [])
        tpls = [t for t in tpls if t["id"] != tid]
        await self._set("templates", tpls)
        return {"ok": True}

    # --------------------------------------------------------------- stats ----
    async def _stats(self):
        posts = await self._get("posts", [])
        c = lambda *st: sum(1 for p in posts if p["status"] in st)
        return {
            "published": c("published"),
            "failed": c("failed"),
            "scheduled": 0,
            "queued": c("queued"),
            "ai_calls": len(await self._get("ai_usage", [])),
        }

    # ------------------------------------------------------------------- AI ---
    async def _ai(self, body: dict):
        key = await self.get_groq_key()
        if not key:
            raise LocalError(
                "کلید Groq تنظیم نشده است. از تنظیمات، کلید خودت را وارد کن."
            )

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
                        "model": GROQ_MODEL,
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
            raise LocalError("ارتباط با Groq برقرار نشد. اینترنت را بررسی کن.")

        if r.status_code != 200:
            msg = "سرویس Groq خطا داد."
            try:
                err = data.get("error", {}).get("message")
                if err:
                    msg += " " + str(err)
            except Exception:
                pass
            raise LocalError(msg)

        try:
            out = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError):
            raise LocalError("پاسخ Groq نامعتبر بود.")

        usage = await self._get("ai_usage", [])
        usage.append({
            "at": dt.datetime.utcnow().isoformat(timespec="seconds"),
            "action": action,
            "chars_in": len(text),
            "chars_out": len(out),
        })
        await self._set("ai_usage", usage[-200:])
        return {"text": out}
