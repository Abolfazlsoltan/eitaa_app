"""Thin adapter over EitaaYar (names per the EitaaYar API PDF supplied by the owner; sendMessage `text` and
getMe response shape still unverified against live responses). Limit: ~20 req/s per IP, so calls are throttled."""
import os, re, time, asyncio, httpx
from urllib.parse import urlparse

BASE = os.getenv("EITAAYAR_BASE", "https://eitaayar.ir/api").rstrip("/")
UNSUPPORTED = "این عملیات در API عمومی فعلی ایتایار در دسترس نیست."

class EitaaError(Exception): pass

def normalize_channel(raw: str) -> str:
    raw = str(raw or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?eitaa\.com/([A-Za-z0-9_]+)/?$", raw)
    if m:
        return m.group(1)
    value = raw.lstrip("@")
    return value if re.fullmatch(r"[A-Za-z0-9_+-]{1,200}", value) else ""

def _scrub(msg: str, token: str) -> str:
    return msg.replace(token, "***")

_last = [0.0]
async def _throttle():  # EitaaYar blocks the IP above ~20 req/s; stay well below
    wait = _last[0] + 0.1 - time.monotonic()
    if wait > 0: await asyncio.sleep(wait)
    _last[0] = time.monotonic()

async def _call(token: str, method: str, files=None, **data):
    token = str(token or "").strip()
    if (not token or any(ch in token for ch in "/?#\r\n\\")
            or not re.fullmatch(r"[A-Za-z0-9:_-]{8,256}", token)):
        raise EitaaError("توکن اتصال ایتایار معتبر نیست.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", method):
        raise EitaaError("عملیات درخواستی ایتایار معتبر نیست.")
    parsed = urlparse(BASE)
    if parsed.scheme != "https" or not parsed.hostname:
        raise EitaaError("نشانی سرویس ایتایار باید HTTPS معتبر باشد.")
    await _throttle()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as c:
            r = await c.post(f"{BASE}/{token}/{method}", data=data, files=files)
        j = r.json()
    except Exception as e:
        raise EitaaError(_scrub(f"ارتباط با ایتایار برقرار نشد ({type(e).__name__})", token))
    if r.status_code != 200 or not isinstance(j, dict) or j.get("ok") is not True:
        detail = (
            j.get("description") or j.get("message") or "خطای نامشخص از ایتایار"
            if isinstance(j, dict) else "پاسخ نامعتبر از ایتایار"
        )
        raise EitaaError(_scrub(str(detail), token))
    return j

async def get_me(token: str):
    return await _call(token, "getMe")

async def send_message(
    token, chat_id, text, title="", silent=False, pin=False,
    view_to_delete=None, reply_to_message_id=None,
):
    p = dict(chat_id=chat_id, text=text)
    if title: p["title"] = title
    if silent: p["disable_notification"] = 1
    if pin: p["pin"] = 1
    if view_to_delete is not None: p["view_to_delete"] = view_to_delete
    if reply_to_message_id is not None:
        p["reply_to_message_id"] = reply_to_message_id
    return await _call(token, "sendMessage", **p)

async def send_file(
    token, chat_id, path, filename, caption="", title="", silent=False,
    pin=False, view_to_delete=None, reply_to_message_id=None,
):
    p = dict(chat_id=chat_id, caption=caption)
    if title: p["title"] = title
    if silent: p["disable_notification"] = 1
    if pin: p["pin"] = 1
    if view_to_delete is not None: p["view_to_delete"] = view_to_delete
    if reply_to_message_id is not None:
        p["reply_to_message_id"] = reply_to_message_id
    with open(path, "rb") as f:
        return await _call(token, "sendDocument", files={"file": (filename, f)}, **p)
