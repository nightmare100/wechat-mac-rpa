"""主动插话（chime_in）：每个群冷却期内只能插一次；冷却中误标 chime_in 的回复，只有在没有任何现有触发时才丢弃。"""

import pytest

from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.tools import get_registry


class _LLM:
    def __init__(self, content):
        self.content = content

    def chat(self, *args, **kwargs):
        return self.content


CHIME = '{"replies":["@得闲射击 欧皇"],"chime_in":true}'


def _gen(content=CHIME):
    return ReplyGenerator(llm_client=_LLM(content), tool_registry=get_registry())


def _m(sender, text):
    return ChatMessage(text=text, sender=sender, sender_type=SenderType.OTHER, chat_name="群")


def _run(gen, msg):
    return gen.generate([msg], [msg], is_group=True)


@pytest.fixture(autouse=True)
def no_self_refine(monkeypatch):
    monkeypatch.setenv("ENABLE_SELF_REFINE", "0")


def test_chime_in_starts_cooldown_and_blocks_next_chime_in():
    gen = _gen()
    assert _run(gen, _m("得闲射击", "100 抽 5 件套"))
    assert not gen.chime_in_allowed("群")
    assert _run(gen, _m("既梦", "300次 歪到姥姥家了")) == []
    assert gen.chime_in_allowed("别的群")


def test_cooldown_does_not_drop_followup_reply():
    gen = _gen()
    _run(gen, _m("得闲射击", "100 抽 5 件套"))
    assert _run(gen, _m("得闲射击", "哈哈 运气好"))


def test_cooldown_does_not_drop_named_or_at_reply():
    gen = _gen()
    _run(gen, _m("得闲射击", "100 抽 5 件套"))
    assert _run(gen, _m("既梦", "囡囡也不安慰我"))
    assert _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))


def test_trigger_reply_flagged_chime_in_does_not_start_cooldown():
    gen = _gen()
    _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))
    assert gen.chime_in_allowed("群")


def test_cooldown_expires(monkeypatch):
    gen = _gen()
    _run(gen, _m("得闲射击", "100 抽 5 件套"))
    gen.chime_in_last["群"] -= gen.chime_in_cooldown + 1
    assert gen.chime_in_allowed("群")
    assert _run(gen, _m("既梦", "300次 歪到姥姥家了"))


def test_cooldown_minutes_from_env(monkeypatch):
    monkeypatch.setenv("CHIME_IN_COOLDOWN_MINUTES", "0")
    gen = _gen()
    _run(gen, _m("得闲射击", "100 抽 5 件套"))
    assert gen.chime_in_allowed("群")


def test_plain_reply_without_flag_is_untouched():
    gen = _gen('{"replies":["@相缺 在哒"]}')
    assert _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))
    assert gen.chime_in_allowed("群")


def test_session_tells_llm_whether_chime_in_allowed():
    gen = _gen()
    msg = _m("得闲射击", "100 抽 5 件套")
    assert "<chime_in_allowed>true</chime_in_allowed>" in gen._build_user_prompt([msg], [msg], is_group=True)
    _run(gen, msg)
    assert "<chime_in_allowed>false</chime_in_allowed>" in gen._build_user_prompt([msg], [msg], is_group=True)
