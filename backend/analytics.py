"""Analytics adapter for metrics derived from this app's own operations."""
import datetime as dt
from abc import ABC, abstractmethod

from sqlalchemy import func, select

from .db import AIUsage, Channel, Delivery, now


class AnalyticsSource(ABC):
    @abstractmethod
    def collect(self, s, days: int) -> dict:
        ...


class LocalOperationsSource(AnalyticsSource):
    def collect(self, s, days: int) -> dict:
        days = max(1, min(int(days), 90))
        today = now().date()
        start_date = today - dt.timedelta(days=days - 1)
        since = dt.datetime.combine(start_date, dt.time.min)
        # Keep the scan bounded: daily/channel statistics need the requested
        # window, while the monthly chart only needs approximately one year.
        month_since = dt.datetime.combine(today, dt.time.min) - dt.timedelta(days=365)
        channels = {
            channel.id: channel.title
            for channel in s.scalars(select(Channel))
        }
        daily = {
            (start_date + dt.timedelta(days=i)).strftime("%Y-%m-%d"): {
                "published": 0,
                "failed": 0,
            }
            for i in range(days)
        }
        per_channel: dict[int, dict] = {}
        monthly: dict[str, int] = {}

        deliveries = s.scalars(
            select(Delivery).where(Delivery.done_at >= month_since)
        )
        for delivery in deliveries:
            if delivery.status not in ("published", "failed"):
                continue
            done_at = delivery.done_at
            day_key = done_at.strftime("%Y-%m-%d")
            month_key = done_at.strftime("%Y-%m")
            if delivery.status == "published":
                monthly[month_key] = monthly.get(month_key, 0) + 1
            if done_at < since or day_key not in daily:
                continue
            daily[day_key][delivery.status] += 1
            item = per_channel.setdefault(
                delivery.channel_id,
                {
                    "title": channels.get(delivery.channel_id, "؟"),
                    "published": 0,
                    "failed": 0,
                },
            )
            item[delivery.status] += 1

        published = sum(item["published"] for item in daily.values())
        failed = sum(item["failed"] for item in daily.values())

        def count_statuses(*statuses: str) -> int:
            return s.scalar(
                select(func.count())
                .select_from(Delivery)
                .where(Delivery.status.in_(statuses))
            ) or 0

        ai_calls = s.scalar(
            select(func.count())
            .select_from(AIUsage)
            .where(AIUsage.at >= since)
        ) or 0

        return {
            "days": [{"date": key, **value} for key, value in daily.items()],
            "channels": sorted(
                per_channel.values(), key=lambda item: item["title"].casefold()
            ),
            "monthly": [
                {"month": key, "published": value}
                for key, value in sorted(monthly.items())[-12:]
            ],
            "totals": {
                "published": published,
                "failed": failed,
                "scheduled": count_statuses("scheduled"),
                "queue": count_statuses("queued", "sending"),
                "failure_rate": round(failed / (published + failed), 3)
                if published + failed else 0,
                "ai_calls": ai_calls,
            },
        }


SOURCES: list[AnalyticsSource] = [LocalOperationsSource()]


def collect(s, days: int) -> dict:
    return SOURCES[0].collect(s, days)
