"""Thin adapter over EitaaYar (names per the EitaaYar API PDF supplied by the owner; sendMessage `text` and
getMe response shape still unverified against live responses). Limit: ~20 req/s per IP, so calls are throttled."""
import os, re, time, asyncio, httpx
from urllib.parse import urlparse

BASE = os.getenv("EITAAYAR_BASE", "https://eitaayar.ir/api").rstrip("/")
UNSUPPORTED = "این عملیات در API عمومی فعلی ایتایار در دسترس نیست."

class EitaaError(Exception): pass

def markdown_to_html(value: str) -> str:
    """Convert common Markdown safely without interpreting markers inside code or links."""
    import html
    import re

    source = html.escape(str(value or ""), quote=True)
    protected = []

    def hold(value):
        protected.append(value)
        return f"\x00P{len(protected) - 1}\x00"

    source = re.sub(r"(?s)\x60\x60\x60(?:[A-Za-z0-9_+.-]+)?[ \t]*\n(.*?)\x60\x60\x60", lambda m: hold("<pre>" + m.group(1) + "</pre>"), source)
    source = re.sub(r"\x60([^\n\x60]+)\x60", lambda m: hold("<code>" + m.group(1) + "</code>"), source)

    def link(match):
        return hold('<a href="' + match.group(2) + '">' + match.group(1) + "</a>")

    source = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", link, source)
    source = re.sub(r"(?m)^#{1,6}[ \t]+(.+)$", r"<b>\1</b>", source)
    source = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: "<b>" + (m.group(1) or m.group(2)) + "</b>", source)
    source = re.sub(r"(?<!\\)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", lambda m: "<i>" + (m.group(1) or m.group(2)) + "</i>", source)
    source = re.sub(r"~~(.+?)~~", r"<s>\1</s>", source)
    source = re.sub(r"(?m)^\s*[-*+]\s+(.+)$", r"• \1", source)
    source = re.sub(r"(?m)^\s*\d+[.)]\s+(.+)$", r"\1", source)

    for index, value in enumerate(protected):
        source = source.replace(f"\x00P{index}\x00", value)
    return source




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
    p = dict(chat_id=chat_id, text=markdown_to_html(text), parse_mode="HTML")
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
    p = dict(chat_id=chat_id, caption=markdown_to_html(caption), parse_mode="HTML")
    if title: p["title"] = title
    if silent: p["disable_notification"] = 1
    if pin: p["pin"] = 1
    if view_to_delete is not None: p["view_to_delete"] = view_to_delete
    if reply_to_message_id is not None:
        p["reply_to_message_id"] = reply_to_message_id
    with open(path, "rb") as f:
        return await _call(token, "sendDocument", files={"file": (filename, f)}, **p)


async def edit_message(token: str, chat_id: str, message_id: int, text: str, *, caption: bool = False):
    """Try editing a previously sent message through the EitaaYar bot API.

    The public EitaaYar reference currently documents send/get methods only;
    these method names are attempted as a capability check and may be rejected
    by the provider. Callers must surface that failure rather than pretending
    the remote channel was updated.
    """
    method = "editMessageCaption" if caption else "editMessageText"
    field = "caption" if caption else "text"
    return await _call(
        token, method, chat_id=chat_id, message_id=int(message_id),
        **{field: markdown_to_html(text), "parse_mode": "HTML"},
    )


async def delete_message(token: str, chat_id: str, message_id: int):
    """Try deleting a previously sent message; provider support is not guaranteed."""
    return await _call(
        token, "deleteMessage", chat_id=chat_id, message_id=int(message_id)
    )
