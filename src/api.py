"""HTTP/AI client for the Eitaa Channel Manager.

Two modes are supported, chosen by `config.MODE`:

    * `"local"`  — every request is routed to `LocalBackend` from
      `local.py`. No network, no server, no login. Posts and settings
      are persisted in the device's client storage.

    * `"server"` — every request goes to the FastAPI backend at
      `config.SERVER_URL` over HTTPS.

The public surface (`get`, `post`, `delete`, `ai`, `upload`, `close`)
is identical in both modes, so `main.py` never needs to know which
backend is answering.

Error handling
--------------
Both backends raise `ApiError` with a Persian `str(error)` message that
is safe to show to the user. `LocalError` from `local.py` is converted
into `ApiError` transparently.
"""

from __future__ import annotations

import mimetypes
from typing import Any

import httpx

import config
from local import LocalBackend, LocalError


# ----------------------------------------------------------------- timeouts --
TIMEOUT_DEFAULT = 30.0
TIMEOUT_AI = 90.0
TIMEOUT_UPLOAD = 180.0


# ----------------------------------------------------------------- ApiError --
class ApiError(Exception):
    """A user-facing error.

    Attributes:
        status:   HTTP status code if the error came from a server response;
                  None for network errors or local errors.
        network:  True when the request never reached a backend (no internet,
                  timeout, DNS failure, …). The UI can use this to show the
                  "check your connection" screen.
        local:    True when the error comes from `LocalBackend` (feature not
                  available offline). The UI can show a different hint.
    """

    def __init__(
        self,
        msg: str,
        status: int | None = None,
        network: bool = False,
        local: bool = False,
    ):
        super().__init__(msg)
        self.status = status
        self.network = network
        self.local = local


# ======================================================================== Api =
class Api:
    """Unified façade over either a local backend or an HTTP server."""

    def __init__(self, storage=None):
        self.mode = config.MODE  # "local" | "server"
        self.base = (config.SERVER_URL or "").rstrip("/")
        self.token: str | None = None
        self.on_401 = lambda: None  # invoked when the server rejects the session

        # Local backend (only used when mode == "local").
        self._local: LocalBackend | None = (
            LocalBackend(storage) if storage is not None else None
        )
        self._provisioned = False

        # HTTP client (only created when needed; cheap to keep around).
        self._client: httpx.AsyncClient | None = None

    # -------------------------------------------------------------- lifecycle
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_DEFAULT)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ---------------------------------------------------------------- setup --
    async def bootstrap(self) -> None:
        """Prepare the chosen backend. Safe to call multiple times.

        In local mode this installs the config-provided credentials and
        the default channel on first run.
        """
        if self.mode == "local":
            if self._local is None:
                raise ApiError(
                    "حالت محلی بدون حافظه‌ی دستگاه قابل اجرا نیست.",
                    local=True,
                )
            if not self._provisioned:
                await self._local.ensure_provisioned()
                self._provisioned = True

    # ============================================================ dispatcher =
    async def req(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        files: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        if self.mode == "local":
            return await self._req_local(method, path, json)
        return await self._req_server(
            method, path,
            json=json, files=files, data=data, params=params,
            timeout=timeout,
        )

    # --------------------------------------------------------------- local --
    async def _req_local(self, method: str, path: str, body: Any) -> Any:
        if self._local is None:
            raise ApiError("حالت محلی فعال نیست.", local=True)
        try:
            return await self._local.handle(method, path, body)
        except LocalError as e:
            raise ApiError(str(e), local=True) from e

    # -------------------------------------------------------------- server --
    async def _req_server(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        files: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        if not self.base:
            raise ApiError(
                "آدرس سرور تنظیم نشده است.",
                network=True,
            )

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        url = self.base + "/api" + path
        client = await self._http()

        try:
            r = await client.request(
                method, url,
                json=json, files=files, data=data, params=params,
                headers=headers,
                timeout=TIMEOUT_DEFAULT if timeout is None else timeout,
            )
        except (httpx.TimeoutException, httpx.HTTPError, ValueError):
            raise ApiError(
                "اتصال به سرور برقرار نشد. اینترنت خود را بررسی کنید و دوباره تلاش کنید.",
                network=True,
            )

        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {}

        if r.status_code == 401 and self.token:
            # `on_401` must be idempotent on the App side.
            self.on_401()

        if r.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise ApiError(
                detail if isinstance(detail, str) else "خطای نامشخص رخ داد.",
                status=r.status_code,
            )
        return body

    # ============================================================== shortcuts
    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self.req("GET", path, params=params)

    async def post(self, path: str, body: Any = None, *,
                   timeout: float | None = None) -> Any:
        return await self.req(
            "POST", path,
            json=body if body is not None else {},
            timeout=timeout,
        )

    async def delete(self, path: str) -> Any:
        return await self.req("DELETE", path)

    # ================================================================== AI ===
    async def ai(self, action: str, text: str, prompt: str = "") -> str:
        """Run an AI action and return the generated text.

        In local mode this talks directly to Groq. In server mode this
        goes through the FastAPI backend's `/api/ai` endpoint.
        """
        r = await self.post(
            "/ai",
            {"action": action, "text": text, "prompt": prompt},
            timeout=TIMEOUT_AI,
        )
        if isinstance(r, dict):
            return str(r.get("text", ""))
        return ""

    # ============================================================== upload ===
    async def upload(self, name: str, data: bytes, tags: str = "") -> Any:
        """Upload a media file.

        Only meaningful in server mode. In local mode the backend will
        raise a LocalError explaining the feature is unavailable.
        """
        if self.mode == "local":
            raise ApiError(
                "کتابخانه رسانه فقط با سرور در دسترس است.",
                local=True,
            )

        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return await self.req(
            "POST", "/media",
            files={"file": (name, data, ctype)},
            data={"tags": tags} if tags else None,
            timeout=TIMEOUT_UPLOAD,
        )

    # ============================================================= helpers ===
    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    def describe(self) -> str:
        """Short human-readable description of the current mode."""
        if self.mode == "local":
            return "حالت محلی (بدون سرور)"
        return f"سرور: {self.base}"
