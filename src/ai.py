"""Keyless AI access with bounded fallback between public text services."""

from __future__ import annotations

import random
from typing import Any

import httpx


class AIServiceError(Exception):
    """Raised when no configured public text service returns usable content."""


class AnonymousAI:
    _POLLINATIONS_URL = "https://text.pollinations.ai/openai/chat/completions"
    _POLLINATIONS_MODEL = "gpt-oss-20b"
    _UNCLOSEAI_URL = "https://hermes.ai.unturf.com/v1/chat/completions"
    _UNCLOSEAI_MODELS_URL = "https://hermes.ai.unturf.com/v1/models"
    _UNCLOSEAI_MODEL_FALLBACK = "hermes"

    def __init__(self, timeout: float = 22.0, connect_timeout: float = 6.0):
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)

    async def complete(self, system: str, user: str) -> str:
        providers = ["pollinations", "uncloseai"]
        random.shuffle(providers)
        for provider in providers:
            try:
                if provider == "pollinations":
                    return await self._complete_pollinations(system, user)
                return await self._complete_uncloseai(system, user)
            except (AIServiceError, httpx.HTTPError, ValueError, TypeError):
                continue
        raise AIServiceError(
            "دستیار هوشمند در حال حاضر پاسخگو نیست. "
            "اتصال اینترنت را بررسی کنید و دوباره تلاش کنید."
        )

    async def _complete_pollinations(self, system: str, user: str) -> str:
        return await self._post_completion(
            self._POLLINATIONS_URL,
            self._POLLINATIONS_MODEL,
            system,
            user,
        )

    async def _complete_uncloseai(self, system: str, user: str) -> str:
        model = await self._uncloseai_model()
        return await self._post_completion(
            self._UNCLOSEAI_URL,
            model,
            system,
            user,
        )

    async def _uncloseai_model(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self._UNCLOSEAI_MODELS_URL)
            if response.status_code == 200:
                payload = response.json()
                records = (
                    payload.get("data", [])
                    if isinstance(payload, dict) else []
                )
                for record in records:
                    model = (
                        record.get("id")
                        if isinstance(record, dict) else None
                    )
                    if isinstance(model, str) and model.strip():
                        return model.strip()
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        return self._UNCLOSEAI_MODEL_FALLBACK

    async def _post_completion(
        self, url: str, model: str, system: str, user: str
    ) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.7,
                        "max_tokens": 1500,
                    },
                )
            if response.status_code != 200:
                raise AIServiceError("Public AI service rejected the request.")
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AIServiceError("Public AI service request failed.") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise AIServiceError(
                "Public AI service returned invalid data."
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise AIServiceError("Public AI service returned an empty response.")
        return content.strip()
