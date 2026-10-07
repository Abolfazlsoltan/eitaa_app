"""FastAPI backend for the Eitaa Channel Manager.

Runs as a package (`backend`) with relative imports. Start with:

    uvicorn backend.main:app --reload --port 8000

or from inside the package directory:

    cd backend
    uvicorn main:app --reload --port 8000

The client (`src/main.py`) may run either against this server (server mode)
or entirely on-device (local mode). In local mode this file is not used at
all; see `src/local.py` for the offline implementation.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update

from . import ai, analytics, auth, eitaa
from .db import (
    ActivityLog,
    AIUsage,
    Automation,
    Channel,
    Connection,
    Delivery,
    MediaAsset,
    Post,
    Session,
    Template,
    User,
    Workspace,
    fernet,
    init,
    now,
)

# ================================================================ constants ==

MAX_ATTEMPTS = 5
MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
os.makedirs(MEDIA_DIR, exist_ok=True)

# Iran Standard Time offset from UTC (minutes). Configurable for tests.
TZ = dt.timedelta(minutes=int(os.getenv("TZ_OFFSET_MIN", "210")))


# ================================================================== helpers ==

def log(s, kind: str, message: str) -> None:
    """Append an entry to the activity log inside an open session."""
    s.add(ActivityLog(kind=kind, message=message))


def _bot_name(get_me_response) -> str:
    """Extract a display name from an EitaaYar `getMe` response."""
    result = (get_me_response or {}).get("result") or {}
    return str(
        result.get("first_name")
        or result.get("username")
        or result.get("title")
        or "ربات"
    )


# =================================================================== worker ==

async def worker() -> None:
    """Background loop: promote due deliveries, deliver them, run automations."""
    while True:
        try:
            with Session() as s:
                # Promote scheduled → queued for anything due.
                sched = s.scalars(
                    select(Delivery).where(
                        Delivery.status == "scheduled",
                        Delivery.run_at <= now(),
                    )
                ).all()
                for d in sched:
                    d.status = "queued"

                # Collect a batch of queued deliveries.
                due_ids = s.scalars(
                    select(Delivery.id)
                    .where(
                        Delivery.status == "queued",
                        Delivery.run_at <= now(),
                    )
                    .limit(20)
                ).all()
                s.commit()

            for did in due_ids:
                await deliver(did)

            await run_automations()
        except Exception as e:  # never let the worker die
            print("worker error:", type(e).__name__, e)
        await asyncio.sleep(5)


async def deliver(delivery_id: int) -> None:
    """Deliver a single queued delivery. Atomic claim prevents double sends."""
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
        conn = s.get(Connection, ch.connection_id)
        token = fernet.decrypt(conn.token_enc.encode()).decode()

        try:
            m = s.get(MediaAsset, p.media_id) if p.media_id else None
            if m:
                await eitaa.send_file(
                    token,
                    ch.chat_id,
                    os.path.join(MEDIA_DIR, m.stored_name),
                    m.filename,
                    p.text,
                    p.title,
                    p.silent,
                    p.pin,
                    p.view_to_delete,
                )
            else:
                await eitaa.send_message(
                    token,
                    ch.chat_id,
                    p.text,
                    p.title,
                    p.silent,
                    p.pin,
                    p.view_to_delete,
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


def _refresh_post(s, post_id: int) -> None:
    """Recompute the post's aggregate status from its deliveries."""
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


# ============================================================ default data ==

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


# ================================================================= lifespan ==

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


app = FastAPI(title="Eitaa Channel Manager", lifespan=lifespan)


# ================================================================ schemas ==

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
    channel_ids: list[int] = []
    mode: str = "draft"  # draft | now | schedule
    scheduled_at: dt.datetime | None = None
    silent: bool = False
    pin: bool = False
    view_to_delete: int | None = None
    media_id: int | None = None
    repeat: str = "none"  # none | daily | weekly
    repeat_count: int = 1


class AIIn(BaseModel):
    action: str
    text: str = ""
    prompt: str = ""


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
    trigger: str = "daily"  # daily | weekly | interval
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
    username: str
    password: str
    display_name: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class EitaaIn(BaseModel):
    init_data: str


