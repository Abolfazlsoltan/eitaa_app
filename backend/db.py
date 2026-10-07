import os, datetime as dt
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, String, Integer, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///./data.db"), connect_args={"check_same_thread": False} if "sqlite" in os.getenv("DATABASE_URL", "sqlite") else {})
Session = sessionmaker(engine, expire_on_commit=False)
_key = os.getenv("APP_SECRET_KEY")
if not _key:
    raise RuntimeError("APP_SECRET_KEY is required (see .env.example)")
fernet = Fernet(_key.encode())
now = lambda: dt.datetime.utcnow()

class Base(DeclarativeBase): pass

class Connection(Base):  # token belongs to the connection, never to a channel
    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_enc: Mapped[str] = mapped_column(Text)
    bot_name: Mapped[str] = mapped_column(String(200), default="")

class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    title: Mapped[str] = mapped_column(String(200))
    chat_id: Mapped[str] = mapped_column(String(200))  # username (no @) or numeric id

class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|scheduled|queued|sending|published|failed|cancelled
    silent: Mapped[bool] = mapped_column(Boolean, default=False)
    pin: Mapped[bool] = mapped_column(Boolean, default=False)
    view_to_delete: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

class Delivery(Base):  # independent status per channel
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("idem_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    idem_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    error: Mapped[str] = mapped_column(Text, default="")
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

class ActivityLog(Base):
    __tablename__ = "activity_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)

class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    action: Mapped[str] = mapped_column(String(30))
    chars_in: Mapped[int] = mapped_column(Integer, default=0)
    chars_out: Mapped[int] = mapped_column(Integer, default=0)

class Template(Base):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(50))
    body: Mapped[str] = mapped_column(Text)  # variables: {{title}} {{summary}} {{link}} {{date}} {{hashtags}}
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)

class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(80))
    mime: Mapped[str] = mapped_column(String(100), default="")
    category: Mapped[str] = mapped_column(String(10))  # images|videos|files|other
    size: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))

class User(Base):  # also serves as TeamMember (role: owner|editor|approver|publisher|analyst)
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, default=1)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    pw_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="editor")
    eitaa_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

class Automation(Base):
    __tablename__ = "automations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="active")
    trigger: Mapped[str] = mapped_column(String(10), default="daily")  # daily|weekly|interval
    at_time: Mapped[str] = mapped_column(String(5), default="09:00")
    weekdays: Mapped[str] = mapped_column(String(20), default="")  # python weekday numbers, Mon=0
    interval_min: Mapped[int] = mapped_column(Integer, default=60)
    template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    use_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_action: Mapped[str] = mapped_column(String(30), default="")
    channel_ids: Mapped[str] = mapped_column(String(200), default="")
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

MIGRATIONS = {"posts": [("media_id", "INTEGER")], "deliveries": [("done_at", "DATETIME")]}
def init():  # create_all + tiny additive migrations; use Alembic for production
    from sqlalchemy import text, inspect
    Base.metadata.create_all(engine)
    for table, cols in MIGRATIONS.items():
        have = [c["name"] for c in inspect(engine).get_columns(table)]
        for name, ddl in cols:
            if name not in have:
                with engine.begin() as c: c.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
