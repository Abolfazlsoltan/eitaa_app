"""Eitaa Manager — a modern client for managing Eitaa channels.

The app handles content creation with AI assistance, direct publishing to
EitaaYar, and lightweight analytics — all through a single, cohesive UI.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import io
import json
import logging
import mimetypes
import re
import time
from pathlib import Path

import flet as ft

import config
from api import Api, ApiError
from fa import (
    combine_local,
    digits,
    humanize_bytes,
    is_valid_username,
    jdate,
    jfull_utc,
    to_utc_iso,
)

logger = logging.getLogger(__name__)


# =================================================================== palette

class Palette:
    # Brand
    PRIMARY = "#2563EB"
    PRIMARY_DARK = "#1D4ED8"
    ACCENT = "#06B6D4"
    # Surfaces (mutated by apply_appearance for light/dark)
    SURFACE = "#F8FAFC"
    SURFACE_DARK = "#0B1120"
    CARD = "#FFFFFF"
    CARD_DARK = "#141C2B"
    # Text
    TEXT = "#0F172A"
    TEXT_DARK = "#F1F5F9"
    MUTED = "#64748B"
    MUTED_DARK = "#94A3B8"
    # Borders & dividers
    BORDER = "#E2E8F0"
    BORDER_DARK = "#1E293B"
    # Semantic
    SUCCESS = "#10B981"
    WARNING = "#F59E0B"
    DANGER = "#EF4444"
    # Soft backgrounds for pills / chips
    SUCCESS_SOFT = "#D1FAE5"
    WARNING_SOFT = "#FEF3C7"
    DANGER_SOFT = "#FEE2E2"
    PRIMARY_SOFT = "#DBEAFE"
    # Aliases kept for backward-compat inside this module
    SURFACE_LIGHT = SURFACE
    CARD_LIGHT = CARD
    TEXT_LIGHT = TEXT
    MUTED_LIGHT = MUTED
