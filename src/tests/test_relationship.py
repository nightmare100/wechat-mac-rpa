"""关系状态按当前回复对象判定，夸奖不升级。"""
import xml.etree.ElementTree as ET

from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.reply.relationship import describe_relationship
from src.tools import get_registry, register_builtin_tools

_REGISTRY = get_registry()
register_builtin_tools(_REGISTRY)


def _msg(text, sender="甲", kind=SenderType.OTHER):
    return ChatMessage(text=text, sender=sender, sender_type=kind, chat_name="群")


def test_relationship_follows_the_person_being_answered():
    assert describe_relationship([_msg("你真可爱")], [])[1] == "stranger"
    assert describe_relationship([_msg("在吗")], [_msg("以前打过招呼")])[1] == "known"
    assert describe_relationship(
        [_msg("再发个语音")],
        [_msg("发个语音听听"), _msg("@甲 不发语音", kind=SenderType.SELF)],
    )[1] == "refused"
    person, level, _rules = describe_relationship([_msg("你真可爱", sender="朧月")], [])
    assert person == "朧月" and level == "buddy"


def test_prompt_contains_relationship_for_the_current_sender():
    generator = ReplyGenerator(llm_client=None, tool_registry=_REGISTRY)
    unread = _msg("@囡囡 你真可爱", sender="乙")
    prompt = generator._build_user_prompt([unread], [unread], is_group=True)
    relation = ET.fromstring(prompt).find("./session/relationship")

    assert relation.attrib == {"person": "乙", "level": "stranger"}
    assert "夸奖" in relation.text
