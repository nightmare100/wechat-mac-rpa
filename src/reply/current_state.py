"""角色自己的心情和精力。由睡前时段决定，不随某一句夸奖或吐槽改写。"""
from __future__ import annotations


def current_state(in_bedtime: bool) -> tuple[str, str, str]:
    if in_bedtime:
        return (
            "犯困",
            "低",
            "可以更短，可以露出犯困。不改变事实、拒绝和 relationship。别人没说要睡时，不要催别人休息。",
        )
    return (
        "平静",
        "平常",
        "按平时的长度和语气。不改变事实、拒绝和 relationship。",
    )