class MemberIn(BaseModel):
    username: str
    password: str
    role: str = "editor"
    display_name: str = ""
    eitaa_id: str | None = None


# ============================================================= connections ==

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
        log(s, "connect", "اتصال ایتایار برقرار شد")
        s.commit()
        # Token is never returned to the client.
        return {"id": c.id, "name": c.name, "bot": c.bot_name}


@app.get("/api/connections")
def conns():
    with Session() as s:
        return [
            {"id": c.id, "name": c.name, "bot": c.bot_name}
            for c in s.scalars(select(Connection))
        ]


# ================================================================ channels ==

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


# =================================================================== posts ==

def _create_post(b: PostIn) -> dict:
    if not b.text.strip():
        raise HTTPException(400, "متن پست خالی است.")
    if b.mode != "draft" and not b.channel_ids:
        raise HTTPException(400, "حداقل یک کانال انتخاب کنید.")
    if b.mode == "schedule" and not b.scheduled_at:
        raise HTTPException(400, "زمان انتشار را مشخص کنید.")

    with Session() as s:
        p = Post(
            title=b.title,
            text=b.text,
            silent=b.silent,
            pin=b.pin,
            view_to_delete=b.view_to_delete,
            media_id=b.media_id,
            scheduled_at=b.scheduled_at,
            status={"draft": "draft", "now": "queued",
                    "schedule": "scheduled"}[b.mode],
        )
        s.add(p)
        s.flush()

        if b.mode != "draft":
            at = b.scheduled_at if b.mode == "schedule" else now()
            for cid in b.channel_ids:
                s.add(Delivery(
                    post_id=p.id,
                    channel_id=cid,
                    idem_key=f"{p.id}:{cid}",
                    run_at=at,
                    status="scheduled" if b.mode == "schedule" else "queued",
                ))

        log(s, "post", f"پست «{b.title or p.id}» ثبت شد ({b.mode})")
        s.commit()
        return {"id": p.id, "status": p.status}


@app.post("/api/posts")
def create_post(b: PostIn, request: Request):
    if not auth.can(
        request.state.user.role,
        "edit" if b.mode == "draft" else "publish",
    ):
        raise HTTPException(403, "دسترسی لازم برای این عملیات را ندارید.")

    res = _create_post(b)

    # Expand recurrence into independent scheduled posts.
    if b.mode == "schedule" and b.repeat in ("daily", "weekly") and b.scheduled_at:
        step = dt.timedelta(days=1 if b.repeat == "daily" else 7)
        for i in range(1, min(max(b.repeat_count, 1), 30)):
            _create_post(b.model_copy(update={
                "scheduled_at": b.scheduled_at + step * i,
                "repeat": "none",
            }))
    return res


@app.get("/api/posts")
def posts(status: str | None = None):
    with Session() as s:
        q = select(Post).order_by(Post.id.desc())
        if status:
            q = q.where(Post.status == status)
        out = []
        for p in s.scalars(q):
            ds = s.scalars(
                select(Delivery).where(Delivery.post_id == p.id)
            ).all()
            out.append({
                "id": p.id,
                "title": p.title,
                "text": p.text,
                "status": p.status,
                "scheduled_at": p.scheduled_at,
                "deliveries": [
                    {
                        "id": d.id,
                        "channel_id": d.channel_id,
                        "status": d.status,
                        "error": d.error,
                    }
                    for d in ds
                ],
            })
        return out


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
            .where(
                Delivery.post_id == pid,
                Delivery.status.in_(["scheduled", "queued", "pending"]),
            )
            .values(status="cancelled")
        )
        p = s.get(Post, pid)
        if p:
            p.status = "cancelled"
        log(s, "cancel", f"پست {pid} لغو شد")
        s.commit()
        return {"ok": True}


class ReIn2(BaseModel):
    scheduled_at: dt.datetime


@app.post("/api/posts/{pid}/reschedule")
def reschedule(pid: int, b: ReIn2):
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


# =================================================================== stats ==

@app.get("/api/stats")
def stats():
    with Session() as s:
        c = lambda *st: s.scalar(
            select(func.count()).select_from(Delivery).where(
                Delivery.status.in_(st)
            )
        ) or 0
        return {
            "published": c("published"),
            "failed": c("failed"),
            "scheduled": c("scheduled"),
            "queued": c("queued", "sending"),
            "ai_calls": s.scalar(
                select(func.count()).select_from(AIUsage)
            ) or 0,
        }


