"""当前要回复的人与 Bot 的关系。由程序判定后注入，不使用好感度数值展示。

支持两种模式：
1. 传入持久化 RelationshipEngine 时，按 (chat_key, user_key) 读取/演进持久化关系（Phase 1）；
2. 未传入 engine 时，保持轻量无状态回退，确保既有单测与独立调用零依赖。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from src.models.base import ChatMessage, SenderType
from src.reply.relationship_store import (
    STAGE_RULES,
    RelationshipEngine,
    _BOUNDARY_ASKS,
    _REFUSALS,
    _clean_sender_name,
    _same_person_name,
)
from src.utils.identity import is_buddy

_LEADING_AT_RE = re.compile(r"^@(\S+)\s+")

_RULES = {
    "buddy": STAGE_RULES["buddy"],
    "refused": STAGE_RULES["refused"],
    "guarded": STAGE_RULES["guarded"],
    "known": STAGE_RULES["acquaintance"],
    "acquaintance": STAGE_RULES["acquaintance"],
    "familiar": STAGE_RULES["familiar"],
    "friend": STAGE_RULES["friend"],
    "stranger": STAGE_RULES["stranger"],
}


def reply_person(unreplied: List[ChatMessage]) -> Optional[str]:
    senders: List[str] = []
    for message in unreplied:
        if message.sender_type != SenderType.OTHER or not message.sender:
            continue
        if not any(_same_person_name(message.sender, known) for known in senders):
            senders.append(message.sender)
    if len(senders) == 1:
        return senders[0]
    return senders[-1] if senders else None


def relationship_for(sender: str, history: List[ChatMessage]) -> Tuple[str, str]:
    if is_buddy(sender):
        level = "buddy"
    elif _already_refused(sender, history):
        level = "refused"
    elif any(
        message.sender_type == SenderType.OTHER and _same_person_name(message.sender, sender)
        for message in history
    ):
        level = "known"
    else:
        level = "stranger"
    return level, _RULES[level]


def describe_relationship(
    unreplied: List[ChatMessage],
    history: List[ChatMessage],
    *,
    engine: Optional[RelationshipEngine] = None,
    chat_name: str = "",
) -> Optional[Tuple[str, str, str]]:
    if engine is not None:
        items = engine.describe_relationships(unreplied, history, chat_name=chat_name, max_people=1)
        return items[-1] if items else None
    person = reply_person(unreplied)
    if not person:
        return None
    level, rules = relationship_for(person, history)
    return person, level, rules


def describe_relationships(
    unreplied: List[ChatMessage],
    history: List[ChatMessage],
    *,
    engine: Optional[RelationshipEngine] = None,
    chat_name: str = "",
    max_people: int = 3,
) -> List[Tuple[str, str, str]]:
    """为本批未读涉及的多位发言人分别返回关系描述（最多 max_people 人）。"""
    if engine is not None:
        return engine.describe_relationships(
            unreplied, history, chat_name=chat_name, max_people=max_people
        )
    if not unreplied:
        return []
    senders: List[str] = []
    for msg in reversed(unreplied):
        if msg.sender_type != SenderType.OTHER or not msg.sender:
            continue
        cleaned = _clean_sender_name(msg.sender)
        if not cleaned or any(_same_person_name(cleaned, s) for s in senders):
            continue
        senders.append(cleaned)
        if len(senders) >= max_people:
            break
    senders.reverse()
    result: List[Tuple[str, str, str]] = []
    for person in senders:
        level, rules = relationship_for(person, history)
        result.append((person, level, rules))
    return result


def _already_refused(sender: str, history: List[ChatMessage]) -> bool:
    """无状态回退下的拒绝检测：若 Bot 拒绝语明确 @ 了其他群友，不误伤当前 sender。"""
    asked = False
    for message in history:
        if message.sender_type == SenderType.OTHER and _same_person_name(message.sender, sender):
            if any(word in (message.text or "") for word in _BOUNDARY_ASKS):
                asked = True
        elif message.sender_type == SenderType.SELF and asked:
            raw_text = message.text or ""
            at_match = _LEADING_AT_RE.match(raw_text)
            if at_match and not _same_person_name(at_match.group(1), sender):
                continue
            text = raw_text.replace(" ", "")
            if any(word in text for word in _REFUSALS):
                return True
    return False
