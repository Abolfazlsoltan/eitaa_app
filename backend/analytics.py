"""Analytics adapter: only metrics derived from this app's own operations.
To add a trusted external source (e.g. official stats), implement AnalyticsSource and add it to SOURCES."""
import datetime as dt
from abc import ABC, abstractmethod
from sqlalchemy import select, func
from .db import Delivery, Channel, AIUsage, now

class AnalyticsSource(ABC):
    @abstractmethod
    def collect(self, s, days: int) -> dict: ...

class LocalOperationsSource(AnalyticsSource):
    def collect(self, s, days):
        days = max(1, min(days, 90)); since = now() - dt.timedelta(days=days)
        ch = {c.id: c.title for c in s.scalars(select(Channel))}
        daily = {(since + dt.timedelta(days=i + 1)).strftime("%Y-%m-%d"): {"published": 0, "failed": 0} for i in range(days)}
        per: dict = {}; monthly: dict = {}
        for d in s.scalars(select(Delivery).where(Delivery.done_at.is_not(None))):
            if d.status not in ("published", "failed"): continue
            if d.status == "published": monthly[d.done_at.strftime("%Y-%m")] = monthly.get(d.done_at.strftime("%Y-%m"), 0) + 1
            if d.done_at < since: continue
            k = d.done_at.strftime("%Y-%m-%d")
            if k in daily: daily[k][d.status] += 1
            x = per.setdefault(d.channel_id, {"title": ch.get(d.channel_id, "؟"), "published": 0, "failed": 0}); x[d.status] += 1
        pub = sum(v["published"] for v in daily.values()); fail = sum(v["failed"] for v in daily.values())
        cnt = lambda *st: s.scalar(select(func.count()).select_from(Delivery).where(Delivery.status.in_(st))) or 0
        return {"days": [{"date": k, **v} for k, v in daily.items()], "channels": list(per.values()),
                "monthly": [{"month": k, "published": v} for k, v in sorted(monthly.items())[-12:]],
                "totals": {"published": pub, "failed": fail, "scheduled": cnt("scheduled"), "queue": cnt("queued", "sending"),
                           "failure_rate": round(fail / (pub + fail), 3) if pub + fail else 0,
                           "ai_calls": s.scalar(select(func.count()).select_from(AIUsage).where(AIUsage.at >= since)) or 0}}

SOURCES: list[AnalyticsSource] = [LocalOperationsSource()]
def collect(s, days: int) -> dict: return SOURCES[0].collect(s, days)