@app.get("/api/activity")
def activity():
    with Session() as s:
        return [
            {"at": a.at, "kind": a.kind, "message": a.message}
            for a in s.scalars(
                select(ActivityLog)
                .order_by(ActivityLog.id.desc())
                .limit(30)
            )
        ]


# ===================================================================== AI ==

@app.post("/api/ai")
async def run_ai(b: AIIn):
    instruction = b.prompt.strip() or ai.ACTIONS.get(b.action)
    if not instruction:
        raise HTTPException(400, "عملیات هوش مصنوعی نامعتبر است.")
    if not (b.text.strip() or b.prompt.strip()):
        raise HTTPException(400, "ابتدا متنی بنویسید.")

    try:
        out = await ai.get_provider().complete(
            ai.SYSTEM, f"{instruction}\n\n{b.text}"
        )
    except ai.AIError as e:
        raise HTTPException(502, str(e))

    with Session() as s:
        s.add(AIUsage(
            action=b.action,
            chars_in=len(b.text),
            chars_out=len(out),
        ))
        s.commit()
    return {"text": out}


# ================================================================ templates ==

@app.get("/api/templates")
def templates():
    with Session() as s:
        return [
            {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "body": t.body,
                "builtin": t.builtin,
            }
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


# ================================================================== media ==

def _cat(mime: str) -> str:
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("video/"):
        return "videos"
    if (
        mime.startswith("text/")
        or "pdf" in mime
        or "word" in mime
        or "sheet" in mime
        or "zip" in mime
    ):
        return "files"
    return "other"


def _media_row(s, m: MediaAsset) -> dict:
    used = s.scalar(
        select(func.count()).select_from(Post).where(Post.media_id == m.id)
    ) or 0
    return {
        "id": m.id,
        "filename": m.filename,
        "category": m.category,
        "size": m.size,
        "tags": [t for t in m.tags.split(",") if t],
        "used": used,
        "created_at": m.created_at,
    }


@app.post("/api/media")
async def upload(file: UploadFile = File(...), tags: str = Form("")):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "حجم فایل بیش از ۲۰ مگابایت است.")

    name = os.path.basename(file.filename or "file")
    stored = uuid.uuid4().hex + os.path.splitext(name)[1][:10]

    with open(os.path.join(MEDIA_DIR, stored), "wb") as f:
        f.write(data)

    with Session() as s:
        m = MediaAsset(
            filename=name,
            stored_name=stored,
            mime=file.content_type or "",
            category=_cat(file.content_type or ""),
            size=len(data),
            tags=tags,
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
        return [
            _media_row(s, m)
            for m in s.scalars(qs)
            if q.lower() in (m.filename + m.tags).lower()
        ]


@app.get("/api/media/{mid}/file")
def media_file(mid: int):
    with Session() as s:
        m = s.get(MediaAsset, mid)
        if not m:
            raise HTTPException(404, "فایل پیدا نشد.")
        return FileResponse(
            os.path.join(MEDIA_DIR, m.stored_name),
            media_type=m.mime or None,
            filename=m.filename,
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
                Post.status.in_(["scheduled", "queued", "sending"]),
            )
        )
        if busy:
            raise HTTPException(
                400, "این فایل در پست‌های در انتظار انتشار استفاده شده است."
            )
        try:
            os.remove(os.path.join(MEDIA_DIR, m.stored_name))
        except OSError:
            pass
        s.delete(m)
        s.commit()
        return {"ok": True}


# ============================================================== automations ==

def _due(a: Automation, local: dt.datetime) -> bool:
    last = a.last_run
    if a.trigger == "interval":
        return last is None or (
            (now() - last).total_seconds() >= max(a.interval_min, 1) * 60
        )
    if a.trigger == "weekly":
        wd = set(x for x in a.weekdays.split(",") if x)
        if str(local.weekday()) not in wd:
            return False
    # daily or weekly: fire once per local day at/after `at_time`
    already_today = last is not None and (last + TZ).date() == local.date()
    return local.strftime("%H:%M") >= a.at_time and not already_today


