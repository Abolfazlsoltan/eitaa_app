"""AI provider for the Eitaa Manager backend.

Two providers are supported and can be selected at deployment time:

    * groq       — default; fast, cheap, OpenAI-compatible.
    * anthropic  — Claude models, for teams that prefer them.

Configuration (environment variables):
    AI_API_KEY   (required)  provider key
    AI_PROVIDER  (optional)  "groq" (default) or "anthropic"
    AI_MODEL     (optional)  model name; sensible default per provider
"""

from __future__ import annotations

import os
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx


class AIError(Exception):
    """Raised when an AI provider call fails. The message is user-facing."""


# ================================================================ providers

class AIProvider(ABC):
    name = "base"
    default_model = ""

    def __init__(self) -> None:
        self.api_key = (os.getenv("AI_API_KEY") or "").strip()
        self.model = (os.getenv("AI_MODEL") or self.default_model).strip()
        if not self.api_key:
            raise AIError("سرویس هوش مصنوعی در دسترس نیست.")
        if not self.model:
            raise AIError("مدل هوش مصنوعی انتخاب نشده است.")

    @abstractmethod
    async def complete(self, system: str, user: str) -> str:
        """Return the model's plain-text answer for `system` + `user`."""


class GroqProvider(AIProvider):
    name = "groq"
    default_model = "llama-3.1-8b-instant"
    models = (
        "llama-3.1-8b-instant",
        "allam-2-7b",
        "openai/gpt-oss-20b",
    )
    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    async def complete(self, system: str, user: str) -> str:
        candidates = list(self.models)
        random.shuffle(candidates)
        failures = []
        for model in candidates:
            try:
                return await self._complete_model(model, system, user)
            except AIError as exc:
                failures.append(exc)
        raise AIError(
            "هیچ‌یک از مدل‌های هوش مصنوعی در دسترس نبودند. "
            "اتصال، کلید API و دسترسی حساب را بررسی کنید."
        ) from failures[-1]

    async def _complete_model(
        self, model: str, system: str, user: str
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
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
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise AIError(
                "ارتباط با سرویس هوش مصنوعی برقرار نشد."
            ) from exc

        if r.status_code != 200 or not isinstance(data, dict):
            if r.status_code in (401, 403):
                raise AIError("کلید API یا دسترسی سرویس هوش مصنوعی معتبر نیست.")
            if r.status_code == 429:
                raise AIError("محدودیت درخواست سرویس هوش مصنوعی.")
            if r.status_code == 404:
                raise AIError("مدل انتخاب‌شده در سرویس پیدا نشد.")
            raise AIError("سرویس هوش مصنوعی پاسخگو نیست.")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise AIError(
                "پاسخ نامعتبر از سرویس هوش مصنوعی."
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise AIError("پاسخ خالی از سرویس هوش مصنوعی.")
        return content.strip()


class AnthropicProvider(AIProvider):
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
            raise AIError("سرویس هوش مصنوعی پاسخگو نیست.")

        try:
            return "".join(
                block.get("text", "") for block in data.get("content", [])
            ).strip()
        except AttributeError:
            raise AIError("پاسخ نامعتبر از سرویس هوش مصنوعی.")


# ================================================================== registry

PROVIDERS: dict[str, type[AIProvider]] = {
    "groq": GroqProvider,
    "anthropic": AnthropicProvider,
}


def get_provider() -> AIProvider:
    name = (os.getenv("AI_PROVIDER") or "groq").strip().lower()
    cls = PROVIDERS.get(name)
    if cls is None:
        raise AIError("سرویس هوش مصنوعی انتخابی پشتیبانی نمی‌شود.")
    return cls()


# ============================================================== prompt table

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
                           "variants": "سه نسخه متفاوت و مستقل از متن زیر پیشنهاد بده و هر نسخه را شماره‌گذاری کن:",
                       }
