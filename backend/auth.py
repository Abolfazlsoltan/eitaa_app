import os, hmac, hashlib, base64, json, time, secrets
from urllib.parse import parse_qsl

SECRET = os.environ["APP_SECRET_KEY"].encode()
ROLES = {"owner": {"*"}, "editor": {"edit"}, "approver": {"approve"}, "publisher": {"publish"}, "analyst": {"analytics"}}
def can(role: str, perm: str) -> bool:
    p = ROLES.get(role, set()); return "*" in p or perm in p

def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()
def verify_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split(":")
    return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), h)

def make_token(uid: int, ttl: int = 7 * 86400) -> str:
    b = base64.urlsafe_b64encode(json.dumps({"u": uid, "e": int(time.time()) + ttl}).encode()).decode()
    return b + "." + hmac.new(SECRET, b.encode(), "sha256").hexdigest()
def read_token(t: str):
    try:
        b, sig = t.split(".")
        if not hmac.compare_digest(hmac.new(SECRET, b.encode(), "sha256").hexdigest(), sig): return None
        d = json.loads(base64.urlsafe_b64decode(b))
        return d["u"] if d["e"] > time.time() else None
    except Exception: return None

def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    """Telegram-compatible WebApp initData check (HMAC 'WebAppData'). Verify the scheme against
    Eitaa's official WebApp docs before enabling in production. Returns the user dict or None."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True)); h = pairs.pop("hash", None)
    if not h: return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), h): return None
    if time.time() - int(pairs.get("auth_date", "0")) > max_age: return None
    try: return json.loads(pairs.get("user", "{}"))
    except Exception: return None
