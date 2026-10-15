"""Authentication and authorization helpers for the Eitaa Manager backend.

Responsibilities
----------------
* Password hashing (scrypt) and constant-time verification.
* Signed session tokens (HMAC-SHA256) with a bounded TTL.
* Role-based permission lookup used by the middleware in ``backend.main``.
* Telegram-compatible WebApp ``initData`` validation (kept for forward
  compatibility; the current client does not rely on it).

Everything here is intentionally dependency-light: the only imports are
the standard library, so this module can be unit-tested without pulling
in FastAPI, SQLAlchemy or the rest of the application.
"""

import os, hmac, hashlib, base64, json, time, secrets, re
from urllib.parse import parse_qsl

SECRET = os.environ["APP_SECRET_KEY"].encode()


# ==================================================================== roles

# Permission sets per role. ``owner`` is a wildcard. Every other role has a
# small, explicit set so it is obvious at a glance what each role can do.
ROLES = {
    "owner": {"*"},
    "editor": {"edit"},
    "approver": {"approve"},
    "publisher": {"publish"},
    "analyst": {"analytics"},
}

# Order used when listing roles in the UI.
ROLE_ORDER = ("owner", "editor", "approver", "publisher", "analyst")

# Persian labels for each role, shown on the team screen.
ROLE_LABELS = {
    "owner": "مالک",
    "editor": "ویرایشگر",
    "approver": "تأییدکننده",
    "publisher": "ناشر",
    "analyst": "تحلیل‌گر",
}

# Persian descriptions for each permission, shown when explaining access.
PERMISSION_LABELS = {
    "edit": "ساخت و ویرایش پیش‌نویس‌ها",
    "approve": "تأیید یا رد پست‌های در انتظار تأیید",
    "publish": "انتشار و لغو پست‌ها در کانال‌ها",
    "analytics": "مشاهده آمار و تحلیل‌ها",
}

# Every permission that a non-owner role can hold. Owners implicitly hold
# all of these through the wildcard in ``ROLES``.
ALL_PERMISSIONS = frozenset(
    perm for role, perms in ROLES.items() if role != "owner" for perm in perms
)


def can(role: str, perm: str) -> bool:
    """Return True when ``role`` is allowed to perform ``perm``."""
    p = ROLES.get(role, set())
    return "*" in p or perm in p


def role_permissions(role: str) -> list[str]:
    """Return the sorted list of explicit permissions for a role.

    For ``owner`` this returns every permission in :data:`ALL_PERMISSIONS`
    because the wildcard grants them all. Unknown roles return an empty
    list. Useful for rendering access badges in the team screen without
    leaking the internal ``{"*"}`` marker.
    """
    perms = ROLES.get(role)
    if not perms:
        return []
    if "*" in perms:
        return sorted(ALL_PERMISSIONS)
    return sorted(p for p in perms if p in ALL_PERMISSIONS)


def role_summary() -> list[dict]:
    """Return a UI-friendly description of every role.

    Each entry has ``role`` (machine name), ``label`` (Persian), and
    ``permissions`` (list of permission keys). The order follows
    :data:`ROLE_ORDER`.
    """
    return [
        {
            "role": role,
            "label": ROLE_LABELS.get(role, role),
            "permissions": role_permissions(role),
        }
        for role in ROLE_ORDER
    ]


def is_valid_role(role: str) -> bool:
    """True when ``role`` is a known role and is not ``owner``.

    ``owner`` is intentionally excluded: the owner account is created at
    registration time and cannot be granted through the team endpoint.
    """
    return isinstance(role, str) and role in ROLES and role != "owner"


# ================================================================= password

def hash_pw(pw: str) -> str:
    if not isinstance(pw, str) or not 8 <= len(pw) <= 1024:
        raise ValueError("Password length must be between 8 and 1024 characters")
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(
        pw.encode(), salt=salt, n=2**14, r=8, p=1
    ).hex()


def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        expected = hashlib.scrypt(
            pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1
        ).hex()
    except (ValueError, TypeError):
        # Malformed stored hash — treat as a failed verification instead of
        # letting the exception bubble up to the middleware.
        return False
    return hmac.compare_digest(expected, h)


# ==================================================================== token

def make_token(uid: int, ttl: int = 7 * 86400) -> str:
    if not isinstance(uid, int) or uid <= 0:
        raise ValueError("User id must be a positive integer")
    ttl = min(max(int(ttl), 60), 30 * 86400)
    b = base64.urlsafe_b64encode(
        json.dumps({"u": uid, "e": int(time.time()) + ttl}).encode()
    ).decode()
    return b + "." + hmac.new(SECRET, b.encode(), "sha256").hexdigest()


def read_token(t: str):
    if not isinstance(t, str) or len(t) > 2048:
        return None
    try:
        b, sig = t.split(".", 1)
        if not hmac.compare_digest(
            hmac.new(SECRET, b.encode(), "sha256").hexdigest(), sig
        ):
            return None
        d = json.loads(base64.urlsafe_b64decode(b))
        if not isinstance(d, dict):
            return None
        uid, expires = d.get("u"), d.get("e")
        if not isinstance(uid, int) or uid <= 0:
            return None
        if not isinstance(expires, (int, float)) or expires <= time.time():
            return None
        return uid
    except Exception:
        return None


# ============================================== Eitaa / Telegram WebApp data

def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    """Telegram-compatible WebApp initData check (HMAC 'WebAppData').

    Verify the scheme against Eitaa's official WebApp docs before enabling
    in production. Returns the user dict on success or ``None`` on any
    failure (missing signature, wrong signature, expired, malformed).
    """
    if (not isinstance(init_data, str) or len(init_data) > 8192
            or not isinstance(bot_token, str) or not bot_token):
        return None
    try:
        items = parse_qsl(init_data, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        return None
    if len({key for key, _ in items}) != len(items):
        return None
    pairs = dict(items)
    h = pairs.pop("hash", None)
    if not h or not re.fullmatch(r"[0-9a-fA-F]{64}", h):
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(
        hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), h
    ):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except (TypeError, ValueError):
        return None
    now = time.time()
    if auth_date <= 0 or auth_date > now + 60 or now - auth_date > max_age:
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
        return user if isinstance(user, dict) else None
    except (TypeError, ValueError):
        return None
