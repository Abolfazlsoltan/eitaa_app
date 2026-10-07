"""AI provider abstraction. Add a new provider by subclassing AIProvider and registering it."""
import os, httpx
from abc import ABC, abstractmethod

class AIError(Exception): pass

class AIProvider(ABC):
    @abstractmethod
    async def complete(self, system: str, user: str) -> str: ...

class AnthropicProvider(AIProvider):
    async def complete(self, system, user):
        key = os.getenv("AI_API_KEY")
        if not key: raise AIError("کلید ارائه‌دهنده هوش مصنوعی تنظیم نشده است (AI_API_KEY).")
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={"model": os.getenv("AI_MODEL", "claude-sonnet-4-6"), "max_tokens": 1200, "system": system,
                          "messages": [{"role": "user", "content": user}]})
            j = r.json()
        except Exception:
            raise AIError("ارتباط با سرویس هوش مصنوعی برقرار نشد.")
        if r.status_code != 200: raise AIError("سرویس هوش مصنوعی خطا داد. کلید و مدل را بررسی کنید.")
        return "".join(b.get("text", "") for b in j.get("content", [])).strip()

PROVIDERS = {"anthropic": AnthropicProvider}
def get_provider() -> AIProvider:
    return PROVIDERS[os.getenv("AI_PROVIDER", "anthropic")]()

SYSTEM = "تو دستیار تولید محتوا برای کانال‌های ایتا هستی. فقط متن نهایی را به فارسی روان بنویس، بدون توضیح اضافه."
ACTIONS = {
    "create": "از اطلاعات زیر یک پست مناسب کانال بساز:", "rewrite": "متن زیر را بازنویسی کن:",
    "shorter": "متن زیر را کوتاه‌تر کن و نکات اصلی را نگه دار:", "longer": "متن زیر را کامل‌تر و با جزئیات بیشتر بنویس:",
    "formal": "لحن متن زیر را رسمی کن:", "casual": "لحن متن زیر را خودمانی کن:",
    "news": "متن زیر را به سبک خبری بنویس:", "educational": "متن زیر را به سبک آموزشی بنویس:",
    "engaging": "متن زیر را جذاب‌تر کن:", "titles": "پنج عنوان پیشنهادی برای متن زیر بنویس (هر عنوان در یک خط):",
    "hashtags": "هشتگ‌های مناسب برای متن زیر پیشنهاد بده (در یک خط):", "translate": "متن زیر را ترجمه کن (فارسی به انگلیسی و غیرفارسی به فارسی):",
    "summarize": "متن زیر را خلاصه کن:", "proofread": "غلط‌های نگارشی و املایی متن زیر را اصلاح کن و معنا را تغییر نده:",
}
