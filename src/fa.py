"""Persian localization helpers — digits, Jalali calendar, timezone.

The app stores every timestamp in the device's local timezone. When a
timestamp comes back from a backend (always in UTC), it is converted to
local time here before it reaches the UI. When the UI sends a timestamp
to the backend, it is converted back to naive-UTC.

Conventions
-----------
* Every public helper that parses input has a ``*_safe`` counterpart that
  returns ``None`` (or a caller-provided default) instead of raising. The
  UI layer should prefer the safe variant so a single malformed value can
  never crash a screen.
* Jalali dates are formatted exactly as «۱۲ آبان ۱۴۰۳» to keep every
  screen visually consistent.
"""

from __future__ import annotations

import datetime as dt


# --------------------------------------------------------------- digits ----

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

# Index matches Python's ``date.weekday()``: Monday == 0.
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


def j2g(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Jalali → Gregorian conversion.

    Inverse of :func:`g2j` for the supported year range. Returns a tuple
    ``(gy, gm, gd)``; the caller is responsible for validating the input
    month/day (1..12 / 1..31).
    """
    jy += 1595
    days = (
        -355668
        + 365 * jy
        + (jy // 33) * 8
        + ((jy % 33) + 3) // 4
        + jd
    )
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += (jm - 7) * 30 + 186

    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1

    leap = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    month_lengths = [
        0,
        31,
        29 if leap else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    ]
    gm = 1
    while gm <= 12 and gd > month_lengths[gm]:
        gd -= month_lengths[gm]
        gm += 1
    return gy, gm, gd


def _local_tz() -> dt.tzinfo:
    """Return the device's current timezone."""
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


# -------------------------------------------------------- timezone layer ---

def to_local(value) -> dt.datetime:
    """Parse a timestamp and return a naive datetime in local time.

    Accepts:
        * a string in ISO format, with or without a timezone offset
        * a ``datetime`` object (naive or aware)

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


def to_local_safe(value, default: dt.datetime | None = None):
    """Like :func:`to_local`, but returns ``default`` on malformed input."""
    try:
        return to_local(value)
    except (ValueError, TypeError, OverflowError):
        return default


def to_utc_iso(local: dt.datetime) -> str:
    """Take a naive local datetime and return a naive-UTC ISO string.

    The backend compares timestamps against ``datetime.utcnow()`` (naive),
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


def jdate_numeric(d: dt.datetime | dt.date) -> str:
    """Format as «۱۴۰۳/۰۸/۱۲» — compact, for toolbars and meta rows."""
    if isinstance(d, dt.datetime):
        d = d.date()
    y, m, day = g2j(d.year, d.month, d.day)
    return digits(f"{y:04d}/{m:02d}/{day:02d}")


def jfull(d: dt.datetime) -> str:
    """Format a datetime as «۱۲ آبان ۱۴۰۳ — ۱۴:۳۰»."""
    return jdate(d) + digits(f" — {d:%H:%M}")


def jfull_utc(value) -> str:
    """Format a UTC timestamp (string or datetime) as local Jalali."""
    return jfull(to_local(value))


def jfull_utc_safe(value, default: str = "—") -> str:
    """Like :func:`jfull_utc`, but never raises on malformed input."""
    try:
        return jfull_utc(value)
    except (ValueError, TypeError, OverflowError):
        return default


def weekday_fa(d: dt.datetime | dt.date) -> str:
    """Return the Persian weekday name for the given date."""
    if isinstance(d, dt.datetime):
        d = d.date()
    return WEEKDAYS[d.weekday()]


def relative(value, *, fallback_to_date: bool = True) -> str:
    """A short, human-friendly relative time («۵ دقیقه پیش»).

    Very old timestamps fall back to a full Jalali date, unless
    ``fallback_to_date`` is False, in which case a coarse «N سال پیش»
    is returned instead.
    """
    when = to_local(value)
    now = dt.datetime.now()
    delta = now - when

    seconds = int(delta.total_seconds())
    if seconds < 0:
        return jfull(when)
    if seconds < 60:
        return "همین حالا"
    if seconds < 3_600:
        return digits(f"{seconds // 60} دقیقه پیش")
    if seconds < 86_400:
        return digits(f"{seconds // 3_600} ساعت پیش")
    if seconds < 604_800:
        return digits(f"{seconds // 86_400} روز پیش")
    if seconds < 2_592_000:
        return digits(f"{seconds // 604_800} هفته پیش")
    if seconds < 31_536_000:
        return digits(f"{seconds // 2_592_000} ماه پیش")
    if fallback_to_date:
        return jdate(when)
    return digits(f"{seconds // 31_536_000} سال پیش")


# -------------------------------------------------------------- parsing ----

def parse_ymd(text: str) -> dt.date | None:
    """Parse a ``YYYY-MM-DD`` string safely; return ``None`` on failure."""
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_hm(text: str) -> tuple[int, int] | None:
    """Parse an ``HH:MM`` string safely; return ``None`` on failure."""
    if not isinstance(text, str):
        return None
    try:
        parsed = dt.datetime.strptime(text.strip(), "%H:%M")
    except (ValueError, TypeError):
        return None
    return parsed.hour, parsed.minute


def combine_local(date_text: str, time_text: str) -> dt.datetime | None:
    """Combine a ``YYYY-MM-DD`` and ``HH:MM`` pair into a local datetime.

    Returns ``None`` if either component is missing or malformed. The
    result is naive, in the device's local timezone.
    """
    date = parse_ymd(date_text)
    time = parse_hm(time_text)
    if date is None or time is None:
        return None
    return dt.datetime(date.year, date.month, date.day, time[0], time[1])


def parse_iso_safe(value) -> dt.datetime | None:
    """Parse an ISO timestamp (string or datetime); never raises."""
    if isinstance(value, dt.datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------ validation ---

def is_valid_username(
    value: str,
    *,
    min_len: int = 3,
    max_len: int = 32,
    allowed_chars: str = "",
) -> bool:
    """Return True when ``value`` looks like a valid username.

    When ``allowed_chars`` is empty, any non-whitespace character is
    accepted as long as the length is within bounds. When provided, every
    character of the trimmed value must appear in that set.

    Intentionally permissive: it protects storage from pathological
    input, but does not try to be a full username validator.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if len(candidate) < min_len or len(candidate) > max_len:
        return False
    if any(ch.isspace() for ch in candidate):
        return False
    if allowed_chars:
        allowed = set(allowed_chars)
        if not all(ch in allowed for ch in candidate):
            return False
    return True


def is_future(when: dt.datetime, *, now: dt.datetime | None = None) -> bool:
    """True when ``when`` is strictly in the future relative to ``now``."""
    now = now or dt.datetime.now()
    return when > now


# -------------------------------------------------------------- sizes ------

def humanize_bytes(size: int | float) -> str:
    """Return a compact, localized size string («۱.۲ مگابایت»).

    Uses Persian digits and a 1024-based scale. Invalid or negative input
    yields «۰ B» so callers never have to special-case it.
    """
    try:
        value = float(size)
    except (TypeError, ValueError):
        value = 0.0
    if value < 0:
        value = 0.0

    units = ["B", "KB", "MB", "GB", "TB"]
    scale = 0
    while value >= 1024 and scale < len(units) - 1:
        value /= 1024.0
        scale += 1

    if scale == 0:
        rendered = str(int(value))
    else:
        rendered = f"{value:.1f}"
        if rendered.endswith(".0"):
            rendered = rendered[:-2]

    return digits(f"{rendered} {units[scale]}")


# ------------------------------------------------------------- ranges ------

def day_bounds(d: dt.datetime | dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Return the local ``[start, end)`` datetimes that bracket a day.

    Useful for filtering history or analytics by a specific day without
    fiddling with timezone conversions at the call site.
    """
    if isinstance(d, dt.datetime):
        d = d.date()
    start = dt.datetime(d.year, d.month, d.day)
    return start, start + dt.timedelta(days=1)
