"""Keyless AI access with bounded fallback between public text services.

Two public, key-free endpoints are used, tried in a random order so the
load is not always concentrated on one provider:

    * Pollinations  — OpenAI-compatible chat completions
    * UncloseAI     — OpenAI-compatible chat completions, with a live
                      model list read at runtime

If every route fails the caller receives a single, user-facing
:class:`AIServiceError`. Provider names, HTTP status codes and the
underlying exception are only written to the module logger so they can
never leak into a dialog or snackbar.

The endpoints and default models are defined here as class attributes
but can be overridden at build time through ``config`` (see
:func:`_config_str`). Overriding is optional — the built-in defaults
match the upstream public services and are safe to ship as-is.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import httpx


logger = logging.getLogger(__name__)


# --------------------------------------------------------------- helpers ---

def _config_str(name: str, default: str) -> str:
    """Read a string override from ``config`` when present.

    Importing ``config`` lazily keeps this module usable in isolated tests
    that do not provide a full application config.
    """
    try:
        import config  # type: ignore
    except Exception:
        return default
    value = getattr(config, name, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _config_float(name: str, default: float) -> float:
    try:
        import config  # type: ignore
    except Exception:
        return default
    value = getattr(config, name, None)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ exceptions ---

class AIServiceError(Exception):
    """Raised when no configured public text service returns usable content.

    The message is always a short Persian sentence suitable for showing
    directly to the user. It intentionally never mentions upstream
    provider names or HTTP details.
    """


# ================================================================= AnonymousAI

class AnonymousAI:
    """Keyless AI client with bounded fallback across public providers.

    Parameters
    ----------
    timeout:
        Base read timeout in seconds. A small additional budget is added
        per input length (see :meth:`_dynamic_timeout`).
    connect_timeout:
        Timeout for establishing the TCP/TLS connection.
    max_tokens:
        Upper bound on the model response length.
    temperature:
        Sampling temperature. 0.7 keeps replies natural without becoming
        unpredictable.
    """

    # ---- Pollinations ----------------------------------------------------
    _POLLINATIONS_URL = _config_str(
        "AI_POLLINATIONS_URL",
        "https://text.pollinations.ai/openai/chat/completions",
    )
    _POLLINATIONS_MODEL = _config_str(
        "AI_POLLINATIONS_MODEL", "gpt-oss-20b",
    )

    # ---- UncloseAI -------------------------------------------------------
    _UNCLOSEAI_URL = _config_str(
        "AI_UNCLOSEAI_URL",
        "https://hermes.ai.unturf.com/v1/chat/completions",
    )
    _UNCLOSEAI_MODELS_URL = _config_str(
        "AI_UNCLOSEAI_MODELS_URL",
        "https://hermes.ai.unturf.com/v1/models",
    )
    _UNCLOSEAI_MODEL_FALLBACK = _config_str(
        "AI_UNCLOSEAI_MODEL_FALLBACK", "hermes",
    )

    # ---- Order of providers to try. Randomised per call. ------------------
    _PROVIDER_ORDER: tuple[str, ...] = ("pollinations", "uncloseai")

    def __init__(
        self,
        timeout: float = 22.0,
        connect_timeout: float = 6.0,
        max_tokens: int = 1500,
        temperature: float = 0.7,
    ):
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self.base_timeout_seconds = float(timeout)
        self.connect_timeout_seconds = float(connect_timeout)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)

    # -------------------------------------------------------- public API ---
    async def complete(self, system: str, user: str) -> str:
        """Return plain-text content for ``system`` + ``user``.

        Providers are tried in a random order. Any provider-level error
        (network, rate limit, malformed response, empty reply) causes an
        immediate fallback to the next route. If every route fails, a
        single unified error is raised.
        """
        if not isinstance(system, str) or not isinstance(user, str):
            raise AIServiceError(
                "درخواست نامعتبر برای دستیار هوشمند."
            )

        providers = list(self._PROVIDER_ORDER)
        random.shuffle(providers)

        failures: list[tuple[str, BaseException]] = []

        for provider in providers:
            try:
                if provider == "pollinations":
                    return await self._complete_pollinations(system, user)
                return await self._complete_uncloseai(system, user)
            except AIServiceError as exc:
                failures.append((provider, exc))
                logger.info("AI provider %s failed: %s", provider, exc)
                continue
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                failures.append((provider, exc))
                logger.info(
                    "AI provider %s raised %s", provider, type(exc).__name__,
                )
                continue

        # Every route failed. Log the reasons internally, return a single
        # user-friendly error externally. The message must not contain the
        # provider names, so it uses a generic phrasing.
        logger.warning(
            "All AI providers failed (%d attempts).",
            len(failures),
        )
        raise AIServiceError(
            "دستیار هوشمند در حال حاضر پاسخگو نیست. "
            "اتصال اینترنت را بررسی کنید و دوباره تلاش کنید."
        )

    # ---------------------------------------------------- provider paths ---
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

    # ----------------------------------------------------- model discovery -
    async def _uncloseai_model(self) -> str:
        """Return the live model id, falling back to a safe default.

        Failure to read the list is *not* fatal: the request is still
        attempted with the built-in model id.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._dynamic_timeout("", ""),
            ) as client:
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

    # ---------------------------------------------------------- transport --
    async def _post_completion(
        self, url: str, model: str, system: str, user: str
    ) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(
                timeout=self._dynamic_timeout(system, user),
            ) as client:
                response = await client.post(
                    url, headers=headers, json=payload,
                )
            if response.status_code != 200:
                raise AIServiceError(
                    "Public AI service rejected the request."
                )
            body: Any = response.json()
        except AIServiceError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AIServiceError(
                "Public AI service request failed."
            ) from exc

        return self._extract_content(body)

    # ---------------------------------------------------------- parsing ----
    @staticmethod
    def _extract_content(payload: Any) -> str:
        """Extract the assistant text from a chat-completions response.

        Accepts the OpenAI-compatible envelope and normalizes the common
        shapes (string content, list of content blocks). Raises
        :class:`AIServiceError` on malformed or empty payloads.
        """
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIServiceError(
                "Public AI service returned invalid data."
            ) from exc

        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise AIServiceError(
                "Public AI service returned invalid data."
            )

        content = message.get("content")

        # Some providers wrap the text in a list of blocks like
        # ``[{"type": "text", "text": "..."}]``. Handle that shape too.
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            content = "".join(parts)

        if not isinstance(content, str):
            raise AIServiceError(
                "Public AI service returned invalid data."
            )

        cleaned = content.strip()
        if not cleaned:
            raise AIServiceError("Public AI service returned an empty response.")
        return cleaned

    # ---------------------------------------------------------- timing -----
    def _dynamic_timeout(self, system: str, user: str) -> httpx.Timeout:
        """Return a request timeout adjusted for the prompt size.

        Short prompts keep the base timeout so failures are detected
        quickly. Longer prompts add up to ``+50%`` of the base budget so
        the model has room to produce a complete answer without the user
        staring at a spinner longer than necessary on trivial requests.
        """
        total_chars = (len(system or "") + len(user or ""))
        # 200 chars → no bonus, 2000 chars → full bonus (50% of base).
        ratio = min(max(total_chars - 200, 0) / 1800.0, 1.0)
        read_timeout = self.base_timeout_seconds * (1.0 + 0.5 * ratio)
        return httpx.Timeout(read_timeout, connect=self.connect_timeout_seconds)
