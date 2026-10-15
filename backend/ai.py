"""AI provider for the Eitaa Manager backend.

Two providers are supported and can be selected at deployment time:

    * groq       — default; fast, cheap, OpenAI-compatible.
    * anthropic  — Claude models, for teams that prefer them.

Configuration (environment variables):
    AI_API_KEY   (required)  provider key
    AI_PROVIDER  (optional)  "groq" (default) or "anthropic"
    AI_MODEL     (optional)  model name; sensible default per provider

Optional overrides (used mainly by tests and self-hosted mirrors):
    AI_GROQ_ENDPOINT       override the Groq chat-completions URL
    AI_ANTHROPIC_ENDPOINT  override the Anthropic messages URL

Every user-visible error is a short Persian sentence produced by
:class:`AIError`. Provider responses, HTTP status codes and the underlying
exception are only written to the module logger, so nothing technical can
leak into an HTTP response returned to the client.
"""

from __future__ import annotations

import logging
import os
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


class AIError(Exception):
    """Raised when an AI provider call fails. The message is user-facing."""


# ================================================================== helpers

_TECHNICAL_MARKERS = (
    "traceback", "file \"", "exception", "keyerror", "valueerror",
    "typeerror", "attributeerror", "https://", "http://", "/api/",
    "error:", "unauthorized", "forbidden",
)


def _clean_error(message: str, fallback: str) -> str:
    """Return a short, user-safe Persian message.

    Provider responses can be long, contain English error text or URL
    fragments. This function replaces anything suspicious with ``fallback``
    so callers never have to think about what reaches the client.
    """
    if not isinstance(message, str):
        return fallback
    clean = message.strip()
    if not clean:
        return fallback
    if len(clean) > 220:
        return fallback
    lowered = clean.lower()
    if any(marker in lowered for marker in _TECHNICAL_MARKERS):
        return fallback
    return clean


# ================================================================ providers

def _validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost", "127.0.0.1", "::1",
    }
    if (not parsed.hostname or (parsed.scheme != "https" and not local_http)
            or parsed.username or parsed.password or parsed.fragment):
        raise AIError("نشانی سرویس هوش مصنوعی امن یا معتبر نیست.")
    return endpoint


def _validate_prompt(system: str, user: str) -> None:
    if not isinstance(system, str) or not isinstance(user, str):
        raise AIError("درخواست هوش مصنوعی معتبر نیست.")
    if not user.strip():
        raise AIError("متن درخواست هوش مصنوعی خالی است.")
    if len(system) + len(user) > 24_000:
        raise AIError("متن درخواست هوش مصنوعی بیش از حد طولانی است.")


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

    # Order matters for tests and for deterministic fallback when
    # ``random.shuffle`` is patched out.
    models = (
        "llama-3.1-8b-instant",
        "allam-2-7b",
        "openai/gpt-oss-20b",
    )

    endpoint = (
        os.getenv("AI_GROQ_ENDPOINT")
        or "https://api.groq.com/openai/v1/chat/completions"
    )

    async def complete(self, system: str, user: str) -> str:
        _validate_prompt(system, user)
        _validate_endpoint(self.endpoint)
        candidates = list(self.models)
        random.shuffle(candidates)
        failures: list[AIError] = []
        for model in candidates:
            try:
                return await self._complete_model(model, system, user)
            except AIError as exc:
                failures.append(exc)
                logger.info(
                    "Groq model %s failed: %s", model, exc,
                )
        # The test asserts that the final message contains
        # "هیچ‌یک از مدل‌ها". Keep that substring intact.
        raise AIError(
            "هیچ‌یک از مدل‌ها در دسترس نبودند. "
            "اتصال، کلید API و دسترسی حساب را بررسی کنید."
        ) from (failures[-1] if failures else None)

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
            logger.info("Groq transport error: %s", type(exc).__name__)
            raise AIError(
                "ارتباط با سرویس هوش مصنوعی برقرار نشد."
            ) from exc

        if r.status_code != 200 or not isinstance(data, dict):
            logger.info(
                "Groq returned status=%s for model=%s",
                r.status_code, model,
            )
            if r.status_code in (401, 403):
                raise AIError(
                    "کلید API یا دسترسی سرویس هوش مصنوعی معتبر نیست."
                )
            if r.status_code == 429:
                raise AIError("محدودیت درخواست سرویس هوش مصنوعی.")
            if r.status_code == 404:
                raise AIError("مدل انتخاب‌شده در سرویس پیدا نشد.")
            raise AIError("سرویس هوش مصنوعی پاسخگو نیست.")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            logger.info("Groq returned malformed payload for model=%s", model)
            raise AIError(
                "پاسخ نامعتبر از سرویس هوش مصنوعی."
            ) from exc
        if not isinstance(content, str) or not content.strip():
            logger.info("Groq returned empty content for model=%s", model)
            raise AIError("پاسخ خالی از سرویس هوش مصنوعی.")
        content = content.strip()
        if len(content) > 50_000:
            raise AIError("پاسخ هوش مصنوعی بیش از حد بزرگ است.")
        return content


class AnthropicProvider(AIProvider):
    name = "anthropic"
    default_model = "claude-sonnet-4-6"

    endpoint = (
        os.getenv("AI_ANTHROPIC_ENDPOINT")
        or "https://api.anthropic.com/v1/messages"
    )
    api_version = "2023-06-01"

    async def complete(self, system: str, user: str) -> str:
        _validate_prompt(system, user)
        _validate_endpoint(self.endpoint)
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
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Anthropic transport error: %s", type(exc).__name__)
            raise AIError("ارتباط با سرویس هوش مصنوعی برقرار نشد.") from exc

        if r.status_code in (401, 403):
            raise AIError(
                "کلید API یا دسترسی سرویس هوش مصنوعی معتبر نیست."
            )
        if r.status_code == 429:
            raise AIError("محدودیت درخواست سرویس هوش مصنوعی.")
        if r.status_code != 200:
            logger.info("Anthropic returned status=%s", r.status_code)
            raise AIError("سرویس هوش مصنوعی پاسخگو نیست.")

        try:
            result = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if isinstance(block, dict)
            ).strip()
        except AttributeError as exc:
            raise AIError("پاسخ نامعتبر از سرویس هوش مصنوعی.") from exc
        if not result:
            raise AIError("پاسخ خالی از سرویس هوش مصنوعی.")
        if len(result) > 50_000:
            raise AIError("پاسخ هوش مصنوعی بیش از حد بزرگ است.")
        return result


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
