"""AI provider abstraction for the Eitaa Channel Manager backend.

Add a new provider by subclassing `AIProvider`, implementing `complete()`,
and registering it in `PROVIDERS`. The active provider is chosen at
runtime via the `AI_PROVIDER` environment variable (default: `"groq"`).

Currently shipped providers
---------------------------
    * GroqProvider   — Groq's OpenAI-compatible endpoint. Default.
                       Models: llama-3.3-70b-versatile, llama-3.1-8b-instant,
                       mixtral-8x7b-32768, gemma2-9b-it, …
                       Docs:   https://console.groq.com/docs/openai
    * AnthropicProvider — kept for teams that prefer Claude.

Environment variables
---------------------
    AI_API_KEY   (required)  — provider API key.
    AI_PROVIDER  (optional)  — "groq" (default) or "anthropic".
    AI_MODEL     (optional)  — model name. Sensible default per provider.

Errors
------
Provider failures raise `AIError` with a Persian message that is safe to
show in the UI. The endpoint layer converts it to HTTP 502.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

import httpx


# ============================================================== exceptions ==

class AIError(Exception):
    """Raised when an AI provider call fails. Message is user-facing."""


# ================================================================ providers ==

class AIProvider(ABC):
    """Base class for AI providers."""

    name: str = "base"
    default_model: str = ""

    def __init__(self) -> None:
        self.api_key = (os.getenv("AI_API_KEY") or "").strip()
        self.model = (os.getenv("AI_MODEL") or self.default_model).strip()
        if not self.api_key:
            raise AIError(
                "کلید سرویس هوش مصنوعی تنظیم نشده است (AI_API_KEY)."
            )
        if not self.model:
            raise AIError(
                "مدل هوش مصنوعی مشخص نشده است (AI_MODEL)."
            )

    @abstractmethod
    async def complete(self, system: str, user: str) -> str:
        """Return the model's plain-text answer for `system` + `user`."""


# --------------------------------------------------------------------- Groq --

class GroqProvider(AIProvider):
    """Groq — OpenAI-compatible chat completions API.

    Groq is used both by the backend (this class) and directly by the client
    in local mode (`src/local.py`). Keep the two implementations in sync.
    """

    name = "groq"
    default_model = "llama-3.3-70b-versatile"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    async def complete(self, system: str, user: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": 1500,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(self.endpoint, headers=headers, json=payload)
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise AIError("ارتباط با سرویس هوش مصنوعی برقرار نشد.")

        if r.status_code != 200:
            detail = ""
            try:
                detail = (data.get("error") or {}).get("message") or ""
            except AttributeError:
                pass
            msg = "سرویس هوش مصنوعی خطا داد."
            if detail:
                msg += f" ({detail})"
            raise AIError(msg)

        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise AIError("پاسخ سرویس هوش مصنوعی نامعتبر بود.")


# ---------------------------------------------------------------- Anthropic --

class AnthropicProvider(AIProvider):
    """Anthropic Claude — Messages API.

    Kept for teams that prefer Claude. Set:
        AI_PROVIDER=anthropic
        AI_MODEL=claude-sonnet-4-6
    """

    name = "anthropic"
    default_model = "claude-sonnet-4-6"
    endpoint = "https://api.anthropic.com/v1/messages"
    api_version = "2023-06-01"

    async def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": 1500,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post(self.endpoint, headers=headers, json=payload)
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise AIError("ارتباط با سرویس هوش مصنوعی برقرار نشد.")

        if r.status_code != 200:
            detail = ""
            try:
                detail = (data.get("error") or {}).get("message") or ""
            except AttributeError:
                pass
            msg = "سرویس هوش مصنوعی خطا داد."
            if detail:
                msg += f" ({detail})"
            raise AIError(msg)

        try:
            return "".join(
                block.get("text", "") for block in data.get("content", [])
            ).strip()
        except AttributeError:
            raise AIError("پاسخ سرویس هوش مصنوعی نامعتبر بود.")


# ================================================================== registry =

PROVIDERS: dict[str, type[AIProvider]] = {
    "groq": GroqProvider,
    "anthropic": AnthropicProvider,
}


def get_provider() -> AIProvider:
    """Return the provider selected by `AI_PROVIDER` (default: groq)."""
    name = (os.getenv("AI_PROVIDER") or "groq").strip().lower()
    cls = PROVIDERS.get(name)
    if cls is None:
        raise AIError(
            f'ارائه‌دهنده هوش مصنوعی «{name}» پشتیبانی نمی‌شود. '
            f'گزینه‌های موجود: {", ".join(sorted(PROVIDERS))}.'
        )
    return cls()


# ============================================================== prompt table =

SYSTEM = (
    "تو دستیار تولید محتوا برای کانال‌های ایتا هستی. "
    "فقط متن نهایی را به فارسی روان بنویس، بدون توضیح اضافه."
)

ACTIONS: dict[str, str] = {
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
