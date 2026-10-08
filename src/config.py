"""Eitaa Manager — Application configuration.

Every value that varies between builds is centralized here. You can override
any of them at build time via environment variables, which is convenient for
CI builds where you do not want to edit source files.

    EITAA_SERVER_URL   — optional backend address
    EITAA_TOKEN        — EitaaYar bot token
    EITAA_CHANNEL      — default channel identifier
    GROQ_API_KEY       — Groq API key for AI features
"""

import os

# ------------------------------------------------------------------ identity
APP_NAME = "ایتا منیجر"
APP_TAGLINE = "مدیریت هوشمند کانال‌های ایتا"
VERSION = "2.0.0"

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
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ------------------------------------------------------------------- layout
# Number of items kept in the local activity feed.
ACTIVITY_LIMIT = 60
# Maximum number of locally stored posts.
POSTS_LIMIT = 200
