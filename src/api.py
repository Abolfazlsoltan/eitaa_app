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
from urllib.parse import urlparse

import httpx

import config


# ================================================================== constants

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_EITAA_BASE = "https://eitaayar.ir/api"
_AI_PROVIDERS = ("groq", "openai", "gemini", "anthropic")
_AI_DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-6",
}

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

    def set_backend(self, base: str) -> None:
        self.base = (base or "").rstrip("/")
        self.token = None
        self._ready = False

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

    async def put(self, path: str, body: Any = None) -> Any:
        return await self._dispatch("PUT", path, body)

    async def delete(self, path: str) -> Any:
        return await self._dispatch("DELETE", path, None)

    async def auth_state(self) -> dict:
        return await self.get("/auth/state")

    async def login(self, username: str, password: str) -> dict:
        return await self._start_session("/auth/login", {
            "username": username,
            "password": password,
        })

    async def register(
        self, username: str, password: str, display_name: str = ""
    ) -> dict:
        return await self._start_session("/auth/register", {
            "username": username,
            "password": password,
            "display_name": display_name,
        })

    async def _start_session(self, path: str, body: dict) -> dict:
        if not self.online:
            raise ApiError("ورود و ثبت‌نام به اتصال سرور نیاز دارد.")
        result = await self._http("POST", path, body, None, None, None, None)
        token = result.get("token") if isinstance(result, dict) else None
        user = result.get("user") if isinstance(result, dict) else None
        if not isinstance(token, str) or not isinstance(user, dict):
            raise ApiError("پاسخ احراز هویت از سرور معتبر نیست.")
        self.token = token
        return user

    def logout(self) -> None:
        self.token = None

    async def ai(self, action: str, text: str, prompt: str = "") -> str:
        result = await self._dispatch(
            "POST", "/ai",
            {"action": action, "text": text, "prompt": prompt},
            timeout=_TIMEOUT_AI,
        )
        return str((result or {}).get("text", ""))

    async def ai_settings(self) -> dict:
        provider = await self.storage.get("ai_provider") or "groq"
        if provider not in _AI_PROVIDERS:
            provider = "groq"
        model = await self.storage.get("ai_model")
        has_key = await self.ai_key_configured(provider)
        default_model = (
            (config.GROQ_MODEL or _AI_DEFAULT_MODELS["groq"])
            if provider == "groq" else _AI_DEFAULT_MODELS[provider]
        )
        return {
            "provider": provider,
            "model": model or default_model,
            "has_key": has_key,
            "base_url": await self.storage.get("ai_base_url") or "",
        }

    async def ai_key_configured(self, provider: str) -> bool:
        if provider not in _AI_PROVIDERS:
            raise ApiError("ارائه‌دهنده هوش مصنوعی پشتیبانی نمی‌شود.")
        key_name = "groq_key" if provider == "groq" else f"ai_key_{provider}"
        key = await self.storage.get(key_name)
        if provider == "groq" and not key:
            key = config.GROQ_API_KEY
        return bool(key)

    async def save_ai_settings(
        self, provider: str, model: str, key: str, base_url: str = ""
    ) -> None:
        if provider not in _AI_PROVIDERS:
            raise ApiError("ارائه‌دهنده هوش مصنوعی پشتیبانی نمی‌شود.")
        if not model.strip():
            raise ApiError("نام مدل را وارد کنید.")
        if provider == "openai":
            self._require_https_endpoint(base_url, allow_empty=True)
        key_name = "groq_key" if provider == "groq" else f"ai_key_{provider}"
        new_key = key.strip()
        if new_key:
            existing_key = await self.storage.get(key_name)
            if provider == "groq" and not existing_key:
                existing_key = config.GROQ_API_KEY
            if existing_key and existing_key != new_key:
                raise ApiError(
                    "کلید از قبل تنظیم شده و برای حفظ مقدار موجود از این صفحه "
                    "قابل جایگزینی نیست."
                )
        await self._store_verified("ai_provider", provider)
        await self._store_verified("ai_model", model.strip())
        if provider == "openai":
            await self._store_verified(
                "ai_base_url", base_url.strip().rstrip("/")
            )
        if new_key and not existing_key:
            await self._store_verified(key_name, new_key)

    @staticmethod
    def _require_https_endpoint(url: str, *, allow_empty: bool = False) -> None:
        if not url and allow_empty:
            return
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise ApiError("نشانی سرویس باید یک آدرس HTTPS معتبر باشد.")

    async def _store_verified(self, name: str, value: str) -> None:
        await self.storage.set(name, value)
        if await self.storage.get(name) != value:
            raise ApiError("ذخیره تنظیمات ناموفق بود؛ فضای ذخیره‌سازی را بررسی کنید.")

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

    async def _device(self, method: str, path: str, body: Any) -> Any:
        path = path.split("?", 1)[0]

        # ---- auth ----
        if path == "/auth/state":
            raise ApiError("احراز هویت به اتصال سرور نیاز دارد.")
        if path == "/auth/me":
            raise ApiError("احراز هویت به اتصال سرور نیاز دارد.")

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

        m = re.match(r"^/posts/(\d+)$", path)
        if m and method == "PUT":
            return await self._update_draft(int(m.group(1)), body or {})
        if m and method == "DELETE":
            return await self._delete_draft(int(m.group(1)))

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
        stored_token = await self.storage.get("token")
        configured_token = config.EITAA_TOKEN
        submitted_token = (body.get("token") or "").strip()
        existing_token = stored_token or configured_token
        if existing_token and submitted_token and submitted_token != existing_token:
            raise ApiError(
                "توکن از قبل در تنظیمات برنامه وجود دارد و برای حفظ تنظیم موجود "
                "قابل جایگزینی از این صفحه نیست."
            )
        token = existing_token or submitted_token
        if not token:
            raise ApiError("توکن ایتایار را وارد کنید.")

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.post(f"{_EITAA_BASE}/{token}/getMe")
            data = r.json()
        except (httpx.HTTPError, ValueError, AttributeError):
            raise ApiError("ارتباط با ایتا برقرار نشد. اتصال اینترنت را بررسی کنید.",
                           network=True)

        if (r.status_code != 200 or not isinstance(data, dict)
                or data.get("ok") is not True
                or not isinstance(data.get("result"), dict)):
            raise ApiError("اتصال به ایتا ناموفق بود.")

        if submitted_token and not existing_token:
            await self._store_verified("token", submitted_token)
        result = data.get("result") or {}
        bot_name = str(result.get("first_name")
                       or result.get("username")
                       or "ربات ایتا")
        conns = [{"id": 1, "name": body.get("name", "اتصال اصلی"),
                  "bot": bot_name, "verified": True}]
        await self._save("connections", conns)
        return conns[0]

    async def _add_channel(self, body: dict) -> dict:
        token = await self.storage.get("token") or config.EITAA_TOKEN
        if not token:
            raise ApiError("ابتدا اتصال ایتایار را اضافه و تأیید کنید.")
        connections = await self._load("connections", [])
        if not connections or not connections[0].get("verified"):
            await self._connect_eitaa({})

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
            raise ApiError("برای زمان‌بندی انتشار به اتصال سرور نیاز دارید.")

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

    async def _update_draft(self, post_id: int, body: dict) -> dict:
        text = (body.get("text") or "").strip()
        if not text:
            raise ApiError("متن پست نمی‌تواند خالی باشد.")
        mode = body.get("mode", "draft")
        if mode not in ("draft", "now", "schedule"):
            raise ApiError("حالت انتشار نامعتبر است.")
        if mode == "schedule":
            raise ApiError("برای زمان‌بندی انتشار به اتصال سرور نیاز دارید.")
        posts = await self._load("posts", [])
        post = next((p for p in posts if p.get("id") == post_id), None)
        if post is None:
            raise ApiError("پست پیدا نشد.")
        if post.get("status") != "draft":
            raise ApiError("فقط پیش‌نویس را می‌توان ویرایش کرد.")
        channel_ids = list(body.get("channel_ids") or [])
        if len(channel_ids) != len(set(channel_ids)):
            raise ApiError("کانال تکراری انتخاب شده است.")
        channels = await self._load("channels", [])
        known_channels = {c.get("id") for c in channels}
        if any(cid not in known_channels for cid in channel_ids):
            raise ApiError("یکی از کانال‌های انتخاب‌شده پیدا نشد.")
        if mode == "now" and not channel_ids:
            raise ApiError("حداقل یک کانال انتخاب کنید.")
        post.update({
            "title": (body.get("title") or "").strip(),
            "text": text,
            "silent": bool(body.get("silent")),
            "pin": bool(body.get("pin")),
            "scheduled_at": None,
            "deliveries": [
                {
                    "id": post_id * 1000 + i,
                    "channel_id": cid,
                    "status": "draft" if mode == "draft" else "queued",
                    "error": "",
                }
                for i, cid in enumerate(channel_ids)
            ],
        })
        if mode == "now":
            post["status"] = "queued"
            await self._publish(post)
        await self._save("posts", posts)
        return {"id": post_id, "status": post["status"]}

    async def _delete_draft(self, post_id: int) -> dict:
        posts = await self._load("posts", [])
        post = next((p for p in posts if p.get("id") == post_id), None)
        if post is None:
            raise ApiError("پست پیدا نشد.")
        if post.get("status") != "draft":
            raise ApiError("از این بخش فقط پیش‌نویس را می‌توان حذف کرد.")
        await self._save("posts", [p for p in posts if p.get("id") != post_id])
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
        provider = await self.storage.get("ai_provider") or "groq"
        if provider not in _AI_PROVIDERS:
            raise ApiError("ارائه‌دهنده هوش مصنوعی پشتیبانی نمی‌شود.")
        key_name = "groq_key" if provider == "groq" else f"ai_key_{provider}"
        key = await self.storage.get(key_name)
        if provider == "groq" and not key:
            key = config.GROQ_API_KEY
        if not key:
            raise ApiError("کلید API برای ارائه‌دهنده انتخاب‌شده ثبت نشده است.")

        action = body.get("action") or ""
        text = body.get("text") or ""
        prompt = body.get("prompt") or ""

        instruction = prompt.strip() or _AI_ACTIONS.get(action)
        if not instruction:
            raise ApiError("عملیات نامعتبر است.")
        if not (text.strip() or prompt.strip()):
            raise ApiError("لطفاً ابتدا متنی بنویسید.")

        user_msg = f"{instruction}\n\n{text}" if text else instruction
        endpoint = _GROQ_URL
        model = await self.storage.get("ai_model")
        if provider == "groq":
            model = model or config.GROQ_MODEL
        else:
            if provider == "openai":
                endpoint = await self.storage.get("ai_base_url") or _OPENAI_URL
            elif provider == "gemini":
                endpoint = _GEMINI_URL
            model = model or _AI_DEFAULT_MODELS[provider]
        self._require_https_endpoint(endpoint)

        if provider == "anthropic":
            out = await self._complete_anthropic(key, model, user_msg)
            return await self._record_ai_usage(action, text, out)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_AI) as c:
                r = await c.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
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
            if r.status_code in (401, 403):
                raise ApiError("کلید API نامعتبر است یا دسترسی لازم را ندارد.")
            if r.status_code == 429:
                raise ApiError("محدودیت درخواست سرویس AI؛ کمی بعد دوباره تلاش کنید.")
            raise ApiError("سرویس هوش مصنوعی پاسخگو نیست.")

        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ApiError("پاسخ خالی از سرویس هوش مصنوعی.")
            out = content.strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ApiError("پاسخ نامعتبر از سرویس هوش مصنوعی.")

        return await self._record_ai_usage(action, text, out)

    async def _complete_anthropic(self, key: str, model: str, user_msg: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_AI) as client:
                response = await client.post(
                    _ANTHROPIC_URL,
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 1500,
                        "system": _SYSTEM,
                        "messages": [{"role": "user", "content": user_msg}],
                    },
                )
                data = response.json()
        except (httpx.HTTPError, ValueError):
            raise ApiError("ارتباط با سرویس هوش مصنوعی برقرار نشد.",
                           network=True)
        if response.status_code in (401, 403):
            raise ApiError("کلید API نامعتبر است یا دسترسی لازم را ندارد.")
        if response.status_code == 429:
            raise ApiError("محدودیت درخواست سرویس AI؛ کمی بعد دوباره تلاش کنید.")
        if response.status_code != 200:
            raise ApiError("سرویس هوش مصنوعی پاسخگو نیست.")
        try:
            result = "".join(
                block["text"] for block in data["content"]
                if block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
        except (KeyError, TypeError, AttributeError):
            raise ApiError("پاسخ نامعتبر از سرویس هوش مصنوعی.")
        if not result:
            raise ApiError("پاسخ خالی از سرویس هوش مصنوعی.")
        return result

    async def _record_ai_usage(self, action: str, text: str, output: str) -> dict:
        usage = await self._load("ai_usage", [])
        usage.append({"at": _iso_now(), "action": action,
                      "chars_in": len(text), "chars_out": len(output)})
        await self._save("ai_usage", usage[-200:])
        return {"text": output}
