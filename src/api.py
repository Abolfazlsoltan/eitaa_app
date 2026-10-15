"""Eitaa Manager — data and AI client.

This module is the app's single point of contact with the outside world.
It handles three concerns transparently:

    1. Data — posts, channels, templates, activity log.
    2. AI — keyless public text service (or configured provider on server).
    3. Publishing — calls to EitaaYar.

When ``config.SERVER_URL`` is set, all requests go through the FastAPI
backend at that address. When it is empty, everything happens on the
device. The UI never needs to know which path is being used.

User-facing errors are always short Persian sentences. Technical details
(endpoints, status codes, exception types) are only written to the
module logger so they never leak into dialogs, snackbars or screens.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import logging
import mimetypes
import random
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from ai import AIServiceError, AnonymousAI
import config


logger = logging.getLogger(__name__)


# ================================================================== constants

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"
    "chat/completions"
)
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_EITAA_BASE = "https://eitaayar.ir/api"
_AI_PROVIDERS = ("groq", "openai", "gemini", "anthropic")
_GROQ_MODELS = (
    "llama-3.1-8b-instant",
    "allam-2-7b",
    "openai/gpt-oss-20b",
)
_AI_DEFAULT_MODELS = {
    "groq": "llama-3.1-8b-instant",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-6",
}

_TIMEOUT = 30.0
_TIMEOUT_AI = 90.0
_TIMEOUT_UPLOAD = 180.0
_TIMEOUT_MIN = 10.0


# ===================================================================== errors

class ApiError(Exception):
    """User-facing error. The message is always safe to display.

    Attributes:
        network:   True when the failure was caused by connectivity.
        retryable: True when a naive retry has a chance of succeeding.
        status:    Optional HTTP status code for internal diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        network: bool = False,
        retryable: bool = False,
        status: int | None = None,
    ):
        super().__init__(message)
        self.network = network
        self.retryable = retryable
        self.status = status


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
    "variants": "سه نسخه متفاوت و مستقل از متن زیر پیشنهاد بده و هر نسخه را شماره‌گذاری کن:",
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

_SAFE_ERRORS = {
    400: "درخواست پذیرفته نشد؛ مقادیر ورودی را بررسی کنید.",
    401: "ابتدا وارد شوید.",
    403: "دسترسی لازم برای این عملیات را ندارید.",
    404: "مورد درخواستی پیدا نشد.",
    408: "پاسخی از سرور دریافت نشد؛ دوباره تلاش کنید.",
    409: "این عملیات با وضعیت فعلی سرور سازگار نیست.",
    413: "حجم فایل بیش از حد مجاز است.",
    422: "اطلاعات وارد‌شده کامل یا معتبر نیست.",
    429: "تعداد درخواست‌ها زیاد بود؛ کمی بعد دوباره تلاش کنید.",
    500: "سرور موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید.",
    502: "ارتباط با سرویس‌های پشت‌صحنه برقرار نشد.",
    503: "سرویس در حال حاضر پاسخگو نیست؛ دوباره تلاش کنید.",
    504: "سرور پاسخ نداد؛ دوباره تلاش کنید.",
}


def normalize_channel(raw: str) -> str:
    """Normalize an Eitaa link, @username, or numeric channel ID safely."""
    raw = str(raw or "").strip()
    match = _CHANNEL_RE.match(raw)
    if match:
        return match.group(1)
    value = raw.lstrip("@")
    return value if re.fullmatch(r"[A-Za-z0-9_+-]{1,200}", value) else ""