async def _fire(s, a: Automation) -> None:
    """Build a post from the automation and queue/pend it."""
    local = now() + TZ
    body = a.body

    if a.template_id:
        t = s.get(Template, a.template_id)
        if t:
            body = t.body

    text = (
        body.replace("{{title}}", a.name)
            .replace("{{date}}", local.strftime("%Y-%m-%d"))
    )
    for v in ("summary", "link", "hashtags"):
        text = text.replace("{{" + v + "}}", "")

    if a.use_ai and a.ai_action in ai.ACTIONS:
        text = await ai.get_provider().complete(
            ai.SYSTEM, ai.ACTIONS[a.ai_action] + "\n\n" + text
        )
        s.add(AIUsage(
            action=a.ai_action,
            chars_in=len(body),
            chars_out=len(text),
        ))

    st = "pending" if a.require_approval else "queued"
    p = Post(title=a.name, text=text.strip(), status=st)
    s.add(p)
    s.flush()

    for cid in [int(x) for x in a.channel_ids.split(",") if x]:
        s.add(Delivery(
            post_id=p.id,
            channel_id=cid,
            idem_key=f"auto{a.id}:{p.id}:{cid}",
            run_at=now(),
            status=st,
        ))

    log(
        s,
        "automation",
        f"[{a.id}] «{a.name}» اجرا شد"
        + (" و در انتظار تأیید است" if a.require_approval
           else " و در صف ارسال قرار گرفت"),
    )


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
        "id": a.id,
        "name": a.name,
        "status": a.status,
        "trigger": a.trigger,
        "at_time": a.at_time,
        "weekdays": [int(x) for x in a.weekdays.split(",") if x],
        "interval_min": a.interval_min,
        "use_ai": a.use_ai,
        "ai_action": a.ai_action,
        "channel_ids": [int(x) for x in a.channel_ids.split(",") if x],
        "require_approval": a.require_approval,
        "last_run": a.last_run,
    }


@app.get("/api/automations")
def autos():
    with Session() as s:
        return [
            _auto_row(a)
            for a in s.scalars(select(Automation).order_by(Automation.id.desc()))
        ]


@app.get("/api/automations/logs")
def auto_logs():
    with Session() as s:
        return [
            {"at": a.at, "message": a.message}
            for a in s.scalars(
                select(ActivityLog)
                .where(ActivityLog.kind == "automation")
                .order_by(ActivityLog.id.desc())
                .limit(20)
            )
        ]


@app.post("/api/automations")
def add_auto(b: AutoIn, request: Request):
    if not b.name.strip() or not b.channel_ids or not (b.body.strip() or b.template_id):
        raise HTTPException(400, "نام، کانال و متن یا قالب الزامی است.")
    if b.trigger not in ("daily", "weekly", "interval"):
        raise HTTPException(400, "نوع محرک نامعتبر است.")
    if not re.match(r"^\d\d:\d\d$", b.at_time):
        raise HTTPException(400, "قالب ساعت باید HH:MM باشد.")
    if b.trigger == "weekly" and not b.weekdays:
        raise HTTPException(400, "حداقل یک روز هفته انتخاب کنید.")
    if not b.require_approval and not auth.can(request.state.user.role, "publish"):
        raise HTTPException(403, "انتشار بدون تأیید نیازمند دسترسی انتشار است.")

    with Session() as s:
        a = Automation(
            name=b.name,
            trigger=b.trigger,
            at_time=b.at_time,
            weekdays=",".join(map(str, b.weekdays)),
            interval_min=b.interval_min,
            template_id=b.template_id,
            body=b.body,
            use_ai=b.use_ai,
            ai_action=b.ai_action,
            channel_ids=",".join(map(str, b.channel_ids)),
            require_approval=b.require_approval,
            # For interval triggers, start the countdown now.
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


# =============================================================== analytics ==

@app.get("/api/analytics")
def get_analytics(days: int = 30):
    with Session() as s:
        return analytics.collect(s, days)


# =================================================================== auth ==

_fails: dict[str, list[float]] = {}
_FAIL_WINDOW = 300  # seconds
_FAIL_LIMIT = 5


def _user(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "name": u.display_name or u.username,
        "role": u.role,
    }


def _check_pw(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "رمز عبور باید حداقل ۸ نویسه باشد.")


@app.get("/api/auth/state")
def auth_state():
    with Session() as s:
        return {
            "has_users": bool(s.scalar(select(func.count()).select_from(User))),
            "eitaa": bool(os.getenv("EITAA_WEBAPP_BOT_TOKEN")),
            "local": False,
        }


@app.post("/api/auth/register")
def register(b: RegIn):
    _check_pw(b.password)
    with Session() as s:
        if s.scalar(select(func.count()).select_from(User)):
            raise HTTPException(
                400, "ثبت‌نام بسته است؛ از مالک بخواهید شما را اضافه کند."
            )
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
            429, "تلاش‌های ناموفق زیاد بود؛ چند دقیقه بعد دوباره امتحان کنید."
        )
    with Session() as s:
        u = s.scalar(select(User).where(User.username == k))
        if not u or not auth.verify_pw(b.password, u.pw_hash):
            _fails[k] = tries + [now().timestamp()]
            raise HTTPException(400, "نام کاربری یا رمز عبور درست نیست.")
        _fails.pop(k, None)
        return {"token": auth.make_token(u.id), "user": _user(u)}


