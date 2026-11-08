"""每日时段（HH:MM-HH:MM，支持跨午夜）：休眠 SLEEP_HOURS、睡前 BEDTIME_HOURS。

休眠时段内主循环不截图、不调 API、不定时推送；醒来后每个聊天第一次看到的积压消息不回复。
"""

from datetime import datetime, time
from typing import Optional, Tuple

SleepWindow = Tuple[time, time]


def parse_sleep_hours(value: str, name: str = "SLEEP_HOURS") -> Optional[SleepWindow]:
    """解析 "HH:MM-HH:MM"，空串返回 None，格式错误抛 ValueError。"""
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"{name} 格式应为 HH:MM-HH:MM: {value!r}")
    start, end = (datetime.strptime(p.strip(), "%H:%M").time() for p in parts)
    if start == end:
        raise ValueError(f"{name} 起止时间不能相同: {value!r}")
    return start, end


def in_sleep_window(window: Optional[SleepWindow], now: datetime) -> bool:
    """now 是否落在休眠时段内（含起点、不含终点，支持跨午夜）。"""
    if window is None:
        return False
    start, end = window
    t = now.time()
    if start < end:
        return start <= t < end
    return t >= start or t < end
