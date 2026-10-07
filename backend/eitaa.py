"""Thin adapter over EitaaYar (names per the EitaaYar API PDF supplied by the owner; sendMessage `text` and
getMe response shape still unverified against live responses). Limit: ~20 req/s per IP, so calls are throttled."""
import os, re, time, asyncio, httpx

BASE = os.getenv("EITAAYAR_BASE", "https://eitaayar.ir/api")
UNSUPPORTED = "این عملیات در API عمومی فعلی ایتایار در دسترس نیست."

class EitaaError(Exception): pass

def normalize_channel(raw: str) -> str:
    raw = raw.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?eitaa\.com/([A-Za-z0-9_]+)/?$", raw)
    if m: return m.group(1)
    return raw.lstrip("@")

def _scrub(msg: str, token: str) -> str:
    return msg.replace(token, "***")

_last = [0.0]
async def _throttle():  # EitaaYar blocks the IP above ~20 req/s; stay well below
    wait = _last[0] + 0.1 - time.monotonic()
    if wait > 0: await asyncio.sleep(wait)
    _last[0] = time.monotonic()

async def _call(token: str, method: str, files=None, **data):
    await _throttle()
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{BASE}/{token}/{method}", data=data, files=files)
        j = r.json()
    except Exception as e:
        raise EitaaError(_scrub(f"ارتباط با ایتایار برقرار نشد ({type(e).__name__})", token))
    if r.status_code != 200 or not j.get("ok", True):
        raise EitaaError(_scrub(str(j.get("description") or j.get("message") or "خطای نامشخص از ایتایار"), token))
    return j

async def get_me(token: str):
    return await _call(token, "getMe")

async def send_message(token, chat_id, text, title="", silent=False, pin=False, view_to_delete=None):
    p = dict(chat_id=chat_id, text=text)
    if title: p["title"] = title
    if silent: p["disable_notification"] = 1
    if pin: p["pin"] = 1
    if view_to_delete: p["view_to_delete"] = view_to_delete
    return await _call(token, "sendMessage", **p)

async def send_file(token, chat_id, path, filename, caption="", title="", silent=False, pin=False, view_to_delete=None):
    p = dict(chat_id=chat_id, caption=caption)
    if title: p["title"] = title
    if silent: p["disable_notification"] = 1
    if pin: p["pin"] = 1
    if view_to_delete: p["view_to_delete"] = view_to_delete
    with open(path, "rb") as f:
        return await _call(token, "sendDocument", files={"file": (filename, f)}, **p)
