"""Persian localization helpers — digits, Jalali calendar, timezone.

The app stores every timestamp in the device's local timezone. When a
timestamp comes back from a backend (always in UTC), it is converted to
local time here before it reaches the UI. When the UI sends a timestamp
to the backend, it is converted back to naive-UTC.
"""

from __future__ import annotations

import datetime as dt

# --------------------------------------------------------------- digits ----

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def digits(value) -> str:
    """Convert ASCII digits in a string (or number) to Persian digits."""
    return str(value).translate(_FA_DIGITS)


# ------------------------------------------------------------ Jalali date --

def g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Gregorian → Jalali conversion (Borkowski's algorithm)."""
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + 365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        + gd
        + gdm[gm - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm = 1 + days // 31 if days < 186 else 7 + (days - 186) // 30
    jd = 1 + (days % 31 if days < 186 else (days - 186) % 30)
    return jy, jm, jd


def _local_tz() -> dt.tzinfo:
    """Return the device's current timezone."""
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def to_local(value) -> dt.datetime:
    """Parse a timestamp and return a naive datetime in local time.

    Accepts:
        * a string in ISO format, with or without a timezone offset
        * a `datetime` object (naive or aware)

    A value without a timezone is assumed to be UTC — this matches the
    backend, which always stores UTC.
    """
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)

    return d.astimezone(_local_tz()).replace(tzinfo=None)


def to_utc_iso(local: dt.datetime) -> str:
    """Take a naive local datetime and return a naive-UTC ISO string.

    The backend compares timestamps against `datetime.utcnow()` (naive),
    so the output must not carry a timezone offset.
    """
    if local.tzinfo is None:
        local = local.replace(tzinfo=_local_tz())
    return (
        local
        .astimezone(dt.timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


# ------------------------------------------------------------ formatting ---

def jdate(d: dt.datetime | dt.date) -> str:
    """Format a date as Jalali, e.g. «۱۲ آبان ۱۴۰۳»."""
    if isinstance(d, dt.datetime):
        d = d.date()
    y, m, day = g2j(d.year, d.month, d.day)
    return digits(f"{day} {MONTHS[m - 1]} {y}")


def jfull(d: dt.datetime) -> str:
    """Format a datetime as «۱۲ آبان ۱۴۰۳ — ۱۴:۳۰»."""
    return jdate(d) + digits(f" — {d:%H:%M}")


def jfull_utc(value) -> str:
    """Format a UTC timestamp (string or datetime) as local Jalali."""
    return jfull(to_local(value))


def relative(value) -> str:
    """A short, human-friendly relative time («۵ دقیقه پیش»)."""
    when = to_local(value)
    now = dt.datetime.now()
    delta = now - when

    seconds = int(delta.total_seconds())
    if seconds < 0:
        return jfull(when)
    if seconds < 60:
        return "همین حالا"
    if seconds < 3600:
        return digits(f"{seconds // 60} دقیقه پیش")
    if seconds < 86400:
        return digits(f"{seconds // 3600} ساعت پیش")
    if seconds < 604800:
        return digits(f"{seconds // 86400} روز پیش")
    return jdate(when)
