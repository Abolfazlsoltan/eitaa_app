"""FastAPI backend for the Eitaa Manager.

The client app works standalone on the device. This backend is optional
and becomes useful when you want to sync data across multiple devices,
run the scheduler 24/7, or manage a team.

Start with:
    uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import (
    FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete, func, select, update

from . import ai, analytics, auth, eitaa
from .db import (
    ActivityLog,
    AIUsage,
    Automation,
    Channel,
    Connection,
    Delivery,
    Feedback,
    MediaAsset,
    Post,
    PostRevision,
    RemovalReminder,
    Session,
    Template,
    User,
    Workspace,
    fernet,
    init,
    now,
)


# ================================================================ constants

MAX_ATTEMPTS = 5
MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
os.makedirs(MEDIA_DIR, exist_ok=True)

# Iran Standard Time offset from UTC (minutes). Overridable per deployment.
TZ = dt.timedelta(minutes=int(os.getenv("TZ_OFFSET_MIN", "210")))


# ================================================================== helpers

def log(s, kind: str, message: str) -> None:
    s.add(ActivityLog(kind=kind, message=message))


def _bot_name(get_me_response) -> str:
    result = (get_me_response or {}).get("result") or {}
    return str(
        result.get("first_name")
        or result.get("username")
        or result.get("title")
        or "ربات ایتا"
    )


# =================================================================== worker

async def worker() -> None:
    """Background loop: promote due deliveries, publish them, run automations."""
    while True:
        try:
            with Session() as s:
                for d in s.scalars(select(Delivery).where(
                    Delivery.status == "scheduled",
                    Delivery.run_at <= now(),
                )).all():
                    d.status = "queued"

                _promote_due_reminders(s)

                due_ids = s.scalars(
                    select(Delivery.id)
                    .where(Delivery.status == "queued", Delivery.run_at <= now())
                    .limit(20)
                ).all()
                s.commit()

            for did in due_ids:
                await deliver(did)

            await run_automations()
        except Exception as e:
            print("worker error:", type(e).__name__, e)
        await asyncio.sleep(5)


def _promote_due_reminders(s, at: dt.datetime | None = None) -> int:
    at = at or now()
    reminder_ids = s.scalars(
        select(RemovalReminder.id).where(
            RemovalReminder.status == "scheduled",
            RemovalReminder.due_at <= at,
        )
    ).all()
    promoted = 0
    for reminder_id in reminder_ids:
        claimed = s.execute(
            update(RemovalReminder)
            .where(
                RemovalReminder.id == reminder_id,
                RemovalReminder.status == "scheduled",
                RemovalReminder.due_at <= at,
            )
            .values(status="due", updated_at=at)
        ).rowcount
        if not claimed:
            continue
        reminder = s.get(RemovalReminder, reminder_id)
        log(
            s,
            "removal_reminder",
            f"یادآوری بررسی پیام {reminder.remote_message_id} "
            "موعدش رسیده است؛ حذف خودکار توسط API پشتیبانی نمی‌شود.",
        )
        promoted += 1
    return promoted


async def deliver(delivery_id: int) -> None:
    """Publish a single queued delivery. Atomic claim prevents double sends."""
    with Session() as s:
        claimed = s.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id, Delivery.status == "queued")
            .values(status="sending")
        ).rowcount
        s.commit()
        if not claimed:
            return

        d = s.get(Delivery, delivery_id)
        p = s.get(Post, d.post_id)
        ch = s.get(Channel, d.channel_id)
        token = fernet.decrypt(
            s.get(Connection, ch.connection_id).token_enc.encode()
        ).decode()

        try:
            m = s.get(MediaAsset, p.media_id) if p.media_id else None
            if m:
                response = await eitaa.send_file(
                    token, ch.chat_id,
                    os.path.join(MEDIA_DIR, m.stored_name), m.filename,
                    _post_content(p), p.title, p.silent, p.pin, p.view_to_delete,
                    p.reply_to_message_id,
                )
            else:
                response = await eitaa.send_message(
                    token, ch.chat_id, _post_content(p), p.title,
                    p.silent, p.pin, p.view_to_delete,
                    p.reply_to_message_id,
                )
            result = response.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            d.remote_message_id = (
                message_id if isinstance(message_id, int)
                and not isinstance(message_id, bool) and message_id > 0
                else None
            )
            d.status, d.error, d.done_at = "published", "", now()
            log(s, "publish", f"«{p.title or p.id}» در {ch.title} منتشر شد")
        except eitaa.EitaaError as e:
            d.attempts += 1
            d.error = str(e)
            if d.attempts >= MAX_ATTEMPTS:
                d.status, d.done_at = "failed", now()
            else:
                d.status = "queued"
                d.run_at = now() + dt.timedelta(seconds=30 * (2 ** d.attempts))
            log(s, "error", f"ارسال به {ch.title} انجام نشد: {e}")
        s.commit()
        _refresh_post(s, p.id)
        s.commit()


def _post_content(post: Post) -> str:
    body = (post.text or "").strip()
    signature = (post.signature or "").strip()
    return f"{body}\n\n{signature}" if signature else body


def _refresh_post(s, post_id: int) -> None:
    p = s.get(Post, post_id)
    statuses = {
        d.status
        for d in s.scalars(select(Delivery).where(Delivery.post_id == post_id))
    }
    if p.status == "cancelled" or not statuses:
        return
    if statuses == {"pending"}:
        p.status = "pending"
        return
    if statuses & {"sending", "queued"}:
        p.status = "sending"
    elif "scheduled" in statuses:
        p.status = "scheduled"
    elif statuses == {"published"}:
        p.status = "published"
    else:
        p.status = "failed"


# ============================================================ default data

DEFAULT_TEMPLATES = [
    ("خبر فوری", "🔴 فوری | {{title}}\n\n{{summary}}\n\n{{link}}\n{{hashtags}}"),
    ("خبر", "📰 {{title}}\n\n{{summary}}\n\n📅 {{date}}\n{{link}}\n{{hashtags}}"),
    ("معرفی محصول", "✨ {{title}}\n\n{{summary}}\n\n🔗 {{link}}\n{{hashtags}}"),
    ("معرفی اپلیکیشن", "📱 {{title}}\n\n{{summary}}\n\n⬇️ دانلود: {{link}}\n{{hashtags}}"),
    ("آموزشی", "💡 {{title}}\n\n{{summary}}\n\n{{hashtags}}"),
    ("اطلاعیه", "📢 اطلاعیه\n\n{{title}}\n{{summary}}\n\n📅 {{date}}"),
    ("تبلیغ", "🔥 {{title}}\n\n{{summary}}\n\n👉 {{link}}\n{{hashtags}}"),
    ("معرفی بازی", "🎮 {{title}}\n\n{{summary}}\n\n⬇️ {{link}}\n{{hashtags}}"),
]


def seed_templates() -> None:
    with Session() as s:
        if s.scalar(select(func.count()).select_from(Template)):
            return
        for name, body in DEFAULT_TEMPLATES:
            s.add(Template(name=name, category=name, body=body, builtin=True))
        s.commit()


# ================================================================= lifespan

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init()
    seed_templates()
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Eitaa Manager", lifespan=lifespan)


# ================================================================= schemas

class ConnIn(BaseModel):
    name: str = "اتصال اصلی"
    token: str


class ChanIn(BaseModel):
    connection_id: int
    value: str
    title: str = ""


class PostIn(BaseModel):
    title: str = ""
    text: str
    signature: str = ""
    channel_ids: list[int] = Field(default_factory=list)
    mode: Literal["draft", "now", "schedule"] = "draft"
    scheduled_at: dt.datetime | None = None
    silent: bool = False
    pin: bool = False
    view_to_delete: int | None = Field(default=None, ge=1)
    reply_to_message_id: int | None = Field(default=None, ge=1)
    media_id: int | None = None
    repeat: Literal["none", "daily", "weekly"] = "none"
    repeat_count: int = Field(default=1, ge=1, le=30)


class PostEditIn(BaseModel):
    title: str = ""
    text: str
    signature: str = ""
    channel_ids: list[int] = Field(default_factory=list)
    mode: Literal["draft", "now", "schedule"] = "draft"
    scheduled_at: dt.datetime | None = None
    silent: bool = False
    pin: bool = False
    view_to_delete: int | None = Field(default=None, ge=1)
    reply_to_message_id: int | None = Field(default=None, ge=1)
    media_id: int | None = None


class FeedbackIn(BaseModel):
    category: Literal["suggestion", "complaint", "bug"]
    message: str = Field(min_length=5, max_length=4000)


class FeedbackUpdateIn(BaseModel):
    status: Literal["open", "in_progress", "resolved"]
    reply: str = Field(default="", max_length=4000)


class RemovalReminderIn(BaseModel):
    delivery_id: int = Field(ge=1)
    due_at: dt.datetime


class RemovalReminderTimeIn(BaseModel):
    due_at: dt.datetime


class AIIn(BaseModel):
    action: str = Field(min_length=1, max_length=30)
    text: str = Field(default="", max_length=20_000)
    prompt: str = Field(default="", max_length=4_000)
    language: Literal["fa", "en", "ar"] = "fa"
    tone: Literal["neutral", "formal", "friendly", "news"] = "neutral"
    length: Literal["short", "medium", "long"] = "medium"


class TplIn(BaseModel):
    name: str
    category: str = "سفارشی"
    body: str


class ReIn(BaseModel):
    scheduled_at: dt.datetime


class TagsIn(BaseModel):
    tags: list[str]


class AutoIn(BaseModel):
    name: str
    trigger: str = "daily"
    at_time: str = "09:00"
    weekdays: list[int] = []
    interval_min: int = 60
    template_id: int | None = None
    body: str = ""
    use_ai: bool = False
    ai_action: str = ""
    channel_ids: list[int]
    require_approval: bool = True


class RegIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=100)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=1024)


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=1024)
    role: str = "editor"
    display_name: str = Field(default="", max_length=100)
    eitaa_id: str | None = Field(default=None, max_length=40)


# ============================================================= connections

@app.post("/api/connections")
async def add_conn(b: ConnIn):
    try:
        me = await eitaa.get_me(b.token.strip())
    except eitaa.EitaaError as e:
        raise HTTPException(400, str(e))
    with Session() as s:
        c = Connection(
            name=b.name,
            token_enc=fernet.encrypt(b.token.strip().encode()).decode(),
            bot_name=_bot_name(me),
        )
        s.add(c)
        log(s, "connect", "اتصال ایتا برقرار شد")
        s.commit()
        return {"id": c.id, "name": c.name, "bot": c.bot_name}


@app.get("/api/connections")
def conns():
    with Session() as s:
        return [
            {"id": c.id, "name": c.name, "bot": c.bot_name}
            for c in s.scalars(select(Connection))
        ]


# ================================================================ channels

@app.post("/api/channels")
def add_channel(b: ChanIn):
    cid = eitaa.normalize_channel(b.value)
    if not cid:
        raise HTTPException(400, "شناسه کانال معتبر نیست.")
    with Session() as s:
        if not s.get(Connection, b.connection_id):
            raise HTTPException(404, "اتصال پیدا نشد.")
        ch = Channel(connection_id=b.connection_id,
                     title=b.title or cid, chat_id=cid)
        s.add(ch)
        log(s, "channel", f"کانال {ch.title} اضافه شد")
        s.commit()
        return {"id": ch.id, "title": ch.title, "chat_id": ch.chat_id}


@app.get("/api/channels")
def channels():
    with Session() as s:
        return [
            {"id": c.id, "title": c.title, "chat_id": c.chat_id}
            for c in s.scalars(select(Channel))
        ]


# =================================================================== posts

def _create_post(b: PostIn) -> dict:
    if not b.text.strip():
        raise HTTPException(400, "متن پست خالی است.")
    if b.mode != "draft" and not b.channel_ids:
        raise HTTPException(400, "حداقل یک کانال انتخاب کنید.")
    if b.mode == "schedule" and not b.scheduled_at:
        raise HTTPException(400, "زمان انتشار را مشخص کنید.")
    if len(b.channel_ids) != len(set(b.channel_ids)):
        raise HTTPException(400, "کانال تکراری انتخاب شده است.")

    scheduled_at = b.scheduled_at
    if b.mode == "schedule" and scheduled_at:
        if scheduled_at.tzinfo is not None:
            scheduled_at = scheduled_at.astimezone(dt.timezone.utc).replace(
                tzinfo=None
            )
        if scheduled_at <= now():
            raise HTTPException(400, "زمان انتشار باید در آینده باشد.")
        if scheduled_at > now() + dt.timedelta(days=30):
            raise HTTPException(400, "زمان‌بندی فقط تا ۳۰ روز آینده مجاز است.")
    if b.mode == "schedule" and b.repeat in ("daily", "weekly"):
        last_occurrence = scheduled_at + dt.timedelta(
            days=(1 if b.repeat == "daily" else 7)
            * (min(max(b.repeat_count, 1), 30) - 1)
        )
        if last_occurrence > now() + dt.timedelta(days=30):
            raise HTTPException(400, "تمام نوبت‌های تکرار باید در ۳۰ روز آینده باشند.")

    with Session() as s:
        if b.channel_ids:
            channel_ids = set(b.channel_ids)
            available_ids = set(s.scalars(
                select(Channel.id).where(Channel.id.in_(channel_ids))
            ))
            if available_ids != channel_ids:
                raise HTTPException(400, "یکی از کانال‌های انتخاب‌شده پیدا نشد.")
        if b.media_id is not None and not s.get(MediaAsset, b.media_id):
            raise HTTPException(404, "فایل رسانه‌ای پیدا نشد.")
        if b.view_to_delete is not None and b.view_to_delete < 1:
            raise HTTPException(400, "تعداد بازدید برای حذف باید حداقل ۱ باشد.")

        p = Post(
            title=b.title,
            text=b.text,
            signature=b.signature.strip(),
            silent=b.silent,
            pin=b.pin,
            view_to_delete=b.view_to_delete,
            reply_to_message_id=b.reply_to_message_id,
            media_id=b.media_id,
            scheduled_at=scheduled_at,
            status={"draft": "draft", "now": "queued",
                    "schedule": "scheduled"}[b.mode],
        )
        s.add(p)
        s.flush()

        at = scheduled_at if b.mode == "schedule" else now()
        for cid in b.channel_ids:
            s.add(Delivery(
                post_id=p.id, channel_id=cid,
                idem_key=f"{p.id}:{cid}", run_at=at,
                status=(
                    "draft" if b.mode == "draft"
                    else "scheduled" if b.mode == "schedule"
                    else "queued"
                ),
            ))

        log(s, "post", f"پست «{b.title or p.id}» ثبت شد ({b.mode})")
        s.commit()
        return {"id": p.id, "status": p.status}


@app.post("/api/posts")
def create_post(b: PostIn, request: Request):
    if not auth.can(request.state.user.role,
                    "edit" if b.mode == "draft" else "publish"):
        raise HTTPException(403, "دسترسی لازم برای این عملیات را ندارید.")

    res = _create_post(b)

    if b.mode == "schedule" and b.repeat in ("daily", "weekly") and b.scheduled_at:
        step = dt.timedelta(days=1 if b.repeat == "daily" else 7)
        for i in range(1, min(max(b.repeat_count, 1), 30)):
            _create_post(b.model_copy(update={
                "scheduled_at": b.scheduled_at + step * i,
                "repeat": "none",
            }))
    return res


def _post_record(s, p: Post) -> dict:
    deliveries = s.scalars(
        select(Delivery).where(Delivery.post_id == p.id)
    ).all()
    return {
        "id": p.id,
        "title": p.title,
        "text": p.text,
        "signature": p.signature,
        "status": p.status,
        "scheduled_at": p.scheduled_at,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "silent": p.silent,
        "pin": p.pin,
        "media_id": p.media_id,
        "view_to_delete": p.view_to_delete,
        "reply_to_message_id": p.reply_to_message_id,
        "deliveries": [
            {
                "id": d.id,
                "channel_id": d.channel_id,
                "status": d.status,
                "error": d.error,
                "done_at": d.done_at,
                "remote_message_id": d.remote_message_id,
            }
            for d in deliveries
        ],
    }


@app.get("/api/posts")
def posts(status: str | None = None):
    with Session() as s:
        q = select(Post).order_by(Post.id.desc())
        if status:
            q = q.where(Post.status == status)
        return [_post_record(s, p) for p in s.scalars(q)]


@app.get("/api/history")
def history(
    q: str = "",
    status: str = "published",
    channel_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    with Session() as s:
        statement = (
            select(Post)
            .join(Delivery, Delivery.post_id == Post.id)
            .join(Channel, Channel.id == Delivery.channel_id)
            .order_by(Post.id.desc())
            .distinct()
        )
        if status and status != "all":
            statement = statement.where(Delivery.status == status)
        if channel_id is not None:
            statement = statement.where(Delivery.channel_id == channel_id)
        needle = q.strip()
        if needle:
            statement = statement.where(
                Post.title.ilike(f"%{needle}%")
                | Post.text.ilike(f"%{needle}%")
                | Channel.title.ilike(f"%{needle}%")
                | Channel.chat_id.ilike(f"%{needle}%")
            )
        statement = statement.offset(offset).limit(limit)
        return [_post_record(s, p) for p in s.scalars(statement)]


@app.get("/api/posts/{pid}/revisions")
def post_revisions(pid: int):
    with Session() as s:
        if not s.get(Post, pid):
            raise HTTPException(404, "پست پیدا نشد.")
        revisions = s.scalars(
            select(PostRevision)
            .where(PostRevision.post_id == pid)
            .order_by(PostRevision.id.desc())
        ).all()
        return [{
            "id": revision.id,
            "title": revision.title,
            "text": revision.text,
            "signature": revision.signature,
            "created_at": revision.created_at,
        } for revision in revisions]


@app.put("/api/posts/{pid}")
def edit_post(pid: int, b: PostEditIn, request: Request):
    if not auth.can(request.state.user.role, "edit"):
        raise HTTPException(403, "دسترسی لازم برای ویرایش را ندارید.")
    if b.mode != "draft" and not auth.can(request.state.user.role, "publish"):
        raise HTTPException(403, "دسترسی لازم برای انتشار را ندارید.")
    if not b.text.strip():
        raise HTTPException(400, "متن پست خالی است.")
    if len(b.channel_ids) != len(set(b.channel_ids)):
        raise HTTPException(400, "کانال تکراری انتخاب شده است.")
    if b.mode != "draft" and not b.channel_ids:
        raise HTTPException(400, "حداقل یک کانال انتخاب کنید.")
    if b.mode == "schedule" and not b.scheduled_at:
        raise HTTPException(400, "زمان انتشار را مشخص کنید.")
    scheduled_at = b.scheduled_at
    if b.mode == "schedule" and scheduled_at:
        if scheduled_at.tzinfo is not None:
            scheduled_at = scheduled_at.astimezone(dt.timezone.utc).replace(
                tzinfo=None
            )
        if scheduled_at <= now():
            raise HTTPException(400, "زمان انتشار باید در آینده باشد.")
        if scheduled_at > now() + dt.timedelta(days=30):
            raise HTTPException(400, "زمان‌بندی فقط تا ۳۰ روز آینده مجاز است.")
    with Session() as s:
        if b.channel_ids:
            channel_ids = set(b.channel_ids)
            available_ids = set(s.scalars(
                select(Channel.id).where(Channel.id.in_(channel_ids))
            ))
            if available_ids != channel_ids:
                raise HTTPException(400, "یکی از کانال‌های انتخاب‌شده پیدا نشد.")
        if b.media_id is not None and not s.get(MediaAsset, b.media_id):
            raise HTTPException(404, "فایل رسانه‌ای پیدا نشد.")
        p = s.get(Post, pid)
        if not p:
            raise HTTPException(404, "پست پیدا نشد.")
        if p.status != "draft":
            raise HTTPException(400, "فقط پیش‌نویس را می‌توان ویرایش کرد.")
        if (
            p.title != b.title.strip()
            or p.text != b.text.strip()
            or (p.signature or "") != b.signature.strip()
        ):
            s.add(PostRevision(
                post_id=p.id,
                title=p.title,
                text=p.text,
                signature=p.signature or "",
            ))
        p.title = b.title.strip()
        p.text = b.text.strip()
        p.signature = b.signature.strip()
        p.updated_at = now()
        p.silent = b.silent
        p.pin = b.pin
        p.view_to_delete = b.view_to_delete
        p.reply_to_message_id = b.reply_to_message_id
        p.media_id = b.media_id
        p.status = {
            "draft": "draft",
            "now": "queued",
            "schedule": "scheduled",
        }[b.mode]
        p.scheduled_at = scheduled_at if b.mode == "schedule" else None
        s.execute(sql_delete(Delivery).where(Delivery.post_id == pid))
        run_at = scheduled_at if b.mode == "schedule" else now()
        for cid in b.channel_ids:
            s.add(Delivery(
                post_id=pid,
                channel_id=cid,
                idem_key=f"{pid}:{cid}",
                run_at=run_at,
                status=(
                    "draft" if b.mode == "draft"
                    else "scheduled" if b.mode == "schedule"
                    else "queued"
                ),
            ))
        log(s, "post", f"پست {pid} به‌روزرسانی شد")
        s.commit()
        return {"id": p.id, "status": p.status}


@app.delete("/api/posts/{pid}")
def delete_post(pid: int, request: Request):
    if not auth.can(request.state.user.role, "edit"):
        raise HTTPException(403, "دسترسی لازم برای حذف را ندارید.")
    with Session() as s:
        p = s.get(Post, pid)
        if not p:
            raise HTTPException(404, "پست پیدا نشد.")
        if p.status != "draft":
            raise HTTPException(
                400, "حذف از کانال یا سوابق پست منتشرشده از این API پشتیبانی نمی‌شود."
            )
        s.execute(sql_delete(Delivery).where(Delivery.post_id == pid))
        s.delete(p)
        log(s, "post", f"پیش‌نویس {pid} حذف شد")
        s.commit()
        return {"ok": True}


@app.post("/api/deliveries/{did}/retry")
def retry(did: int):
    with Session() as s:
        d = s.get(Delivery, did)
        if not d or d.status != "failed":
            raise HTTPException(400, "فقط ارسال ناموفق قابل تلاش دوباره است.")
        d.status, d.attempts, d.run_at = "queued", 0, now()
        s.commit()
        return {"ok": True}


@app.post("/api/posts/{pid}/cancel")
def cancel(pid: int):
    with Session() as s:
        s.execute(
            update(Delivery)
            .where(Delivery.post_id == pid,
                   Delivery.status.in_(["scheduled", "queued", "pending"]))
            .values(status="cancelled")
        )
        p = s.get(Post, pid)
        if p:
            p.status = "cancelled"
        log(s, "cancel", f"پست {pid} لغو شد")
        s.commit()
        return {"ok": True}


@app.post("/api/posts/{pid}/reschedule")
def reschedule(pid: int, b: ReIn):
    with Session() as s:
        p = s.get(Post, pid)
        if not p or p.status != "scheduled":
            raise HTTPException(400, "فقط پست زمان‌بندی‌شده قابل جابجایی است.")
        if b.scheduled_at < now():
            raise HTTPException(400, "زمان جدید گذشته است.")
        p.scheduled_at = b.scheduled_at
        s.execute(
            update(Delivery)
            .where(Delivery.post_id == pid, Delivery.status == "scheduled")
            .values(run_at=b.scheduled_at)
        )
        log(s, "reschedule", f"زمان پست {pid} تغییر کرد")
        s.commit()
        return {"ok": True}


@app.post("/api/posts/{pid}/approve")
def approve(pid: int):
    with Session() as s:
        p = s.get(Post, pid)
        if not p or p.status != "pending":
            raise HTTPException(400, "این پست در انتظار تأیید نیست.")
        s.execute(
            update(Delivery)
            .where(Delivery.post_id == pid, Delivery.status == "pending")
            .values(status="queued", run_at=now())
        )
        p.status = "queued"
        log(s, "approve", f"پست {pid} تأیید شد")
        s.commit()
        return {"ok": True}


@app.post("/api/posts/{pid}/reject")
def reject(pid: int):
    with Session() as s:
        p = s.get(Post, pid)
        if not p or p.status != "pending":
            raise HTTPException(400, "این پست در انتظار تأیید نیست.")
        s.execute(
            update(Delivery)
            .where(Delivery.post_id == pid, Delivery.status == "pending")
            .values(status="cancelled")
        )
        p.status = "cancelled"
        log(s, "reject", f"پست {pid} رد شد")
        s.commit()
        return {"ok": True}


# =================================================================== stats

@app.get("/api/stats")
def stats():
    with Session() as s:
        c = lambda *st: s.scalar(
            select(func.count()).select_from(Delivery).where(
                Delivery.status.in_(st))
        ) or 0
        reminder_count = s.scalar(
            select(func.count()).select_from(RemovalReminder).where(
                RemovalReminder.status == "scheduled"
            )
        ) or 0
        return {
            "published": c("published"),
            "failed": c("failed"),
            "scheduled": c("scheduled"),
            "queued": c("queued", "sending"),
            "ai_calls": s.scalar(select(func.count()).select_from(AIUsage)) or 0,
            "channels": s.scalar(
                select(func.count()).select_from(Channel)
            ) or 0,
            "drafts": s.scalar(
                select(func.count()).select_from(Post).where(
                    Post.status == "draft"
                )
            ) or 0,
            "removal_reminders": reminder_count,
        }


def _reminder_record(s, reminder: RemovalReminder) -> dict:
    channel = s.get(Channel, reminder.channel_id)
    return {
        "id": reminder.id,
        "delivery_id": reminder.delivery_id,
        "remote_message_id": reminder.remote_message_id,
        "channel_id": reminder.channel_id,
        "channel_title": channel.title if channel else "",
        "due_at": reminder.due_at,
        "status": reminder.status,
        "created_at": reminder.created_at,
        "updated_at": reminder.updated_at,
    }


@app.get("/api/removal-reminders")
def removal_reminders(status: str = "all"):
    with Session() as s:
        query = select(RemovalReminder).order_by(RemovalReminder.due_at)
        if status != "all":
            query = query.where(RemovalReminder.status == status)
        return [
            _reminder_record(s, item)
            for item in s.scalars(query.limit(500))
        ]


@app.post("/api/removal-reminders")
def create_removal_reminder(b: RemovalReminderIn):
    due_at = b.due_at
    if due_at.tzinfo is not None:
        due_at = due_at.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if due_at <= now():
        raise HTTPException(400, "زمان یادآوری باید در آینده باشد.")
    with Session() as s:
        delivery = s.get(Delivery, b.delivery_id)
        if (
            not delivery or delivery.status != "published"
            or not delivery.remote_message_id
        ):
            raise HTTPException(
                400,
                "فقط پیام منتشرشده با شناسه پیام ثبت‌شده قابل یادآوری است.",
            )
        existing = s.scalar(select(RemovalReminder).where(
            RemovalReminder.delivery_id == delivery.id,
            RemovalReminder.status == "scheduled",
        ))
        if existing:
            raise HTTPException(
                409, "برای این پیام یادآوری فعال وجود دارد."
            )
        reminder = RemovalReminder(
            delivery_id=delivery.id,
            remote_message_id=delivery.remote_message_id,
            channel_id=delivery.channel_id,
            due_at=due_at,
        )
        s.add(reminder)
        log(
            s, "removal_reminder",
            f"یادآوری دستی برای پیام {delivery.remote_message_id} ثبت شد",
        )
        s.commit()
        return _reminder_record(s, reminder)


@app.post("/api/removal-reminders/{reminder_id}/reschedule")
def reschedule_removal_reminder(
    reminder_id: int, b: RemovalReminderTimeIn
):
    due_at = b.due_at
    if due_at.tzinfo is not None:
        due_at = due_at.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if due_at <= now():
        raise HTTPException(400, "زمان یادآوری جدید باید در آینده باشد.")
    with Session() as s:
        reminder = s.get(RemovalReminder, reminder_id)
        if not reminder or reminder.status == "cancelled":
            raise HTTPException(404, "یادآوری فعالی پیدا نشد.")
        reminder.due_at = due_at
        reminder.status = "scheduled"
        reminder.updated_at = now()
        log(s, "removal_reminder", f"زمان یادآوری {reminder_id} تغییر کرد")
        s.commit()
        return _reminder_record(s, reminder)


@app.post("/api/removal-reminders/{reminder_id}/cancel")
def cancel_removal_reminder(reminder_id: int):
    with Session() as s:
        reminder = s.get(RemovalReminder, reminder_id)
        if not reminder or reminder.status == "cancelled":
            raise HTTPException(404, "یادآوری فعالی پیدا نشد.")
        reminder.status = "cancelled"
        reminder.updated_at = now()
        log(s, "removal_reminder", f"یادآوری {reminder_id} لغو شد")
        s.commit()
        return _reminder_record(s, reminder)


def _feedback_record(feedback: Feedback) -> dict:
    return {
        "ticket": feedback.ticket,
        "category": feedback.category,
        "message": feedback.message,
        "status": feedback.status,
        "reply": feedback.reply,
        "created_at": feedback.created_at,
        "updated_at": feedback.updated_at,
    }


@app.post("/api/feedback")
def submit_feedback(b: FeedbackIn):
    ticket = f"FB-{uuid.uuid4().hex[:16].upper()}"
    with Session() as s:
        feedback = Feedback(
            ticket=ticket,
            category=b.category,
            message=b.message.strip(),
        )
        s.add(feedback)
        log(s, "feedback", f"بازخورد ناشناس با شماره پیگیری {ticket} ثبت شد")
        s.commit()
        return {"ticket": feedback.ticket, "status": feedback.status}


@app.get("/api/feedback")
def get_feedback(ticket: str):
    with Session() as s:
        feedback = s.scalar(
            select(Feedback).where(Feedback.ticket == ticket.strip().upper())
        )
        if not feedback:
            raise HTTPException(404, "شماره پیگیری پیدا نشد.")
        return {
            "ticket": feedback.ticket,
            "status": feedback.status,
            "reply": feedback.reply,
            "updated_at": feedback.updated_at,
        }


@app.get("/api/feedback/admin")
def list_feedback(request: Request):
    if not auth.can(request.state.user.role, "edit"):
        raise HTTPException(403, "دسترسی لازم برای مدیریت بازخورد را ندارید.")
    with Session() as s:
        return [
            _feedback_record(item)
            for item in s.scalars(
                select(Feedback).order_by(Feedback.id.desc()).limit(500)
            )
        ]


@app.patch("/api/feedback/admin/{ticket}")
def update_feedback(ticket: str, b: FeedbackUpdateIn, request: Request):
    if not auth.can(request.state.user.role, "edit"):
        raise HTTPException(403, "دسترسی لازم برای مدیریت بازخورد را ندارید.")
    with Session() as s:
        feedback = s.scalar(
            select(Feedback).where(Feedback.ticket == ticket.upper())
        )
        if not feedback:
            raise HTTPException(404, "شماره پیگیری پیدا نشد.")
        feedback.status = b.status
        feedback.reply = b.reply.strip()
        feedback.updated_at = now()
        log(s, "feedback", f"وضعیت بازخورد {feedback.ticket} به‌روز شد")
        s.commit()
        return _feedback_record(feedback)


@app.get("/api/activity")
def activity():
    with Session() as s:
        return [
            {"at": a.at, "kind": a.kind, "message": a.message}
            for a in s.scalars(
                select(ActivityLog).order_by(ActivityLog.id.desc()).limit(30)
            )
        ]


# ===================================================================== AI

@app.post("/api/ai")
async def run_ai(b: AIIn):
    if b.action not in ai.ACTIONS:
        raise HTTPException(400, "عملیات هوش مصنوعی نامعتبر است.")
    if len(b.text) + len(b.prompt) > 20_000:
        raise HTTPException(413, "متن درخواست بیش از حد طولانی است.")
    instruction = b.prompt.strip() or ai.ACTIONS.get(b.action)
    if not instruction:
        raise HTTPException(400, "عملیات هوش مصنوعی نامعتبر است.")
    if not (b.text.strip() or b.prompt.strip()):
        raise HTTPException(400, "ابتدا متنی بنویسید.")
    language = {"fa": "فارسی", "en": "انگلیسی", "ar": "عربی"}[b.language]
    tone = {
        "neutral": "متعادل",
        "formal": "رسمی",
        "friendly": "دوستانه",
        "news": "خبری",
    }[b.tone]
    length = {
        "short": "کوتاه",
        "medium": "متوسط",
        "long": "مفصل",
    }[b.length]
    instruction = (
        f"{instruction}\n\nزبان خروجی: {language}. "
        f"لحن: {tone}. طول: {length}."
    )
    try:
        out = await ai.get_provider().complete(
            ai.SYSTEM, f"{instruction}\n\n{b.text}"
        )
    except ai.AIError as e:
        raise HTTPException(502, str(e))
    if not isinstance(out, str) or not out.strip():
        raise HTTPException(502, "سرویس هوش مصنوعی پاسخ معتبری نداد.")
    if len(out) > 50_000:
        raise HTTPException(502, "پاسخ هوش مصنوعی بیش از حد بزرگ است.")
    out = out.strip()
    with Session() as s:
        s.add(AIUsage(action=b.action,
                      chars_in=len(b.text), chars_out=len(out)))
        s.commit()
    return {"text": out}


# ================================================================ templates

@app.get("/api/templates")
def templates():
    with Session() as s:
        return [
            {"id": t.id, "name": t.name, "category": t.category,
             "body": t.body, "builtin": t.builtin}
            for t in s.scalars(select(Template))
        ]


@app.post("/api/templates")
def add_template(b: TplIn):
    if not b.name.strip() or not b.body.strip():
        raise HTTPException(400, "نام و متن قالب الزامی است.")
    with Session() as s:
        t = Template(name=b.name, category=b.category, body=b.body)
        s.add(t)
        s.commit()
        return {"id": t.id}


@app.delete("/api/templates/{tid}")
def del_template(tid: int):
    with Session() as s:
        t = s.get(Template, tid)
        if not t or t.builtin:
            raise HTTPException(400, "قالب‌های پیش‌فرض قابل حذف نیستند.")
        s.delete(t)
        s.commit()
        return {"ok": True}


# ================================================================== media

def _cat(mime: str) -> str:
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("video/"):
        return "videos"
    if (mime.startswith("text/") or "pdf" in mime or "word" in mime
            or "sheet" in mime or "zip" in mime):
        return "files"
    return "other"


def _media_row(s, m: MediaAsset) -> dict:
    used = s.scalar(
        select(func.count()).select_from(Post).where(Post.media_id == m.id)
    ) or 0
    return {
        "id": m.id, "filename": m.filename, "category": m.category,
        "size": m.size, "tags": [t for t in m.tags.split(",") if t],
        "used": used, "created_at": m.created_at,
    }


@app.post("/api/media")
async def upload(file: UploadFile = File(...), tags: str = Form("")):
    if len(tags) > 2_000:
        raise HTTPException(413, "برچسب‌های فایل بیش از حد طولانی هستند.")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "حجم فایل بیش از ۲۰ مگابایت است.")
    if not data:
        raise HTTPException(400, "فایل خالی است؛ فایل دیگری انتخاب کنید.")
    name = os.path.basename(file.filename or "file").strip()[:255] or "file"
    stored = uuid.uuid4().hex + os.path.splitext(name)[1][:10]
    with open(os.path.join(MEDIA_DIR, stored), "wb") as f:
        f.write(data)
    with Session() as s:
        m = MediaAsset(
            filename=name, stored_name=stored,
            mime=file.content_type or "",
            category=_cat(file.content_type or ""),
            size=len(data), tags=tags,
        )
        s.add(m)
        log(s, "media", f"فایل {name} آپلود شد")
        s.commit()
        return _media_row(s, m)


@app.get("/api/media")
def media_list(q: str = "", category: str = ""):
    with Session() as s:
        qs = select(MediaAsset).order_by(MediaAsset.id.desc())
        if category:
            qs = qs.where(MediaAsset.category == category)
        return [_media_row(s, m) for m in s.scalars(qs)
                if q.lower() in (m.filename + m.tags).lower()]


@app.get("/api/media/{mid}/file")
def media_file(mid: int):
    with Session() as s:
        m = s.get(MediaAsset, mid)
        if not m:
            raise HTTPException(404, "فایل پیدا نشد.")
        return FileResponse(
            os.path.join(MEDIA_DIR, m.stored_name),
            media_type=m.mime or None, filename=m.filename,
        )


@app.post("/api/media/{mid}/tags")
def media_tags(mid: int, b: TagsIn):
    with Session() as s:
        m = s.get(MediaAsset, mid)
        if not m:
            raise HTTPException(404, "فایل پیدا نشد.")
        m.tags = ",".join(t.strip() for t in b.tags if t.strip())
        s.commit()
        return {"ok": True}


@app.delete("/api/media/{mid}")
def media_delete(mid: int):
    with Session() as s:
        m = s.get(MediaAsset, mid)
        if not m:
            raise HTTPException(404, "فایل پیدا نشد.")
        busy = s.scalar(
            select(func.count()).select_from(Post).where(
                Post.media_id == mid,
                Post.status.in_(["scheduled", "queued", "sending"]))
        )
        if busy:
            raise HTTPException(400, "این فایل در پست‌های در انتظار انتشار استفاده شده است.")
        try:
            os.remove(os.path.join(MEDIA_DIR, m.stored_name))
        except OSError:
            pass
        s.delete(m)
        s.commit()
        return {"ok": True}


# ============================================================== automations

def _due(a: Automation, local: dt.datetime) -> bool:
    last = a.last_run
    if a.trigger == "interval":
        return last is None or (
            (now() - last).total_seconds() >= max(a.interval_min, 1) * 60)
    if a.trigger == "weekly":
        wd = set(x for x in a.weekdays.split(",") if x)
        if str(local.weekday()) not in wd:
            return False
    already_today = last is not None and (last + TZ).date() == local.date()
    return local.strftime("%H:%M") >= a.at_time and not already_today


async def _fire(s, a: Automation) -> None:
    local = now() + TZ
    body = a.body
    if a.template_id:
        t = s.get(Template, a.template_id)
        if t:
            body = t.body

    text = (body.replace("{{title}}", a.name)
                .replace("{{date}}", local.strftime("%Y-%m-%d")))
    for v in ("summary", "link", "hashtags"):
        text = text.replace("{{" + v + "}}", "")

    if a.use_ai and a.ai_action in ai.ACTIONS:
        text = await ai.get_provider().complete(
            ai.SYSTEM, ai.ACTIONS[a.ai_action] + "\n\n" + text)
        s.add(AIUsage(action=a.ai_action,
                      chars_in=len(body), chars_out=len(text)))

    st = "pending" if a.require_approval else "queued"
    p = Post(title=a.name, text=text.strip(), status=st)
    s.add(p)
    s.flush()

    for cid in [int(x) for x in a.channel_ids.split(",") if x]:
        s.add(Delivery(
            post_id=p.id, channel_id=cid,
            idem_key=f"auto{a.id}:{p.id}:{cid}",
            run_at=now(), status=st,
        ))

    log(s, "automation",
        f"[{a.id}] «{a.name}» اجرا شد"
        + (" و در انتظار تأیید است" if a.require_approval
           else " و در صف ارسال قرار گرفت"))


async def run_automations() -> None:
    local = now() + TZ
    with Session() as s:
        active = s.scalars(
            select(Automation).where(Automation.status == "active")
        ).all()
        for a in active:
            if not _due(a, local):
                continue
            a.last_run = now()
            s.commit()
            try:
                await _fire(s, a)
            except Exception as e:
                s.rollback()
                msg = str(e) if isinstance(e, ai.AIError) else "خطای داخلی"
                log(s, "automation", f"[{a.id}] اجرا ناموفق بود: {msg}")
            s.commit()


def _auto_row(a: Automation) -> dict:
    return {
        "id": a.id, "name": a.name, "status": a.status,
        "trigger": a.trigger, "at_time": a.at_time,
        "weekdays": [int(x) for x in a.weekdays.split(",") if x],
        "interval_min": a.interval_min,
        "use_ai": a.use_ai, "ai_action": a.ai_action,
        "channel_ids": [int(x) for x in a.channel_ids.split(",") if x],
        "require_approval": a.require_approval,
        "last_run": a.last_run,
    }


@app.get("/api/automations")
def autos():
    with Session() as s:
        return [_auto_row(a) for a in s.scalars(
            select(Automation).order_by(Automation.id.desc())
        )]


@app.get("/api/automations/logs")
def auto_logs():
    with Session() as s:
        return [
            {"at": a.at, "message": a.message}
            for a in s.scalars(
                select(ActivityLog)
                .where(ActivityLog.kind == "automation")
                .order_by(ActivityLog.id.desc()).limit(20)
            )
        ]


@app.post("/api/automations")
def add_auto(b: AutoIn, request: Request):
    if (not b.name.strip() or not b.channel_ids
            or not (b.body.strip() or b.template_id)):
        raise HTTPException(400, "نام، کانال و متن یا قالب الزامی است.")
    if b.trigger not in ("daily", "weekly", "interval"):
        raise HTTPException(400, "نوع محرک نامعتبر است.")
    try:
        parsed_time = dt.datetime.strptime(b.at_time, "%H:%M").time()
    except (TypeError, ValueError):
        raise HTTPException(400, "ساعت باید بین 00:00 تا 23:59 باشد.")
    if parsed_time.strftime("%H:%M") != b.at_time:
        raise HTTPException(400, "ساعت باید با قالب HH:MM باشد.")
    if b.trigger == "interval" and not 1 <= b.interval_min <= 10080:
        raise HTTPException(400, "فاصله اجرا باید بین ۱ تا ۱۰۰۸۰ دقیقه باشد.")
    if any(day not in range(7) for day in b.weekdays):
        raise HTTPException(400, "روز هفته نامعتبر است.")
    if b.trigger == "weekly" and not b.weekdays:
        raise HTTPException(400, "حداقل یک روز هفته انتخاب کنید.")
    if len(b.channel_ids) != len(set(b.channel_ids)):
        raise HTTPException(400, "کانال تکراری انتخاب شده است.")
    if not b.require_approval and not auth.can(request.state.user.role, "publish"):
        raise HTTPException(403, "انتشار بدون تأیید نیازمند دسترسی انتشار است.")
    with Session() as s:
        requested_channels = set(b.channel_ids)
        available_channels = set(s.scalars(
            select(Channel.id).where(Channel.id.in_(requested_channels))
        ))
        if available_channels != requested_channels:
            raise HTTPException(400, "یکی از کانال‌های انتخاب‌شده پیدا نشد.")
        if b.template_id is not None and not s.get(Template, b.template_id):
            raise HTTPException(404, "قالب انتخاب‌شده پیدا نشد.")
        a = Automation(
            name=b.name, trigger=b.trigger, at_time=b.at_time,
            weekdays=",".join(map(str, b.weekdays)),
            interval_min=b.interval_min, template_id=b.template_id,
            body=b.body, use_ai=b.use_ai, ai_action=b.ai_action,
            channel_ids=",".join(map(str, b.channel_ids)),
            require_approval=b.require_approval,
            last_run=now() if b.trigger == "interval" else None,
        )
        s.add(a)
        s.commit()
        return _auto_row(a)


@app.post("/api/automations/{aid}/toggle")
def toggle_auto(aid: int):
    with Session() as s:
        a = s.get(Automation, aid)
        if not a:
            raise HTTPException(404, "اتوماسیون پیدا نشد.")
        a.status = "paused" if a.status == "active" else "active"
        s.commit()
        return _auto_row(a)


@app.post("/api/automations/{aid}/run")
async def run_auto(aid: int, request: Request):
    with Session() as s:
        a = s.get(Automation, aid)
        if not a:
            raise HTTPException(404, "اتوماسیون پیدا نشد.")
        if not a.require_approval and not auth.can(request.state.user.role, "publish"):
            raise HTTPException(403, "دسترسی لازم برای این عملیات را ندارید.")
        try:
            await _fire(s, a)
        except ai.AIError as e:
            raise HTTPException(502, str(e))
        s.commit()
        return {"ok": True}


@app.delete("/api/automations/{aid}")
def del_auto(aid: int):
    with Session() as s:
        a = s.get(Automation, aid)
        if a:
            s.delete(a)
            s.commit()
    return {"ok": True}


# =============================================================== analytics

@app.get("/api/analytics")
def get_analytics(days: int = 30):
    with Session() as s:
        return analytics.collect(s, days)


# =================================================================== auth

_fails: dict[str, list[float]] = {}
_FAIL_WINDOW = 300
_FAIL_LIMIT = 5


def _user(u: User) -> dict:
    return {
        "id": u.id, "username": u.username,
        "name": u.display_name or u.username, "role": u.role,
    }


def _check_pw(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "رمز عبور باید حداقل ۸ نویسه باشد.")
    if len(pw) > 1024:
        raise HTTPException(400, "رمز عبور بیش از حد طولانی است.")


@app.get("/api/auth/state")
def auth_state():
    with Session() as s:
        return {
            "has_users": bool(s.scalar(select(func.count()).select_from(User))),
        }


@app.post("/api/auth/register")
def register(b: RegIn):
    _check_pw(b.password)
    with Session() as s:
        if s.scalar(select(func.count()).select_from(User)):
            raise HTTPException(
                400, "ثبت‌نام بسته است؛ از مالک بخواهید شما را اضافه کند.")
        s.add(Workspace(name="فضای کاری من"))
        u = User(
            username=b.username.strip().lower(),
            display_name=b.display_name,
            pw_hash=auth.hash_pw(b.password),
            role="owner",
        )
        s.add(u)
        log(s, "auth", f"مالک {u.username} ثبت شد")
        s.commit()
        return {"token": auth.make_token(u.id), "user": _user(u)}


@app.post("/api/auth/login")
def login(b: LoginIn):
    k = b.username.strip().lower()
    cutoff = now().timestamp() - _FAIL_WINDOW
    tries = [t for t in _fails.get(k, []) if t > cutoff]
    if len(tries) >= _FAIL_LIMIT:
        raise HTTPException(
            429, "تلاش‌های ناموفق زیاد بود؛ چند دقیقه بعد دوباره امتحان کنید.")
    with Session() as s:
        u = s.scalar(select(User).where(User.username == k))
        if not u or not auth.verify_pw(b.password, u.pw_hash):
            _fails[k] = tries + [now().timestamp()]
            raise HTTPException(400, "نام کاربری یا رمز عبور درست نیست.")
        _fails.pop(k, None)
        return {"token": auth.make_token(u.id), "user": _user(u)}


@app.get("/api/auth/me")
def me(request: Request):
    return _user(request.state.user)


# =================================================================== team

@app.get("/api/team")
def team():
    with Session() as s:
        return [{**_user(u), "eitaa_id": u.eitaa_id}
                for u in s.scalars(select(User))]


@app.post("/api/team")
def add_member(b: MemberIn):
    _check_pw(b.password)
    if b.role not in auth.ROLES or b.role == "owner":
        raise HTTPException(400, "نقش نامعتبر است.")
    with Session() as s:
        uname = b.username.strip().lower()
        if s.scalar(select(User).where(User.username == uname)):
            raise HTTPException(400, "این نام کاربری قبلاً ثبت شده است.")
        u = User(
            username=uname, display_name=b.display_name,
            pw_hash=auth.hash_pw(b.password),
            role=b.role, eitaa_id=b.eitaa_id or None,
        )
        s.add(u)
        log(s, "team", f"عضو {u.username} با نقش {u.role} اضافه شد")
        s.commit()
        return _user(u)


@app.delete("/api/team/{uid}")
def del_member(uid: int, request: Request):
    if uid == request.state.user.id:
        raise HTTPException(400, "نمی‌توانید خودتان را حذف کنید.")
    with Session() as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)
            s.commit()
    return {"ok": True}


# ================================================================= health

@app.get("/api/health")
def health():
    return {"ok": True, "app": "Eitaa Manager",
            "time": now().isoformat(timespec="seconds")}


# =============================================================== security

PUBLIC = (
    "/api/health", "/api/auth/state", "/api/auth/register",
    "/api/auth/login", "/api/feedback",
)

RULES = [
    (("GET", "POST", "DELETE"), r"^/api/team", "owner"),
    (("GET", "PATCH"), r"^/api/feedback/admin", "edit"),
    (("GET", "POST"), r"^/api/removal-reminders", "edit"),
    (("POST",), r"^/api/removal-reminders/\d+/(cancel|reschedule)", "edit"),
    (("POST",), r"^/api/(connections|channels)", "owner"),
    (("GET",), r"^/api/analytics", "analytics"),
    (("PUT", "DELETE"), r"^/api/posts/\d+$", "edit"),
    (("POST",), r"^/api/posts/\d+/publish$", "publish"),
    (("POST",), r"^/api/(posts/\d+/(cancel|reschedule)|deliveries/\d+/retry)", "publish"),
    (("POST",), r"^/api/posts/\d+/(approve|reject)", "approve"),
    (("POST", "DELETE"), r"^/api/(templates|media|ai)", "edit"),
    (("POST", "DELETE"), r"^/api/automations(?:/\d+)?$", "edit"),
    (("POST",), r"^/api/automations/\d+/toggle$", "edit"),
]


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if (not path.startswith("/api/")
            or path in PUBLIC
            or request.method == "OPTIONS"):
        return await call_next(request)

    raw = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    uid = auth.read_token(raw) if raw else None
    with Session() as s:
        u = s.get(User, uid) if uid else None
    if not u:
        return JSONResponse({"detail": "ابتدا وارد شوید."}, status_code=401)
    request.state.user = u

    # Running an automation may create an approval-pending post or publish
    # immediately. Allow editors and publishers to reach the endpoint; the
    # endpoint itself enforces the extra publish permission when approval is
    # disabled. The broad automation-management rule intentionally excludes
    # this route so it cannot reject publishers before that check.
    matched_rule = False
    if request.method == "POST" and re.fullmatch(r"/api/automations/\\d+/run", path):
        if not (auth.can(u.role, "edit") or auth.can(u.role, "publish")):
            return JSONResponse(
                {"detail": "دسترسی لازم برای این عملیات را ندارید."},
                status_code=403,
            )
        matched_rule = True

    # POST /api/posts chooses its permission from the submitted mode inside    # create_post(): drafts require "edit", while immediate/scheduled posts
    # require "publish". The middleware must allow either capability to reach
    # that endpoint-level check; otherwise publishers are blocked before it.
    matched_rule = False
    if request.method == "POST" and path == "/api/posts":
        if not (auth.can(u.role, "edit") or auth.can(u.role, "publish")):
            return JSONResponse(
                {"detail": "دسترسی لازم برای این عملیات را ندارید."},
                status_code=403,
            )
        # This endpoint has its own mode-dependent permission check below.
        # Mark it as explicitly handled so the generic fallback does not
        # reject non-owner editors/publishers.
        matched_rule = True

    for methods, rx, perm in RULES:
        if request.method in methods and re.match(rx, path):
            matched_rule = True
            if not auth.can(u.role, perm):
                return JSONResponse(
                    {"detail": "دسترسی لازم برای این عملیات را ندارید."},
                    status_code=403,
                )
            break

    # Non-owner write operations must match an explicit permission rule.
    # This prevents new endpoints from accidentally bypassing role checks.
    if (not matched_rule
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not auth.can(u.role, "*")):
        return JSONResponse(
            {"detail": "برای این عملیات مجوز مشخصی تعریف نشده است."},
            status_code=403,
        )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "tauri://localhost,http://tauri.localhost,http://localhost:5173",
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
