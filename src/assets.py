"""Remote UI assets with silent, cached download and icon fallback.

A few decorative images (splash background, empty-state illustration,
post/channel placeholders) are downloaded once and cached on the device
so the APK does not need to ship binary assets and visuals can be
updated without a new release.

Design guarantees
-----------------
* Downloads run entirely in the background — the user never sees a
  spinner, dialog or snackbar for them.
* Every asset has *multiple candidate URLs*. The cache tries them in
  order and stops at the first success. If every candidate fails, the
  asset is silently left missing and the UI falls back to a vector icon
  or a coloured placeholder.
* Every download has a strict per-asset timeout and a size cap, so a
  slow or broken endpoint can never block boot or fill up storage.
* The cache is keyed by *asset name*, never by URL. Changing a URL for
  a given name requires no changes at any call site.
* The on-disk format is plain base64 in SharedPreferences, which is
  exactly what Flet's ``ft.Image(src_base64=...)`` expects.
* ``refresh()`` re-downloads everything, ``clear()`` wipes the cache.
  Both are safe to call at any time.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx


logger = logging.getLogger(__name__)


# ================================================================ candidates
#
# Each asset name maps to an ordered list of URLs. The cache tries them
# one by one and stops at the first successful response. Keep the lists
# short — the point is resilience, not a full mirroring strategy.
#
# Only the first URL is expected to succeed under normal conditions. The
# rest are fallbacks for networks where the primary host is unreachable.

ASSET_CANDIDATES: dict[str, list[str]] = {
    # Splash background: shown briefly on cold start before the app
    # finishes bootstrapping. Kept small (720×1280) to stay under the
    # size cap even on slow mobile connections.
    "splash": [
        "https://placehold.co/720x1280/0066FF/FFFFFF/png?text=Eitaa+Manager",
        "https://placehold.co/720x1280/0047B2/FFFFFF/png?text=Eitaa",
        "https://picsum.photos/seed/eitaa-splash/720/1280",
    ],

    # Post placeholder: shown on cards whose post has no attached media.
    "post_placeholder": [
        "https://placehold.co/600x400/EEF2F7/94A3B8/png?text=No+Image",
        "https://placehold.co/600x400/FFFFFF/CBD5E1/png?text=",
        "https://picsum.photos/seed/eitaa-post/600/400",
    ],

    # Channel placeholder: shown in the channel picker and history view.
    "channel_placeholder": [
        "https://placehold.co/400x400/0066FF/FFFFFF/png?text=Eitaa",
        "https://placehold.co/400x400/0047B2/FFFFFF/png?text=Eitaa",
    ],

    # Generic empty-state illustration, tinted to match the app surface.
    "empty_state": [
        "https://placehold.co/480x360/F7F9FC/CBD5E1/png?text=",
        "https://placehold.co/480x360/FFFFFF/E2E8F0/png?text=",
    ],
}


# ==================================================================== limits

# Per-asset size cap. 250 KB is more than enough for a solid-colour PNG
# or a small illustrative JPEG, and small enough that caching four of
# them in SharedPreferences never becomes a storage concern.
_MAX_BYTES_PER_ASSET = 250_000

# Per-request timeouts. Kept short because these are decorative: it is
# better to fall back to an icon quickly than to hold a connection open.
_DOWNLOAD_TIMEOUT = 12.0
_CONNECT_TIMEOUT = 5.0

# Whole-pass ceiling: even if every candidate times out, the pass aborts
# after this many seconds and the app continues with whatever was cached.
_PASS_TIMEOUT = 25.0

# SharedPreferences key prefix. Kept short; each key is ``prefix + name``.
_CACHE_KEY_PREFIX = "asset_cache:"


# ==================================================================== cache

class AssetCache:
    """Silent downloader and on-device cache for decorative UI assets.

    Typical usage in the app::

        cache = AssetCache(api.storage)
        await cache.load_from_disk()          # cheap, synchronous-ish
        asyncio.create_task(cache.ensure_all())  # fire-and-forget
        ...
        b64 = cache.get("post_placeholder")   # None if not available
        if b64:
            image = ft.Image(src_base64=b64)
        else:
            image = ft.Icon(ft.Icons.IMAGE_OUTLINED)

    The class is intentionally storage-agnostic: it only expects the same
    ``get``/``set``/``remove`` coroutine interface that ``main.Storage``
    exposes.
    """

    def __init__(self, storage) -> None:
        self.storage = storage
        self._memory: dict[str, str] = {}
        self._loaded = False

    # -------------------------------------------------------------- public
    async def load_from_disk(self) -> None:
        """Populate the in-memory cache from persistent storage.

        Safe to call more than once; only reads on the first call per
        process. Never raises: a broken storage layer simply leaves the
        cache empty and every ``get`` returns ``None``.
        """
        if self._loaded:
            return
        for name in ASSET_CANDIDATES:
            try:
                raw = await self.storage.get(_CACHE_KEY_PREFIX + name)
            except Exception:
                raw = None
            if isinstance(raw, str) and raw:
                self._memory[name] = raw
        self._loaded = True

    def get(self, name: str) -> str | None:
        """Return the base64 payload for ``name`` or ``None``.

        Never raises, never returns an empty string. Callers should treat
        ``None`` as "not available" and render a vector-icon fallback.
        """
        value = self._memory.get(name)
        if isinstance(value, str) and value:
            return value
        return None

    def has(self, name: str) -> bool:
        """True when the asset is present in the in-memory cache."""
        return self.get(name) is not None

    async def ensure_all(self) -> None:
        """Download any missing assets. Safe to call on every boot.

        Missing assets are retried on subsequent boots; once cached they
        are skipped entirely. Any failure is swallowed and logged at INFO
        level — the UI never learns that a download failed, it just uses
        the fallback path.
        """
        await self.load_from_disk()
        missing = [
            name for name in ASSET_CANDIDATES
            if name not in self._memory
        ]
        if not missing:
            return

        try:
            await asyncio.wait_for(
                self._download_pass(missing), timeout=_PASS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.info(
                "Asset download pass exceeded %.0fs; "
                "%d assets remain missing.",
                _PASS_TIMEOUT,
                len([n for n in missing if n not in self._memory]),
            )
        except Exception as exc:
            logger.info(
                "Asset download pass failed: %s", type(exc).__name__,
            )

    async def refresh(self) -> None:
        """Force re-download of every asset.

        Useful after changing a URL or when a caller wants to repair a
        partially populated cache. Failures remain silent, exactly like
        ``ensure_all``.
        """
        self._memory.clear()
        for name in ASSET_CANDIDATES:
            try:
                await self.storage.remove(_CACHE_KEY_PREFIX + name)
            except Exception:
                pass
        await self.ensure_all()

    async def clear(self) -> None:
        """Wipe the cache entirely (memory and disk)."""
        self._memory.clear()
        for name in ASSET_CANDIDATES:
            try:
                await self.storage.remove(_CACHE_KEY_PREFIX + name)
            except Exception:
                pass

    # ------------------------------------------------------------ internal
    async def _download_pass(self, names: list[str]) -> None:
        """Try to fetch each missing asset once, with a shared client."""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _DOWNLOAD_TIMEOUT, connect=_CONNECT_TIMEOUT,
                ),
                follow_redirects=True,
                headers={"User-Agent": "EitaaManager/asset-cache"},
            ) as client:
                for name in names:
                    if name in self._memory:
                        continue
                    await self._download(client, name)
        except Exception as exc:
            logger.info(
                "Shared HTTP client setup failed: %s", type(exc).__name__,
            )

    async def _download(self, client: httpx.AsyncClient, name: str) -> None:
        """Try each candidate URL for ``name`` in order."""
        candidates = ASSET_CANDIDATES.get(name) or []
        for url in candidates:
            payload = await self._fetch_one(client, url)
            if payload is None:
                continue
            try:
                encoded = base64.b64encode(payload).decode("ascii")
            except (TypeError, ValueError):
                continue
            if not encoded:
                continue
            self._memory[name] = encoded
            try:
                await self.storage.set(_CACHE_KEY_PREFIX + name, encoded)
            except Exception:
                # In-memory copy is still valid for this session.
                pass
            logger.info(
                "Asset %s cached (%d bytes from %s)",
                name, len(payload), url,
            )
            return
        logger.info(
            "Asset %s could not be downloaded from any candidate.", name,
        )

    @staticmethod
    async def _fetch_one(
        client: httpx.AsyncClient, url: str,
    ) -> bytes | None:
        """Fetch a single URL, returning bytes or ``None`` on any failure."""
        try:
            response = await client.get(url)
        except (httpx.HTTPError, ValueError):
            return None
        if response.status_code != 200:
            return None
        data = response.content or b""
        if not data:
            return None
        if len(data) > _MAX_BYTES_PER_ASSET:
            return None
        # Reject obviously broken responses (HTML error pages served with
        # a 200 status by captive portals, for example).
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type:
            return None
        return data


# ================================================================== helpers

def asset_names() -> tuple[str, ...]:
    """Return every asset key. Used by tests and by refresh tooling."""
    return tuple(ASSET_CANDIDATES.keys())
