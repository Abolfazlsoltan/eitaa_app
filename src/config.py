"""Build-time configuration for the Eitaa Channel Manager Android client.

Two execution modes
-------------------
The client can run in one of two modes, chosen by the `MODE` constant below:

    MODE = "local"   ← همه چیز روی گوشی اجرا می‌شود (پیش‌فرض فعلی)
    MODE = "server"  ← کلاینت به FastAPI بک‌اند وصل می‌شود

In local mode the app does not need any server. AI calls go directly to
Groq and posts go directly to EitaaYar from the device. Features that
strictly require a 24/7 worker (scheduling, automations, media library,
team management, advanced analytics) are disabled with a clear message
instead of failing.

In server mode the app behaves exactly like before: it talks to the
FastAPI backend at SERVER_URL.

Switching to server later
-------------------------
When you have a server running, change these two lines:

    MODE = "server"
    SERVER_URL = "https://channels.example.com"

Nothing else needs to change.

Credentials
-----------
For the current test phase, the EitaaYar token, the Groq API key and the
default channel are hard-coded below so the app works out of the box.
Normal users never see these values — the onboarding and settings screens
hide them and only show a "connected" state. Later, when the app is
public, these should be removed and users should enter their own values.
"""

import os

# --------------------------------------------------------------------- mode ---
# "local"  → everything runs on the device (no server needed)
# "server" → client connects to the FastAPI backend at SERVER_URL
MODE = "local"

# Only used when MODE == "server". Ignored in local mode.
# Example: "https://channels.example.com" (no trailing slash, no "/api")
SERVER_URL = ""

# Environment variable can override SERVER_URL at build time:
#   export EITAA_CLIENT_SERVER_URL="https://..."
# It has no effect in local mode.
if os.getenv("EITAA_CLIENT_SERVER_URL"):
    SERVER_URL = os.getenv("EITAA_CLIENT_SERVER_URL", "").strip().rstrip("/")
    MODE = "server"

# ------------------------------------------------------------------ identity --
APP_NAME = "مدیریت کانال ایتا"
VERSION = "1.1.0"

# -------------------------------------------------------------- credentials --
# ⚠️ TEST PHASE ONLY — these values are baked into the APK.
# They are hidden from the UI but a determined user can still extract them
# from the installed app. Do NOT ship this to the public Play Store.

# EitaaYar bot token (includes the "botNNN:" prefix).
EITAA_TOKEN = "bot278898:9e293be8-2dc3-4483-9e0b-f81d0f39efc7"

# Default channel that the app connects to on first run.
DEFAULT_CHANNEL_ID = "11259266"

# Groq API key for AI features.
GROQ_API_KEY = "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"
GROQ_MODEL = "llama-3.3-70b-versatile"

# -------------------------------------------------------------- HTTP client --
# Normal API calls (Groq, EitaaYar): 30 seconds.
REQUEST_TIMEOUT = 30

# Uploads (when server mode is on): 180 seconds.
UPLOAD_TIMEOUT = 180

# -------------------------------------------------------- Android cleartext ---
# Android 9+ blocks plain http:// by default. In local mode we always talk
# to the internet over https:// so this can stay False. It only matters when
# you test server mode against a local backend over http://.
ALLOW_CLEARTEXT = False
