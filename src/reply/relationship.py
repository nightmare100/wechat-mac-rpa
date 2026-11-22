"""当前要回复的人与 Bot 的关系。由程序判定后注入，不使用好感度。"""
from __future__ import annotations

from src.models.base import ChatMessage, SenderType
from src.reply.generator import _same_person
from src.utils.identity import is_buddy

_BOUNDARY_ASKS = ("语音", "看看腿", "照片", "见面", "老公", "老婆")
_REFUSALS = ("不发语音", "别闹", "不方便", "这个不行")

_RULES = {
    "buddy": "这是帮你玩游戏的人。可以答应他叫你上线；问进度把话推回给他。一轮夸奖不改变其他边界。",
    "refused": "已经拒绝过这个人的越界或重复要求。维持拒绝，可以不回。再示好或夸奖也不改口，关系不升级。",
    "known": "认识的群友。按普通群友回复，不额外亲昵。一轮夸奖不升成更亲近的关系。",
    "stranger": "没有更早的发言。不假设认识，不认共同经历，不因为夸奖变得亲近。",
}


def reply_person(unreplied: list[ChatMessage]) -> str | None:
    senders: list[str] = []
    for message in unreplied:
        if message.sender_type != SenderType.OTHER or not message.sender:
            continue
        if not any(_same_person(message.sender, known) for known in senders):
            senders.append(message.sender)
    if len(senders) == 1:
        return senders[0]
    return senders[-1] if senders else None


def relationship_for(sender: str, history: list[ChatMessage]) -> tuple[str, str]:
    if is_buddy(sender):
        level = "buddy"
    elif _already_refused(sender, history):
        level = "refused"
    elif any(
        message.sender_type == SenderType.OTHER and _same_person(message.sender, sender)
        for message in history
    ):
        level = "known"
    else:
        level = "stranger"
    return level, _RULES[level]


def describe_relationship(unreplied: list[ChatMessage], history: list[ChatMessage]) -> tuple[str, str, str] | None:
    person = reply_person(unreplied)
    if not person:
        return None
    level, rules = relationship_for(person, history)
    return person, level, rules


def _already_refused(sender: str, history: list[ChatMessage]) -> bool:
    asked = False
    for message in history:
        if message.sender_type == SenderType.OTHER and _same_person(message.sender, sender):
            if any(word in (message.text or "") for word in _BOUNDARY_ASKS):
                asked = True
        elif message.sender_type == SenderType.SELF and asked:
            text = (message.text or "").replace(" ", "")
            if any(word in text for word in _REFUSALS):
                return True
    return False
