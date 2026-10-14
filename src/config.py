"""Eitaa Manager — Application configuration.

Every value that varies between builds is centralized here. You can override
any of them at build time via environment variables, which is convenient for
CI builds where you do not want to edit source files.

    EITAA_SERVER_URL   — optional backend address
    EITAA_TOKEN        — EitaaYar bot token
    EITAA_CHANNEL      — default channel identifier
    GROQ_API_KEY       — Groq API key for AI features

Values that are not read from the environment are constants intentionally
kept in code so that the built APK behaves identically to a desktop run.
"""

from __future__ import annotations

import os


# ------------------------------------------------------------------ identity
APP_NAME = "ایتا منیجر"
APP_TAGLINE = "مدیریت هوشمند کانال‌های ایتا"
VERSION = "2.1.0"

# Package / org identifier used by the Flet build. Kept here so code paths
# that display it (About screen, update dialog) always match pyproject.toml.
PACKAGE_ID = "app.eitaamanager.eitaa_manager"
ORG_ID = "app.eitaamanager"


# ------------------------------------------------------------------- backend
# Optional backend address. When empty, the app keeps all data on the device
# and connects directly to EitaaYar and the AI service. When set, the app
# syncs through the FastAPI backend at this address.
SERVER_URL = os.getenv("EITAA_SERVER_URL", "").strip().rstrip("/")


# --------------------------------------------------------------- credentials
# EitaaYar bot token used to publish on the user's behalf.
EITAA_TOKEN = os.getenv(
    "EITAA_TOKEN",
    "bot278898:9e293be8-2dc3-4483-9e0b-f81d0f39efc7",
)

# Channel that the app connects to automatically on first run.
EITAA_CHANNEL = os.getenv("EITAA_CHANNEL", "11259266")


# --------------------------------------------------------------- AI service
GROQ_API_KEY = os.getenv(
    "GROQ_API_KEY",
    "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",
)
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


# ------------------------------------------------------------------- layout
# Number of items kept in the local activity feed.
ACTIVITY_LIMIT = 60
# Maximum number of locally stored posts.
POSTS_LIMIT = 200


# =================================================================
# Feature flags — safe switches for the current rollout.
# Set to False in a build to hide a feature entirely, without touching UI
# code. Every new feature added to the app must be gated here so a bad
# release can be turned off by flipping a single line.
# =================================================================

# Allow optional local profile (name, username, avatar) on first run.
FEATURES_LOCAL_PROFILE = True

# Enable the global notification/dialog system across screens.
FEATURES_GLOBAL_NOTIFICATIONS = True

# Show the live Eitaa-style post preview inside the composer.
FEATURES_LIVE_PREVIEW = True

# Allow attaching images in the AI chat as visual context.
FEATURES_AI_CHAT_IMAGES = False

# Show the "check for updates" entry in Settings.
FEATURES_UPDATE_CHECK = True

# Show a global search button in the app bar of list screens.
FEATURES_GLOBAL_SEARCH = False


# =================================================================
# Profile — user-editable fields kept only on the device.
# =================================================================

# Minimum and maximum length of the optional username.
PROFILE_USERNAME_MIN = 3
PROFILE_USERNAME_MAX = 32

# Maximum length of the display name.
PROFILE_DISPLAY_NAME_MAX = 60

# Avatar constraints (bytes). 1.5 MB is safe for SharedPreferences and is
# roughly the size of a 512x512 JPEG at high quality.
PROFILE_AVATAR_MAX_BYTES = 1_500_000
PROFILE_AVATAR_MIN_BYTES = 256

# Accepted avatar mime-types (validated on save by the UI layer).
PROFILE_AVATAR_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
)

# Allowed username characters. Latin letters, Persian-friendly digits, and
# underscores. Enforced by UI validation, not silently stripped.
PROFILE_USERNAME_ALLOWED_CHARS = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)


# =================================================================
# Notifications registry
#
# A single source of truth for in-app promotions, update banners and
# feature announcements. The data layer in ``src/api.py`` reads this
# registry and decides whether a notification should be shown on the
# current device based on:
#
#     * ``active``           — master on/off switch
#     * ``expires_at``       — auto-hide after the given UTC date
#     * ``max_shows``        — do not show more than N times on one device
#     * ``cooldown_hours``   — minimum hours between two consecutive shows
#
# The UI decides how to render (dialog, bottom sheet, banner) based on
# ``kind`` and ``priority``.
#
# Keep this list short and honest. A notification is a promise: "there is
# something new and worth your time". If it is not true, remove the entry.
# =================================================================

