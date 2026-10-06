"""Persian helpers: digits, Jalali dates, local-time conversion."""
import datetime as dt
FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
def digits(s) -> str: return str(s).translate(FA)

def g2j(gy, gm, gd):
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053); days %= 12053
    jy += 4 * (days // 1461); days %= 1461
    if days > 365: jy += (days - 1) // 365; days = (days - 1) % 365
    jm = 1 + days // 31 if days < 186 else 7 + (days - 186) // 30
    jd = 1 + (days % 31 if days < 186 else (days - 186) % 30)
    return jy, jm, jd

def offset() -> dt.timedelta: return dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
def to_local(s: str) -> dt.datetime: return dt.datetime.fromisoformat(s.split("+")[0].replace("Z", "")) + offset()
def to_utc_iso(local: dt.datetime) -> str: return (local - offset()).isoformat(timespec="seconds")

def jdate(d: dt.datetime) -> str:
    y, m, day = g2j(d.year, d.month, d.day); return digits(f"{day} {MONTHS[m - 1]} {y}")
def jfull(d: dt.datetime) -> str: return jdate(d) + digits(f" — {d:%H:%M}")
def jfull_utc(s: str) -> str: return jfull(to_local(s))