@app.post("/api/auth/eitaa")
def eitaa_login(b: EitaaIn):
    tok = os.getenv("EITAA_WEBAPP_BOT_TOKEN")
    if not tok:
        raise HTTPException(501, "ورود با Mini App ایتا پیکربندی نشده است.")
    info = auth.validate_init_data(b.init_data, tok)
    if not info or "id" not in info:
        raise HTTPException(400, "اعتبارسنجی initData ناموفق بود.")
    with Session() as s:
        u = s.scalar(select(User).where(User.eitaa_id == str(info["id"])))
        if not u:
            raise HTTPException(
                400,
                "این حساب ایتا به هیچ عضوی متصل نیست؛ "
                "از مالک بخواهید شناسه ایتای شما را ثبت کند.",
            )
        return {"token": auth.make_token(u.id), "user": _user(u)}


@app.get("/api/auth/me")
def me(request: Request):
    return _user(request.state.user)


# =================================================================== team ==

@app.get("/api/team")
def team():
    with Session() as s:
        return [
            {**_user(u), "eitaa_id": u.eitaa_id}
            for u in s.scalars(select(User))
        ]


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
            username=uname,
            display_name=b.display_name,
            pw_hash=auth.hash_pw(b.password),
            role=b.role,
            eitaa_id=b.eitaa_id or None,
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


# ============================================================== health check =

@app.get("/api/health")
def health():
    """Public liveness probe — useful for the client to detect a live server."""
    return {
        "ok": True,
        "app": "Eitaa Channel Manager",
        "time": now().isoformat(timespec="seconds"),
    }


# ================================================================ security ==

PUBLIC = (
    "/api/health",
    "/api/auth/state",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/eitaa",
)

RULES = [
    (("GET", "POST", "DELETE"), r"^/api/team", "owner"),
    (("POST",), r"^/api/(connections|channels)", "owner"),
    (("GET",), r"^/api/analytics", "analytics"),
    (("POST",), r"^/api/(posts/\d+/(cancel|reschedule)|deliveries/\d+/retry)", "publish"),
    (("POST",), r"^/api/posts/\d+/(approve|reject)", "approve"),
    (("POST", "DELETE"), r"^/api/(templates|media|automations|ai)", "edit"),
]


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if (
        not path.startswith("/api/")
        or path in PUBLIC
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    raw = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    uid = auth.read_token(raw) if raw else None
    with Session() as s:
        u = s.get(User, uid) if uid else None
    if not u:
        return JSONResponse({"detail": "ابتدا وارد شوید."}, status_code=401)
    request.state.user = u

    for methods, rx, perm in RULES:
        if request.method in methods and re.match(rx, path) and not auth.can(u.role, perm):
            return JSONResponse(
                {"detail": "دسترسی لازم برای این عملیات را ندارید."},
                status_code=403,
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS",
        "tauri://localhost,http://tauri.localhost,http://localhost:5173",
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
