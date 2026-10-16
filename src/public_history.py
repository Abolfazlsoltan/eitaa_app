"""Read recent posts from Eitaa's public web view.

Eitaa does not currently expose a documented public history endpoint. This
reader supports public username channels only and fails closed when the HTML
structure changes; it never treats a failed fetch as an empty history.
"""
from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

_BASE = "https://eitaa.com/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_POST_RE = re.compile(r"^([A-Za-z0-9_]+)/([0-9]+)$")


class PublicHistoryError(RuntimeError):
    """The public channel page could not be read or parsed."""


class _Node:
    def __init__(self, tag: str = "", attrs: dict[str, str] | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_Node | str] = []

    def text(self) -> str:
        pieces: list[str] = []
        def visit(node: _Node | str):
            if isinstance(node, str):
                pieces.append(node)
            else:
                if node.tag in ("br", "p", "div") and pieces and not pieces[-1].endswith("\n"):
                    pieces.append("\n")
                for child in node.children:
                    visit(child)
                if node.tag in ("p", "div", "br"):
                    pieces.append("\n")
        for child in self.children:
            visit(child)
        return "".join(pieces)


class _TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_public_channel_html(username: str, html_text: str) -> list[dict[str, Any]]:
    """Parse posts from the public Eitaa channel page HTML."""
    if not _CHANNEL_RE.fullmatch(username or ""):
        raise PublicHistoryError("این کانال شناسهٔ عمومی قابل‌خواندن ندارد.")
    parser = _TreeParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:
        raise PublicHistoryError("ساختار صفحهٔ کانال قابل‌خواندن نبود.") from exc

    results: dict[int, dict[str, Any]] = {}
    for node in _walk(parser.root):
        classes = _classes(node)
        if node.tag != "div" or "etme_widget_message" not in classes:
            continue
        if "etme_widget_message_wrap" in classes:
            continue
        match = _POST_RE.fullmatch(node.attrs.get("data-post", ""))
        if not match or match.group(1) != username:
            continue
        message_id = int(match.group(2))
        text = ""
        created_at = ""
        views = None
        for child in _walk(node):
            child_classes = _classes(child)
            if child.tag == "div" and "etme_widget_message_text" in child_classes:
                text = _clean(child.text())
            elif child.tag == "time" and "time" in child_classes:
                created_at = child.attrs.get("datetime", "")
            elif child.tag == "span" and "etme_widget_message_views" in child_classes:
                raw_views = child.attrs.get("data-count", "")
                if raw_views.isdigit():
                    views = int(raw_views)
        if not text and not created_at:
            continue
        results[message_id] = {
            "id": f"eitaa:{username}:{message_id}",
            "source_post_id": message_id,
            "source_url": f"{_BASE}{username}/{message_id}",
            "source": "eitaa_public",
            "is_external": True,
            "title": "",
            "text": text,
            "status": "published",
            "created_at": created_at,
            "channel_title": username,
            "channel_username": username,
            "views": views,
            "deliveries": [{
                "channel_id": None,
                "channel_title": username,
                "status": "published",
                "remote_message_id": message_id,
                "done_at": created_at,
            }],
        }
    if not results and "etme_widget_message" not in html_text:
        raise PublicHistoryError(
            "ساختار صفحهٔ عمومی ایتا قابل‌تشخیص نبود؛ احتمالاً صفحه تغییر کرده است."
        )
    return [results[key] for key in sorted(results, reverse=True)]


async def fetch_public_channel_history(
    username: str, *, limit: int = 100, max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Fetch up to 100 recent posts from a public username channel."""
    if not _CHANNEL_RE.fullmatch(username or ""):
        raise PublicHistoryError(
            "برای خواندن تاریخچهٔ عمومی، کانال باید شناسهٔ متنی داشته باشد."
        )
    limit = max(1, min(int(limit), 500))
    max_pages = max(1, min(int(max_pages), 10))
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fa,en;q=0.8",
    }
    collected: dict[int, dict[str, Any]] = {}
    before_id: int | None = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(18.0, connect=8.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            for _ in range(max_pages):
                url = urljoin(_BASE, username)
                params = {"before": before_id} if before_id else None
                response = await client.get(url, params=params)
                response.raise_for_status()
                page = parse_public_channel_html(username, response.text)
                if not page:
                    if collected:
                        break
                    raise PublicHistoryError(
                        "پستی از صفحه پیدا نشد؛ کانال ممکن است خصوصی باشد یا ساختار ایتا تغییر کرده باشد."
                    )
                previous_count = len(collected)
                for item in page:
                    collected[item["source_post_id"]] = item
                next_before = min(item["source_post_id"] for item in page)
                if len(collected) >= limit or len(collected) == previous_count:
                    break
                if before_id is not None and next_before >= before_id:
                    break
                before_id = next_before
                await asyncio.sleep(0.12)
    except PublicHistoryError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise PublicHistoryError(
            "دریافت تاریخچه از صفحهٔ عمومی ایتا ناموفق بود؛ اتصال یا دسترسی کانال را بررسی کن."
        ) from exc

    if not collected:
        raise PublicHistoryError(
            "در صفحهٔ عمومی کانال پستی پیدا نشد؛ کانال ممکن است خصوصی باشد."
        )
    return sorted(
        collected.values(),
        key=lambda item: item["source_post_id"],
        reverse=True,
    )[:limit]
