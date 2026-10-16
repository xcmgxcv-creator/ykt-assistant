"""纯时间工具。

从上游超星版的 ``xxt/parse.py`` 搬过来的三个函数：它们只依赖标准库，
放在这里可以让 UI（``xxt/ui/widget.py``）和提醒引擎（``xxt/notify.py``）
不再依赖那个塞满学习通页面解析的模块。

雨课堂的截止时间本身已经是完整时间戳（毫秒），这三个函数只用于兜底：
把各平台五花八门的时间文本统一成 datetime，以及把剩余时间说成人话。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

__all__ = ["nearest_datetime_for", "parse_full_datetime", "humanize_delta"]


def nearest_datetime_for(month_day: str, today: date) -> datetime | None:
    """把只有 "MM-DD HH:MM"、没有年份的时间补成完整 datetime。

    取去年/今年/明年里离今天最近的那个年份，这样 12 月留的作业 1 月截止
    也能排对顺序。解析失败返回 None。
    """
    import re

    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", str(month_day or "").strip())
    if not m:
        return None
    mo, day, hh, mm = map(int, m.groups())
    best: datetime | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = datetime(year, mo, day, hh, mm)
        except ValueError:
            continue
        if best is None or abs((cand.date() - today).days) < abs((best.date() - today).days):
            best = cand
    return best


def parse_full_datetime(text: str) -> datetime | None:
    """解析带年份的时间（如 '2026-03-20 14:00'）。"""
    import re

    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})",
                  str(text or ""))
    if not m:
        return None
    y, mo, d, hh, mi = map(int, m.groups())
    try:
        return datetime(y, mo, d, hh, mi)
    except ValueError:
        return None


def humanize_delta(delta: timedelta | None) -> str:
    """把剩余时间说成人话：'还有 3 天 5 小时' / '已过期 2 小时'。"""
    if delta is None:
        return "截止未知"
    seconds = int(delta.total_seconds())
    overdue = seconds < 0
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days >= 1:
        text = f"{days} 天" + (f" {hours} 小时" if hours else "")
    elif hours >= 1:
        text = f"{hours} 小时" + (f" {minutes} 分" if minutes and hours < 6 else "")
    else:
        text = f"{minutes} 分钟" if minutes else "不到 1 分钟"
    return f"已过期 {text}" if overdue else f"还有 {text}"