def _utcnow_naive() -> dt.datetime:
    """Return UTC now as naive datetime for the existing local-storage schema."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso_now() -> str:
    return _utcnow_naive().isoformat(timespec="seconds")


def _mask_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


# ====================================================================== Api

class Api:
    """Unified data + AI client."""

    def __init__(self, storage):
        self.storage = storage
        self.base = (config.SERVER_URL or "").rstrip("/")
        self.token: str | None = None
        self.on_unauthorized = lambda: None

        self._client: httpx.AsyncClient | None = None
        self._ai_client = AnonymousAI()
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

    async def patch(self, path: str, body: Any = None) -> Any:
        return await self._dispatch("PATCH", path, body)

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

    # ------------------------------------------------------------- AI ------
    async def ai(
        self, action: str, text: str, prompt: str = "",
        language: str = "fa", tone: str = "neutral", length: str = "medium",
    ) -> str:
        # Validate and bound data before it leaves the device.
        action = action.strip() if isinstance(action, str) else ""
        text = text.strip() if isinstance(text, str) else ""
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        language = language.strip() if isinstance(language, str) else "fa"
        tone = tone.strip() if isinstance(tone, str) else "neutral"
        length = length.strip() if isinstance(length, str) else "medium"

        if action not in _AI_ACTIONS:
            raise ApiError("نوع درخواست دستیار هوشمند معتبر نیست.")
        if not text and not prompt:
            raise ApiError("ابتدا متن یا توضیح درخواست را وارد کنید.")
        if len(text) + len(prompt) > 20_000:
            raise ApiError("متن درخواست خیلی طولانی است؛ آن را کوتاه‌تر کنید.")
        if language not in ("fa", "en", "ar"):
            raise ApiError("زبان خروجی پشتیبانی نمی‌شود.")
        if tone not in ("neutral", "formal", "friendly", "news"):
            raise ApiError("لحن انتخاب‌شده معتبر نیست.")
        if length not in ("short", "medium", "long"):
            raise ApiError("اندازهٔ پاسخ انتخاب‌شده معتبر نیست.")

        result = await self._anonymous_ai(
            {
                "action": action,
                "text": text,
                "prompt": prompt,
                "language": language,
                "tone": tone,
                "length": length,
            }
        )
        output = result.get("text") if isinstance(result, dict) else None
        if not isinstance(output, str) or not output.strip():
            raise ApiError("دستیار هوشمند پاسخ معتبری برنگرداند.")
        return output.strip()

    async def ai_settings(self) -> dict:
        provider = await self.storage.get("ai_provider") or "groq"
        if provider not in _AI_PROVIDERS:
            provider = "groq"
        model = await self.storage.get("ai_model")
        has_key = await self.ai_key_configured(provider)
        default_model = _AI_DEFAULT_MODELS[provider]
        return {
            "provider": provider,
            "model": default_model if provider == "groq"
                     else model or default_model,
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
        if provider != "groq" and not model.strip():
            raise ApiError("نام مدل را وارد کنید.")
        if provider == "openai":
            self._require_https_endpoint(base_url, allow_empty=True)
        key_name = "groq_key" if provider == "groq" else f"ai_key_{provider}"
        new_key = key.strip()
        existing_key = await self.storage.get(key_name)
        if provider == "groq" and not existing_key:
            existing_key = config.GROQ_API_KEY
        if new_key and existing_key and existing_key != new_key:
            raise ApiError(
                "کلید از قبل تنظیم شده و برای حفظ مقدار موجود از این صفحه "
                "قابل جایگزینی نیست."
            )
        await self._store_verified("ai_provider", provider)
        if provider != "groq":
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
            raise ApiError(
                "ذخیره تنظیمات ناموفق بود؛ فضای ذخیره‌سازی را بررسی کنید."
            )

    # ---------------------------------------------------------- uploads ----
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

    # ----------------------------------------------------- latest release --
    async def latest_release(self) -> dict:
        url = (
            "https://api.github.com/repos/"
            "Abolfazlsoltan/eitaa_app/releases/latest"
        )
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True
            ) as client:
                response = await client.get(
                    url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Latest-release fetch failed: %s", exc)
            raise ApiError(
                "بررسی نسخه جدید انجام نشد؛ اتصال اینترنت را بررسی کنید.",
                network=True, retryable=True,
            )
        if response.status_code != 200 or not isinstance(data, dict):
            logger.warning(
                "Latest-release returned status=%s", response.status_code
            )
            raise ApiError("اطلاعات نسخه جدید در دسترس نیست.")
        if data.get("draft") or data.get("prerelease"):
            raise ApiError("نسخه پایدار در دسترس نیست.")
        page_url = data.get("html_url")
        if not self._is_release_url(page_url):
            raise ApiError("پیوند انتشار دریافتی معتبر نیست.")
        apk_url = next((
            asset.get("browser_download_url")
            for asset in data.get("assets", [])
            if isinstance(asset, dict)
            and str(asset.get("name", "")).lower().endswith(".apk")
            and self._is_release_url(asset.get("browser_download_url"))
        ), "")
        return {
            "tag": str(data.get("tag_name") or ""),
            "name": str(data.get("name") or data.get("tag_name") or ""),
            "notes": str(data.get("body") or "")[:12000],
            "url": page_url,
            "apk_url": apk_url,
        }

    @staticmethod
    def _is_release_url(url: Any) -> bool:
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname == "github.com"

    # ---------------------------------------------------------- exports ----
    async def export_history(
        self, export_format: str, *, query: str = "", status: str = "all",
        channel_id: int | None = None,
    ) -> tuple[str, bytes]:
        if export_format not in ("json", "csv"):
            raise ApiError("قالب خروجی پشتیبانی نمی‌شود.")
        params: dict[str, Any] = {
            "q": query,
            "status": status,
            "limit": 1000,
            "offset": 0,
        }
        if channel_id is not None:
            params["channel_id"] = channel_id
        records = await self.get("/history", params=params)
        if not isinstance(records, list):
            raise ApiError("پاسخ تاریخچه برای خروجی معتبر نیست.")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        if export_format == "json":
            content = json.dumps(
                records, ensure_ascii=False, indent=2, default=str
            ).encode("utf-8")
            return f"eitaa-history-{stamp}.json", content

        channels = await self.get("/channels")
        channel_names = {
            channel.get("id"): channel.get("title", "")
            for channel in channels if isinstance(channel, dict)
        }
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow([
            "post_id", "title", "text", "post_status", "channel",
            "channel_id", "delivery_status", "message_id", "published_at",
            "created_at",
        ])
        for post in records:
            for delivery in post.get("deliveries", []):
                values = [
                    post.get("id"),
                    post.get("title", ""),
                    post.get("text", ""),
                    post.get("status", ""),
                    channel_names.get(delivery.get("channel_id"), ""),
                    delivery.get("channel_id", ""),
                    delivery.get("status", ""),
                    delivery.get("remote_message_id") or "",
                    delivery.get("done_at") or "",
                    post.get("created_at") or "",
                ]
                writer.writerow([
                    "'" + value if isinstance(value, str)
                    and value.startswith(("=", "+", "-", "@"))
                    else value
                    for value in values
                ])
        return f"eitaa-history-{stamp}.csv", stream.getvalue().encode("utf-8-sig")

    # ------------------------------------------------------- local profile --
    # Profile fields are stored only on the device. They never leave it.

    async def get_profile(self) -> dict:
        raw = await self.storage.get("data:local_profile")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        if "display_name" not in data and isinstance(data.get("name"), str):
            data["display_name"] = data["name"]
        return data

    async def has_profile(self) -> bool:
        return bool(await self.get_profile())

    async def save_profile(
        self,
        name: str | dict = "",
        username: str = "",
        avatar_b64: str = "",
        avatar_mime: str = "",
    ) -> dict:
        # Accept both the old positional call and the profile dialog's dict.
        payload = name if isinstance(name, dict) else {}
        display_name = (
            payload.get("display_name") or payload.get("name") or ""
            if payload else name
        )
        if payload:
            username = payload.get("username", username)
            avatar_b64 = payload.get("avatar_b64", avatar_b64)
            avatar_mime = payload.get("avatar_mime", avatar_mime)
        current = await self.get_profile()
        display_name = str(display_name or "").strip()
        profile = {
            "display_name": display_name,
            "name": display_name,
            "username": str(username or "").strip(),
            "avatar_b64": avatar_b64 or current.get("avatar_b64", ""),
            "avatar_mime": avatar_mime or current.get("avatar_mime", ""),
            "updated_at": _iso_now(),
        }
        await self._store_verified(
            "data:local_profile",
            json.dumps(profile, ensure_ascii=False),
        )
        return profile

    async def clear_profile(self) -> None:
        await self.storage.remove("data:local_profile")

    # ------------------------------------------------- notifications ------
    # The registry itself lives in ``config.NOTIFICATION_REGISTRY``.
    # Here we only persist per-notification viewing state.

    async def list_notifications(self) -> list[dict]:
        registry = getattr(config, "NOTIFICATION_REGISTRY", None) or []
        return [item for item in registry if isinstance(item, dict)]

    async def notification_state(self) -> dict:
        raw = await self.storage.get("data:notifications_state")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def _save_notification_state(self, state: dict) -> None:
        await self.storage.set(
            "data:notifications_state",
            json.dumps(state, ensure_ascii=False),
        )

    async def record_notification_shown(self, notification_id: str) -> None:
        if not notification_id:
            return
        state = await self.notification_state()
        entry = state.get(notification_id) or {}
        entry["shown"] = int(entry.get("shown", 0)) + 1
        entry["last_shown"] = _iso_now()
        state[notification_id] = entry
        await self._save_notification_state(state)

    async def dismiss_notification(self, notification_id: str) -> None:
        if not notification_id:
            return
        state = await self.notification_state()
        entry = state.get(notification_id) or {}
        entry["dismissed"] = True
        entry["dismissed_at"] = _iso_now()
        state[notification_id] = entry
        await self._save_notification_state(state)

    async def snooze_notification(
        self, notification_id: str, days: int = 3
    ) -> None:
        if not notification_id:
            return
        until = (
            _utcnow_naive() + dt.timedelta(days=max(1, int(days)))
        ).isoformat(timespec="seconds")
        state = await self.notification_state()
        entry = state.get(notification_id) or {}
        entry["snoozed_until"] = until
        state[notification_id] = entry
        await self._save_notification_state(state)

    async def reset_notifications(self) -> None:
        await self.storage.remove("data:notifications_state")

    async def notification_decision(
        self, notification: dict, now: dt.datetime | None = None
    ) -> str:
        """Return 'show', 'skip' or 'expired' for the given notification.

        Decision rules (all are per-device):
            * disabled / active == False         → skip
            * expires_at in the past             → expired
            * dismissed by the user              → skip
            * snoozed_until in the future        → skip
            * shown >= max_shows                 → skip
            * cooldown_hours since last_shown    → skip
            * otherwise                          → show
        """
        if not isinstance(notification, dict):
            return "skip"
        if notification.get("active") is False:
            return "skip"

        now = now or _utcnow_naive()
        expires = notification.get("expires_at")
        if isinstance(expires, str) and expires.strip():
            try:
                if dt.datetime.fromisoformat(expires) <= now:
                    return "expired"
            except ValueError:
                pass

        state = (await self.notification_state()).get(
            notification.get("id") or "", {}
        ) or {}

        if state.get("dismissed"):
            return "skip"

        snoozed_until = state.get("snoozed_until")
        if isinstance(snoozed_until, str) and snoozed_until:
            try:
                if dt.datetime.fromisoformat(snoozed_until) > now:
                    return "skip"
            except ValueError:
                pass

        max_shows = notification.get("max_shows")
        if isinstance(max_shows, int) and max_shows > 0:
            if int(state.get("shown", 0)) >= max_shows:
                return "skip"

        cooldown = notification.get("cooldown_hours")
        last_shown = state.get("last_shown")
        if isinstance(cooldown, (int, float)) and cooldown > 0 \
                and isinstance(last_shown, str) and last_shown:
            try:
                delta = now - dt.datetime.fromisoformat(last_shown)
                if delta.total_seconds() < cooldown * 3600:
                    return "skip"
            except ValueError:
                pass

        return "show"

    # -------------------------------------------------------- dispatcher ---
    async def _dispatch(self, method, path, body, *, params=None,
                        files=None, data=None, timeout=None):
        if self.online:
            return await self._http_with_retry(
                method, path, body, params, files, data, timeout,
            )
        return await self._device(method, path, body, params=params)

    # -------------------------------------------------------- HTTP path ----
    async def _http_with_retry(self, method, path, body, params, files,
                               data, timeout):
        """Wrap ``_http`` with a bounded retry for transient failures.

        Only idempotent requests (GET / HEAD) or requests the backend treats
        as idempotent (paths covered by an idempotency key) are retried.
        Everything else is attempted once — matches the previous behavior.
        """
        retry_safe = method in ("GET", "HEAD")
        max_attempts = 2 if retry_safe else 1
        last_error: ApiError | None = None

        for attempt in range(max_attempts):
            try:
                return await self._http(
                    method, path, body, params, files, data, timeout,
                )
            except ApiError as exc:
                last_error = exc
                if not (exc.retryable and retry_safe):
                    raise
                if attempt >= max_attempts - 1:
                    raise
                delay = 0.4 * (attempt + 1)
                logger.info(
                    "Retrying %s %s after %.1fs (%s)",
                    method, path, delay, exc,
                )
                await asyncio.sleep(delay)
        if last_error:
            raise last_error
        raise ApiError("درخواست انجام نشد؛ دوباره تلاش کنید.")

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_TIMEOUT, connect=_TIMEOUT_MIN),
                limits=httpx.Limits(
                    max_connections=10, max_keepalive_connections=5,
                ),
            )
        return self._client

    async def _http(self, method, path, body, params, files, data, timeout):
        client = self._client_or_create()

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        url = f"{self.base}/api{path}"

        try:
            r = await client.request(
                method, url,
                json=body, params=params, files=files, data=data,
                headers=headers, timeout=timeout or _TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            logger.warning("HTTP timeout on %s %s: %s", method, url, exc)
            raise ApiError(
                "سرور پاسخ نداد؛ اتصال اینترنت را بررسی و دوباره تلاش کنید.",
                network=True, retryable=True,
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("HTTP error on %s %s: %s", method, url, exc)
            raise ApiError(
                "ارتباط با سرور برقرار نشد. لطفاً دوباره تلاش کنید.",
                network=True, retryable=True,
            )

        try:
            payload = r.json() if r.content else {}
        except ValueError:
            payload = {}

        if r.status_code == 401 and self.token:
            self.on_unauthorized()

        if r.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            logger.info(
                "API %s %s returned %s (detail=%r)",
                method, path, r.status_code, detail,
            )
            message = self._translate_http_error(r.status_code, detail)
            raise ApiError(
                message,
                network=r.status_code in (502, 503, 504),
                retryable=r.status_code >= 500 or r.status_code == 429,
                status=r.status_code,
            )

        return payload

    @staticmethod
    def _translate_http_error(status: int, detail: Any) -> str:
        """Map an HTTP status / server detail to a friendly Persian message.

        The server's own Persian ``detail`` is preferred when it looks like a
        short sentence, otherwise a generic fallback is used so callers never
        see stack traces, JSON blobs or English error strings.
        """
        if isinstance(detail, str):
            clean = detail.strip()
            if (
                1 <= len(clean) <= 220
                and not re.search(r"[{}<>]|Traceback|Error:|Exception",
                                  clean)
            ):
                return clean
        return _SAFE_ERRORS.get(
            status, "خطایی رخ داد؛ دوباره تلاش کنید."
        )

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

    async def _device(
        self, method: str, path: str, body: Any, *, params: dict | None = None
    ) -> Any:
        path = path.split("?", 1)[0]

        # ---- auth ----
        if path in ("/auth/state", "/auth/me"):
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

        if path == "/history":
            return await self._device_history(params or {})

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

    async def _device_history(self, params: dict) -> list[dict]:
        posts = await self._load("posts", [])
        channels = await self._load("channels", [])
        names = {
            channel.get("id"): channel
            for channel in channels if isinstance(channel, dict)
        }
        query = str(params.get("q") or "").strip().casefold()
        status = str(params.get("status") or "published")
        channel_id = params.get("channel_id")
        try:
            channel_id = int(channel_id) if channel_id is not None else None
            limit = max(1, min(int(params.get("limit", 200)), 1000))
            offset = max(0, int(params.get("offset", 0)))
        except (TypeError, ValueError):
            raise ApiError("پارامترهای تاریخچه معتبر نیستند.")

        matches = []
        for post in posts:
            deliveries = [
                delivery for delivery in post.get("deliveries", [])
                if (status == "all" or delivery.get("status") == status)
                and (channel_id is None
                     or delivery.get("channel_id") == channel_id)
            ]
            if not deliveries:
                continue
            searchable = " ".join([
                str(post.get("title") or ""),
                str(post.get("text") or ""),
                *[
                    str(names.get(delivery.get("channel_id"), {}).get(key) or "")
                    for delivery in deliveries
                    for key in ("title", "chat_id")
                ],
            ]).casefold()
            if query and query not in searchable:
                continue
            item = dict(post)
            item["deliveries"] = deliveries
            matches.append(item)
        return matches[offset:offset + limit]

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
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("getMe request failed: %s", type(exc).__name__)
            raise ApiError(
                "ارتباط با ایتا برقرار نشد. اتصال اینترنت را بررسی کنید.",
                network=True, retryable=True,
            )

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
        view_threshold = body.get("view_to_delete")
        reply_to_message_id = body.get("reply_to_message_id")
        if view_threshold is not None and (
            not isinstance(view_threshold, int) or view_threshold < 1
        ):
            raise ApiError("آستانه بازدید باید عدد صحیح مثبت باشد.")
        if reply_to_message_id is not None and (
            not isinstance(reply_to_message_id, int) or reply_to_message_id < 1
        ):
            raise ApiError("شناسه پیام باید عدد صحیح مثبت باشد.")

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
            "signature": (body.get("signature") or "").strip(),
            "status": "draft" if mode == "draft" else "queued",
            "scheduled_at": None,
            "created_at": _iso_now(),
            "updated_at": _iso_now(),
            "silent": bool(body.get("silent")),
            "pin": bool(body.get("pin")),
            "view_to_delete": body.get("view_to_delete"),
            "reply_to_message_id": body.get("reply_to_message_id"),
            "revisions": [],
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

                message_text = post["text"].strip()
                signature = (post.get("signature") or "").strip()
                if signature:
                    message_text = f"{message_text}\n\n{signature}"
                payload = {"chat_id": ch["chat_id"], "text": message_text}
                if post["title"]:
                    payload["title"] = post["title"]
                if post["silent"]:
                    payload["disable_notification"] = 1
                if post["pin"]:
                    payload["pin"] = 1
                if post.get("view_to_delete") is not None:
                    payload["view_to_delete"] = post["view_to_delete"]
                if post.get("reply_to_message_id") is not None:
                    payload["reply_to_message_id"] = post["reply_to_message_id"]

                try:
                    r = await c.post(f"{_EITAA_BASE}/{token}/sendMessage",
                                     data=payload)
                    j = r.json()
                except (httpx.HTTPError, ValueError):
                    d["status"], d["error"] = "failed", "خطای ارتباط"
                    continue

                if (r.status_code == 200 and isinstance(j, dict)
                        and j.get("ok") is True):
                    d["status"] = "published"
                    d["done_at"] = _iso_now()
                    result = j.get("result")
                    message_id = (
                        result.get("message_id")
                        if isinstance(result, dict) else None
                    )
                    d["remote_message_id"] = (
                        message_id
                        if isinstance(message_id, int)
                        and not isinstance(message_id, bool)
                        and message_id > 0
                        else None
                    )
                    success += 1
                else:
                    d["status"] = "failed"
                    if isinstance(j, dict):
                        error = str(
                            j.get("description")
                            or j.get("message")
                            or "خطای نامشخص"
                        )
                    else:
                        error = "پاسخ نامعتبر از ایتا"
                    d["error"] = _mask_secret(error, token)

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
        revisions = post.setdefault("revisions", [])
        if any(
            post.get(key, "") != value
            for key, value in (
                ("title", (body.get("title") or "").strip()),
                ("text", text),
                ("signature", (body.get("signature") or "").strip()),
            )
        ):
            revisions.insert(0, {
                "title": post.get("title", ""),
                "text": post.get("text", ""),
                "signature": post.get("signature", ""),
                "created_at": _iso_now(),
            })
        post.update({
            "title": (body.get("title") or "").strip(),
            "text": text,
            "signature": (body.get("signature") or "").strip(),
            "updated_at": _iso_now(),
            "silent": bool(body.get("silent")),
            "pin": bool(body.get("pin")),
            "view_to_delete": body.get("view_to_delete"),
            "reply_to_message_id": body.get("reply_to_message_id"),
            "media_id": body.get("media_id"),
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
            "channels": len(await self._load("channels", [])),
            "drafts": count("draft"),
        }

    # ------------------------------------------------------------ AI -------
    async def _anonymous_ai(self, body: dict) -> dict:
        action = body.get("action") or ""
        text = body.get("text") or ""
        prompt = body.get("prompt") or ""
        if not isinstance(text, str) or not isinstance(prompt, str):
            raise ApiError("درخواست متن معتبر نیست.")

        instruction = prompt.strip() or _AI_ACTIONS.get(action)
        if not instruction:
            raise ApiError("عملیات نامعتبر است.")
        if not (text.strip() or prompt.strip()):
            raise ApiError("لطفاً ابتدا متنی بنویسید.")

        language = body.get("language", "fa")
        tone = body.get("tone", "neutral")
        length = body.get("length", "medium")
        if language not in ("fa", "en", "ar"):
            raise ApiError("زبان خروجی انتخاب‌شده معتبر نیست.")
        if tone not in ("neutral", "formal", "friendly", "news"):
            raise ApiError("لحن انتخاب‌شده معتبر نیست.")
        if length not in ("short", "medium", "long"):
            raise ApiError("اندازه خروجی انتخاب‌شده معتبر نیست.")

        guidance = {"fa": "فارسی", "en": "انگلیسی", "ar": "عربی"}[language]
        tone_guidance = {
            "neutral": "متعادل",
            "formal": "رسمی",
            "friendly": "دوستانه",
            "news": "خبری",
        }[tone]
        length_guidance = {
            "short": "کوتاه",
            "medium": "متوسط",
            "long": "مفصل",
        }[length]
        preferences = (
            f"زبان خروجی: {guidance}. لحن: {tone_guidance}. "
            f"طول: {length_guidance}."
        )
        user_msg = (
            f"{instruction}\n\n{preferences}"
            + (f"\n\n{text}" if text else "")
        )
        try:
            output = await self._ai_client.complete(_SYSTEM, user_msg)
        except AIServiceError as exc:
            raise ApiError(str(exc), network=True, retryable=True) from exc
        return await self._record_ai_usage(action, text, output)

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

        language = body.get("language", "fa")
        tone = body.get("tone", "neutral")
        length = body.get("length", "medium")
        if language not in ("fa", "en", "ar"):
            raise ApiError("زبان خروجی انتخاب‌شده معتبر نیست.")
        if tone not in ("neutral", "formal", "friendly", "news"):
            raise ApiError("لحن انتخاب‌شده معتبر نیست.")
        if length not in ("short", "medium", "long"):
            raise ApiError("اندازه خروجی انتخاب‌شده معتبر نیست.")
        guidance = {
            "fa": "فارسی",
            "en": "انگلیسی",
            "ar": "عربی",
        }[language]
        tone_guidance = {
            "neutral": "متعادل",
            "formal": "رسمی",
            "friendly": "دوستانه",
            "news": "خبری",
        }[tone]
        length_guidance = {
            "short": "کوتاه",
            "medium": "متوسط",
            "long": "مفصل",
        }[length]
        preferences = (
            f"زبان خروجی: {guidance}. لحن: {tone_guidance}. "
            f"طول: {length_guidance}."
        )
        user_msg = (
            f"{instruction}\n\n{preferences}"
            + (f"\n\n{text}" if text else "")
        )
        endpoint = _GROQ_URL
        model = await self.storage.get("ai_model")
        if provider == "groq":
            models = list(_GROQ_MODELS)
            random.shuffle(models)
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

        candidates = models if provider == "groq" else [model]
        failures: list[ApiError] = []
        for candidate in candidates:
            try:
                out = await self._complete_openai_compatible(
                    endpoint, key, candidate, user_msg
                )
                return await self._record_ai_usage(action, text, out)
            except ApiError as exc:
                failures.append(exc)
        if provider == "groq":
            raise ApiError(
                "هیچ‌یک از مدل‌های هوش مصنوعی در دسترس نبودند. "
                "اتصال، کلید API و دسترسی حساب را بررسی کنید.",
                network=all(error.network for error in failures),
                retryable=True,
            ) from failures[-1]
        raise failures[-1]

    async def _complete_openai_compatible(
        self, endpoint: str, key: str, model: str, user_msg: str
    ) -> str:
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
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ApiError(
                "ارتباط با سرویس هوش مصنوعی برقرار نشد.",
                network=True, retryable=True,
            ) from exc

        if r.status_code != 200:
            if r.status_code in (401, 403):
                raise ApiError("کلید API نامعتبر است یا دسترسی لازم را ندارد.")
            if r.status_code == 429:
                raise ApiError(
                    "محدودیت درخواست سرویس AI؛ کمی بعد دوباره تلاش کنید.",
                    retryable=True, status=429,
                )
            if r.status_code == 404:
                raise ApiError("مدل انتخاب‌شده در سرویس پیدا نشد.")
            raise ApiError("سرویس هوش مصنوعی پاسخگو نیست.", retryable=True)

        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ApiError("پاسخ خالی از سرویس هوش مصنوعی.")
            return content.strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ApiError("پاسخ نامعتبر از سرویس هوش مصنوعی.") from exc

    async def _complete_anthropic(
        self, key: str, model: str, user_msg: str
    ) -> str:
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
        except (httpx.HTTPError, ValueError) as exc:
            raise ApiError(
                "ارتباط با سرویس هوش مصنوعی برقرار نشد.",
                network=True, retryable=True,
            ) from exc
        if response.status_code in (401, 403):
            raise ApiError("کلید API نامعتبر است یا دسترسی لازم را ندارد.")
        if response.status_code == 429:
            raise ApiError(
                "محدودیت درخواست سرویس AI؛ کمی بعد دوباره تلاش کنید.",
                retryable=True, status=429,
            )
        if response.status_code != 200:
            raise ApiError("سرویس هوش مصنوعی پاسخگو نیست.", retryable=True)
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

    async def _record_ai_usage(
        self, action: str, text: str, output: str
    ) -> dict:
        usage = await self._load("ai_usage", [])
        usage.append({"at": _iso_now(), "action": action,
                      "chars_in": len(text), "chars_out": len(output)})
        await self._save("ai_usage", usage[-200:])
        return {"text": output}
