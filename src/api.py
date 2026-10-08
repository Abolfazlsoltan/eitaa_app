"""Eitaa Manager — data and AI client.

This module is the app's single point of contact with the outside world.
It handles three concerns transparently:

    1. Data — posts, channels, templates, activity log.
    2. AI — calls to Groq.
    3. Publishing — calls to EitaaYar.

When ``config.SERVER_URL`` is set, all requests go through the FastAPI
backend at that address. When it is empty, everything happens on the
device. The UI never needs to know which path is being used.
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
from typing import Any

import httpx

import config


# ================================================================== constants

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_EITAA_BASE = "https://eitaayar.ir/api"

_TIMEOUT = 30.0
_TIMEOUT_AI = 90.0
_TIMEOUT_UPLOAD = 180.0


# ===================================================================== errors

class ApiError(Exception):
    """User-facing error. The message is always safe to display."""

    def __init__(self, message: str, network: bool = False):
        super().__init__(message)
        self.network = network


# ==================================================================== prompts

_SYSTEM = (
    "تو دستیار تولید محتوا برای کانال‌های ایتا هستی. "
    "فقط متن نهایی را به فارسی روان بنویس، بدون توضیح اضافه."
)

_AI_ACTIONS: dict[str, str] = {
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


# ============================================================== default data

_DEFAULT_TEMPLATES = [
    {"id": 1, "name": "خبر فوری", "category": "خبر",
     "body": "🔴 فوری | {{title}}\n\n{{summary}}\n\n{{link}}\n{{hashtags}}"},
    {"id": 2, "name": "خبر", "category": "خبر",
     "body": "📰 {{title}}\n\n{{summary}}\n\n📅 {{date}}\n{{link}}\n{{hashtags}}"},
    {"id": 3, "name": "معرفی محصول", "category": "معرفی",
     "body": "✨ {{title}}\n\n{{summary}}\n\n🔗 {{link}}\n{{hashtags}}"},
    {"id": 4, "name": "معرفی اپلیکیشن", "category": "معرفی",
     "body": "📱 {{title}}\n\n{{summary}}\n\n⬇️ دانلود: {{link}}\n{{hashtags}}"},
    {"id": 5, "name": "آموزشی", "category": "آموزشی",
     "body": "💡 {{title}}\n\n{{summary}}\n\n{{hashtags}}"},
    {"id": 6, "name": "اطلاعیه", "category": "اطلاعیه",
     "body": "📢 اطلاعیه\n\n{{title}}\n{{summary}}\n\n📅 {{date}}"},
    {"id": 7, "name": "تبلیغ", "category": "تبلیغ",
     "body": "🔥 {{title}}\n\n{{summary}}\n\n👉 {{link}}\n{{hashtags}}"},
    {"id": 8, "name": "معرفی بازی", "category": "معرفی",
     "body": "🎮 {{title}}\n\n{{summary}}\n\n⬇️ {{link}}\n{{hashtags}}"},
]


# =================================================================== helpers

_CHANNEL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?eitaa\.com/([A-Za-z0-9_]+)/?$"
)


def normalize_channel(raw: str) -> str:
    """Turn a link, @username or id into a bare channel identifier."""
    raw = (raw or "").strip()
    m = _CHANNEL_RE.match(raw)
    if m:
        return m.group(1)
    return raw.lstrip("@")


def _iso_now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


# ====================================================================== Api

class Api:
    """Unified data + AI client."""

    def __init__(self, storage):
        self.storage = storage
        self.base = (config.SERVER_URL or "").rstrip("/")
        self.token: str | None = None
        self.on_unauthorized = lambda: None

        self._client: httpx.AsyncClient | None = None
        self._ready = False

    # ---------------------------------------------------------- lifecycle --
    @property
    def online(self) -> bool:
        """True when a backend is configured."""
        return bool(self.base)

    async def bootstrap(self) -> None:
        """Prepare defaults. Safe to call more than once."""
        if self._ready:
            return
        if not self.online:
            await self._provision()
        self._ready = True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------- public API ---
    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._dispatch("GET", path, None, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self._dispatch("POST", path, body)

    async def delete(self, path: str) -> Any:
        return await self._dispatch("DELETE", path, None)

    async def ai(self, action: str, text: str, prompt: str = "") -> str:
        result = await self._dispatch(
            "POST", "/ai",
            {"action": action, "text": text, "prompt": prompt},
            timeout=_TIMEOUT_AI,
        )
        return str((result or {}).get("text", ""))

    async def upload(self, name: str, data: bytes, tags: str = "") -> Any:
        if not self.online:
            raise ApiError("کتابخانه رسانه در حال حاضر در دسترس نیست.")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return await self._dispatch(
            "POST", "/media", None,
            files={"file": (name, data, ctype)},
            data={"tags": tags} if tags else None,
            timeout=_TIMEOUT_UPLOAD,
        )

    # -------------------------------------------------------- dispatcher ---
    async def _dispatch(self, method, path, body, *, params=None,
                        files=None, data=None, timeout=None):
        if self.online:
            return await self._http(method, path, body,
                                    params, files, data, timeout)
        return await self._device(method, path, body)

    # -------------------------------------------------------- HTTP path ----
    async def _http(self, method, path, body, params, files, data, timeout):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        url = f"{self.base}/api{path}"

        try:
            r = await self._client.request(
                method, url,
                json=body, params=params, files=files, data=data,
                headers=headers, timeout=timeout or _TIMEOUT,
            )
        except (httpx.TimeoutException, httpx.HTTPError, ValueError):
            raise ApiError("ارتباط با سرور برقرار نشد. لطفاً دوباره تلاش کنید.",
                           network=True)

        try:
            payload = r.json() if r.content else {}
        except ValueError:
            payload = {}

        if r.status_code == 401 and self.token:
            self.on_unauthorized()

        if r.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise ApiError(detail if isinstance(detail, str) else "خطایی رخ داد.")

        return payload

    # -------------------------------------------------------- device path --
    async def _provision(self) -> None:
        """Silently install credentials and defaults on first run."""
        if config.EITAA_TOKEN and not await self.storage.get("token"):
            await self.storage.set("token", config.EITAA_TOKEN)
        if config.GROQ_API_KEY and not await self.storage.get("groq_key"):
            await self.storage.set("groq_key", config.GROQ_API_KEY)

        channels = await self._load("channels", [])
        if not channels and config.EITAA_CHANNEL:
            cid = normalize_channel(config.EITAA_CHANNEL)
            await self._save("channels", [
                {"id": 1, "title": "کانال من", "chat_id": cid}
            ])
            await self._log("channel", "کانال به اپلیکیشن متصل شد")

        if await self._load("templates", None) is None:
            await self._save("templates", _DEFAULT_TEMPLATES)

        if not await self._load("connections", []):
            await self._save("connections", [
                {"id": 1, "name": "اتصال اصلی", "bot": "ربات ایتا"}
            ])

    async def _device(self, method: str, path: str, body: Any) -> Any:
        path = path.split("?", 1)[0]

        # ---- auth ----
        if path == "/auth/state":
            return {"has_users": True}
        if path == "/auth/me":
            return {"id": 0, "username": "me", "name": "کاربر", "role": "owner"}

        # ---- connections ----
        if path == "/connections":
            if method == "POST":
                return await self._connect_eitaa(body or {})
            return await self._load("connections", [])

        # ---- channels ----
        if path == "/channels":
            if method == "POST":
                return await self._add_channel(body or {})
            return await self._load("channels", [])

        # ---- posts ----
        if path == "/posts":
            if method == "POST":
                return await self._create_post(body or {})
            return await self._load("posts", [])

        m = re.match(r"^/posts/(\d+)/cancel$", path)
        if m and method == "POST":
            return await self._cancel_post(int(m.group(1)))

        # ---- templates ----
        if path == "/templates":
            if method == "POST":
                return await self._add_template(body or {})
            return await self._load("templates", [])

        m = re.match(r"^/templates/(\d+)$", path)
        if m and method == "DELETE":
            return await self._delete_template(int(m.group(1)))

        # ---- stats / activity ----
        if path == "/stats":
            return await self._stats()
        if path == "/activity":
            return await self._load("activity", [])

        # ---- AI ----
        if path == "/ai":
            return await self._ai(body or {})

        raise ApiError("این قابلیت در حال حاضر در دسترس نیست.")

    # -------------------------------------------------------- storage ------
    async def _load(self, key: str, default):
        raw = await self.storage.get("data:" + key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    async def _save(self, key: str, value) -> None:
        await self.storage.set(
            "data:" + key,
            json.dumps(value, ensure_ascii=False, default=str),
        )

    async def _log(self, kind: str, message: str) -> None:
        log = await self._load("activity", [])
        log.insert(0, {"at": _iso_now(), "kind": kind, "message": message})
        await self._save("activity", log[:config.ACTIVITY_LIMIT])

    # -------------------------------------------------------- EitaaYar -----
    async def _connect_eitaa(self, body: dict) -> dict:
        token = await self.storage.get("token") or config.EITAA_TOKEN
        if not token:
            raise ApiError("اتصال به ایتا در دسترس نیست.")

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.post(f"{_EITAA_BASE}/{token}/getMe")
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise ApiError("ارتباط با ایتا برقرار نشد. اتصال اینترنت را بررسی کنید.",
                           network=True)

        if r.status_code != 200 or not data.get("ok", True):
            raise ApiError("اتصال به ایتا ناموفق بود.")

        result = data.get("result") or {}
        bot_name = str(result.get("first_name")
                       or result.get("username")
                       or "ربات ایتا")
        conns = [{"id": 1, "name": body.get("name", "اتصال اصلی"),
                  "bot": bot_name}]
        await self._save("connections", conns)
        return conns[0]

    async def _add_channel(self, body: dict) -> dict:
        cid = normalize_channel(body.get("value") or "")
        if not cid:
            raise ApiError("شناسه کانال معتبر نیست.")

        channels = await self._load("channels", [])
        if any(c.get("chat_id") == cid for c in channels):
            raise ApiError("این کانال قبلاً اضافه شده است.")

        new_id = max((c.get("id", 0) for c in channels), default=0) + 1
        channel = {
            "id": new_id,
            "title": (body.get("title") or "کانال جدید").strip(),
            "chat_id": cid,
        }
        channels.append(channel)
        await self._save("channels", channels)
        await self._log("channel", f"کانال «{channel['title']}» اضافه شد")
        return channel

    # ---------------------------------------------------------- posts ------
    async def _create_post(self, body: dict) -> dict:
        text = (body.get("text") or "").strip()
        if not text:
            raise ApiError("متن پست نمی‌تواند خالی باشد.")

        mode = body.get("mode", "draft")
        if mode == "schedule":
            raise ApiError("زمان‌بندی در حال حاضر در دسترس نیست.")

        channel_ids = list(body.get("channel_ids") or [])
        if mode != "draft" and not channel_ids:
            raise ApiError("حداقل یک کانال انتخاب کنید.")

        posts = await self._load("posts", [])
        new_id = max((p.get("id", 0) for p in posts), default=0) + 1

        post = {
            "id": new_id,
            "title": (body.get("title") or "").strip(),
            "text": text,
            "status": "draft" if mode == "draft" else "queued",
            "scheduled_at": None,
            "silent": bool(body.get("silent")),
            "pin": bool(body.get("pin")),
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
            await self._publish(post)

        posts.insert(0, post)
        await self._save("posts", posts[:config.POSTS_LIMIT])
        return {"id": new_id, "status": post["status"]}

    async def _publish(self, post: dict) -> None:
        token = await self.storage.get("token") or config.EITAA_TOKEN
        if not token:
            for d in post["deliveries"]:
                d["status"], d["error"] = "failed", "اتصال به ایتا برقرار نشده"
            post["status"] = "failed"
            return

        channels = {c["id"]: c for c in await self._load("channels", [])}
        success = 0

        async with httpx.AsyncClient(timeout=60) as c:
            for d in post["deliveries"]:
                ch = channels.get(d["channel_id"])
                if not ch:
                    d["status"], d["error"] = "failed", "کانال یافت نشد"
                    continue

                payload = {"chat_id": ch["chat_id"], "text": post["text"]}
                if post["title"]:
                    payload["title"] = post["title"]
                if post["silent"]:
                    payload["disable_notification"] = 1
                if post["pin"]:
                    payload["pin"] = 1

                try:
                    r = await c.post(f"{_EITAA_BASE}/{token}/sendMessage",
                                     data=payload)
                    j = r.json()
                except (httpx.HTTPError, ValueError):
                    d["status"], d["error"] = "failed", "خطای ارتباط"
                    continue

                if r.status_code == 200 and j.get("ok", True):
                    d["status"] = "published"
                    success += 1
                else:
                    d["status"] = "failed"
                    d["error"] = str(j.get("description")
                                     or j.get("message") or "خطای نامشخص")

        post["status"] = "published" if success else "failed"
        await self._log(
            "publish" if post["status"] == "published" else "error",
            f"«{post['title'] or post['id']}» "
            + ("منتشر شد" if post["status"] == "published" else "منتشر نشد"),
        )

    async def _cancel_post(self, post_id: int) -> dict:
        posts = await self._load("posts", [])
        for p in posts:
            if p.get("id") == post_id:
                p["status"] = "cancelled"
                for d in p.get("deliveries", []):
                    if d.get("status") == "queued":
                        d["status"] = "cancelled"
                break
        await self._save("posts", posts)
        return {"ok": True}

    # ------------------------------------------------------ templates ------
    async def _add_template(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()
        text = (body.get("body") or "").strip()
        if not name or not text:
            raise ApiError("نام و متن قالب الزامی است.")

        templates = await self._load("templates", [])
        new_id = max((t.get("id", 0) for t in templates), default=0) + 1
        templates.append({
            "id": new_id, "name": name, "body": text,
            "category": "سفارشی", "builtin": False,
        })
        await self._save("templates", templates)
        return {"id": new_id}

    async def _delete_template(self, tid: int) -> dict:
        templates = await self._load("templates", [])
        templates = [t for t in templates if t.get("id") != tid]
        await self._save("templates", templates)
        return {"ok": True}

    # ---------------------------------------------------------- stats ------
    async def _stats(self) -> dict:
        posts = await self._load("posts", [])
        count = lambda *s: sum(1 for p in posts if p.get("status") in s)
        return {
            "published": count("published"),
            "failed": count("failed"),
            "scheduled": 0,
            "queued": count("queued"),
            "ai_calls": len(await self._load("ai_usage", [])),
        }

    # ------------------------------------------------------------ AI -------
    async def _ai(self, body: dict) -> dict:
        key = await self.storage.get("groq_key") or config.GROQ_API_KEY
        if not key:
            raise ApiError("سرویس هوش مصنوعی در دسترس نیست.")

        action = body.get("action") or ""
        text = body.get("text") or ""
        prompt = body.get("prompt") or ""

        instruction = prompt.strip() or _AI_ACTIONS.get(action)
        if not instruction:
            raise ApiError("عملیات نامعتبر است.")
        if not (text.strip() or prompt.strip()):
            raise ApiError("لطفاً ابتدا متنی بنویسید.")

        user_msg = f"{instruction}\n\n{text}" if text else instruction

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_AI) as c:
                r = await c.post(
                    _GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": config.GROQ_MODEL,
                        "messages": [
                            {"role": "system", "content": _SYSTEM},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": 0.7,
                        "max_tokens": 1500,
                    },
                )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise ApiError("ارتباط با سرویس هوش مصنوعی برقرار نشد.",
                           network=True)

        if r.status_code != 200:
            raise ApiError("سرویس هوش مصنوعی پاسخگو نیست.")

        try:
            out = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise ApiError("پاسخ نامعتبر از سرویس هوش مصنوعی.")

        usage = await self._load("ai_usage", [])
        usage.append({"at": _iso_now(), "action": action,
                      "chars_in": len(text), "chars_out": len(out)})
        await self._save("ai_usage", usage[-200:])
        return {"text": out}