NOTIFICATION_REGISTRY: list[dict] = [
    {
        "id": "welcome-2.1",
        "kind": "intro",
        "priority": 10,
        "active": True,
        "title": "به نسخه تازه خوش آمدید",
        "body": (
            "چند بهبود کوچک اما کاربردی اضافه شد: قالب‌بندی Markdown در "
            "دستیار هوشمند، پروفایل اختیاری، و صفحات سریع‌تر. اگر سؤالی "
            "داری از راهنمای اتصال شروع کن."
        ),
        "action_label": "دیدن راهنما",
        "action_key": "open_help",
        "secondary_label": "بعداً",
        "icon": "auto_awesome",
        "expires_at": "2027-01-01T00:00:00",
        "max_shows": 2,
        "cooldown_hours": 24,
    },
    {
        "id": "profile-tip",
        "kind": "tip",
        "priority": 40,
        "active": True,
        "title": "پروفایل خودت را کامل کن",
        "body": (
            "می‌توانی نام و عکس پروفایل خودت را وارد کنی تا در تنظیمات و "
            "پست‌ها نمایش داده شود. این اطلاعات فقط روی دستگاه خودت ذخیره "
            "می‌شود."
        ),
        "action_label": "تنظیم پروفایل",
        "action_key": "open_profile",
        "secondary_label": "بی‌خیال",
        "icon": "person_outline",
        "expires_at": "2027-06-01T00:00:00",
        "max_shows": 1,
        "cooldown_hours": 72,
    },
    {
        "id": "update-check",
        "kind": "info",
        "priority": 60,
        "active": True,
        "title": "بررسی نسخه جدید",
        "body": (
            "از تنظیمات می‌توانی نسخه‌های پایدار منتشرشده را ببینی و "
            "APK تازه را از مرورگر دریافت کنی."
        ),
        "action_label": "بررسی به‌روزرسانی",
        "action_key": "check_updates",
        "secondary_label": "بستن",
        "icon": "system_update_alt",
        "expires_at": "",
        "max_shows": 3,
        "cooldown_hours": 168,
    },
]


# =================================================================
# Design tokens — spacing, radii and sizes used across the UI.
# Keeping them here makes it trivial to enforce a consistent look on
# every screen, instead of re-declaring magic numbers in each view.
# =================================================================

# Corner radii
RADIUS_SMALL = 10
RADIUS_MEDIUM = 14
RADIUS_LARGE = 18
RADIUS_XLARGE = 22
RADIUS_PILL = 999

# Standard paddings
PADDING_PAGE_H = 16
PADDING_PAGE_V = 16
PADDING_CARD = 16
PADDING_HEADER_V = 20

# Icon sizes
ICON_SMALL = 16
ICON_MEDIUM = 20
ICON_LARGE = 24
ICON_XLARGE = 32

# Animation timing (ms) for small, subtle movements.
ANIMATION_FAST_MS = 150
ANIMATION_NORMAL_MS = 250
ANIMATION_SLOW_MS = 350


# =================================================================
# Behavior / limits used by the UI layer.
# =================================================================

# Chat transcript: keep the last N messages on the device.
CHAT_HISTORY_LIMIT = 30

# How many past messages are sent to the AI as context for the next reply.
CHAT_CONTEXT_MESSAGES = 7

# Per-message limit for the AI context window (characters).
CHAT_CONTEXT_CHAR_LIMIT = 1500

# Maximum number of media items listed in the media screen.
MEDIA_LIST_LIMIT = 200

# Maximum size of a single upload, in bytes (20 MB). Mirrors the backend.
UPLOAD_MAX_BYTES = 20 * 1024 * 1024


# =================================================================
# Public helpers
# =================================================================

def is_feature_enabled(name: str) -> bool:
    """Return True when a named ``FEATURES_*`` flag exists and is enabled.

    Usage:
        if config.is_feature_enabled("FEATURES_LIVE_PREVIEW"):
            ...
    """
    if not isinstance(name, str) or not name.startswith("FEATURES_"):
        return False
    value = globals().get(name)
    return bool(value)


def active_notifications(now_iso: str | None = None) -> list[dict]:
    """Return the subset of notification entries that are not disabled.

    This helper only filters on ``active`` and ``expires_at``. Per-device
    checks (dismissed, snoozed, cooldown, max shows) live in the data
    layer so that the UI stays a pure function of what the API returns.
    """
    import datetime as _dt

    try:
        now = (
            _dt.datetime.fromisoformat(now_iso)
            if now_iso else _dt.datetime.utcnow()
        )
    except ValueError:
        now = _dt.datetime.utcnow()

    result: list[dict] = []
    for item in NOTIFICATION_REGISTRY:
        if not isinstance(item, dict):
            continue
        if item.get("active") is False:
            continue
        expires = item.get("expires_at")
        if isinstance(expires, str) and expires.strip():
            try:
                if _dt.datetime.fromisoformat(expires) <= now:
                    continue
            except ValueError:
                pass
        result.append(item)
    result.sort(key=lambda item: int(item.get("priority", 50)))
    return result
