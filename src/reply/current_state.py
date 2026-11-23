"""角色自己的心情和精力。

支持两种模式：
1. 传入持久化 MoodEngine 时，综合个人短期情绪残留、群级氛围、时间衰减、睡前时段与聊天疲劳度返回状态；
2. 未传入 mood_engine 时，保持原有睡前二元静态回退，确保既有调用与单测完全兼容。
"""
from __future__ import annotations

from typing import Optional, Tuple

from src.reply.mood_engine import MOOD_RULES, MoodEngine


def current_state(
    in_bedtime: bool,
    *,
    mood_engine: Optional[MoodEngine] = None,
    chat_key: str = "",
    user_key: Optional[str] = None,
) -> Tuple[str, str, str]:
    if mood_engine is not None and chat_key:
        return mood_engine.get_state(chat_key, user_key=user_key, in_bedtime=in_bedtime)
    if in_bedtime:
        return ("犯困", "低", MOOD_RULES["犯困"])
    return ("平静", "平常", MOOD_RULES["平静"])
