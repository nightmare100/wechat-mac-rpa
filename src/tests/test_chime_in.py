"""主动插话（chime_in）：每个群冷却期内只能插一次；冷却中误标 chime_in 的回复，只有在没有任何现有触发时才丢弃。"""

import time

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
    gen = ReplyGenerator(llm_client=_LLM(content), tool_registry=get_registry())
    gen._started_at -= gen.chime_in_cooldown + 1  # 跳过启动冷却
    return gen


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
    gen.llm_client.content = '{"replies":["@既梦 抱抱"],"chime_in":true}'
    assert _run(gen, _m("既梦", "囡囡也不安慰我"))
    gen.llm_client.content = '{"replies":["@相缺 在哒"],"chime_in":true}'
    assert _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))


def test_trigger_reply_flagged_chime_in_does_not_start_cooldown():
    gen = _gen('{"replies":["@相缺 在哒"],"chime_in":true}')
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


def test_unflagged_chime_in_still_counts():
    """LLM 没标 chime_in、但回复对象没有任何触发：照样算插话，开始冷却；冷却中丢弃。"""
    gen = _gen('{"replies":["@花间灼晚意 摸摸 明天再排一次"]}')
    assert _run(gen, _m("花间灼晚意", "昨天上船今天3票没上车"))
    assert not gen.chime_in_allowed("群")
    gen.llm_client.content = '{"replies":["@许愿大王 明天再排一次"]}'
    assert _run(gen, _m("许愿大王", "明天也没上怎么办")) == []
    assert gen.last_chime_in_dropped is True


def test_unmatched_target_falls_back_to_whole_batch():
    """回复里 @ 的昵称对不上任何未读发送者时按整批判断，批里有 @ 就不当插话丢弃。"""
    gen = _gen('{"replies":["@相缺缺缺 在哒"]}')
    gen.chime_in_last["群"] = time.time()
    assert _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))


def test_session_tells_llm_whether_chime_in_allowed():
    gen = _gen()
    msg = _m("得闲射击", "100 抽 5 件套")
    assert "<chime_in_allowed>true</chime_in_allowed>" in gen._build_user_prompt([msg], [msg], is_group=True)
    _run(gen, msg)
    assert "<chime_in_allowed>false</chime_in_allowed>" in gen._build_user_prompt([msg], [msg], is_group=True)


MIXED = '{"replies":["@相缺 在哒","@得闲射击 欧皇"],"chime_in":true}'


def _mixed_batch():
    return [_m("相缺", "@『宵桜』囡囡 在吗"), _m("得闲射击", "100 抽 5 件套")]


def test_mixed_batch_starts_cooldown_and_gives_rounds_to_chime_target():
    """同批既有被 @ 的回复又有插话：两条都发，插话照样开始冷却，插话对象也拿到接话额度。"""
    gen = _gen(MIXED)
    batch = _mixed_batch()
    assert len(gen.generate(batch, batch, is_group=True)) == 2
    assert not gen.chime_in_allowed("群")
    assert {s for s, _ in gen.followup.candidates("群")} == {"相缺", "得闲射击"}


def test_mixed_batch_in_cooldown_drops_only_chime_reply():
    gen = _gen(MIXED)
    gen.chime_in_last["群"] = time.time()
    batch = _mixed_batch()
    replies = gen.generate(batch, batch, is_group=True)
    assert len(replies) == 1 and replies[0].endswith("在哒")
    assert gen.last_chime_in_dropped is True
    assert [s for s, _ in gen.followup.candidates("群")] == ["相缺"]


def test_dropped_flag_resets_on_next_generate():
    gen = _gen()
    _run(gen, _m("得闲射击", "100 抽 5 件套"))
    assert _run(gen, _m("既梦", "300次 歪到姥姥家了")) == []
    assert gen.last_chime_in_dropped is True
    gen.llm_client.content = '{"replies":["@相缺 在哒"]}'
    _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))
    assert gen.last_chime_in_dropped is False


def test_no_chime_in_right_after_startup():
    """刚启动时视为刚插过话：屏幕上的旧消息不插话，被 @ 照常回。"""
    gen = ReplyGenerator(llm_client=_LLM(CHIME), tool_registry=get_registry())
    assert not gen.chime_in_allowed("群")
    assert _run(gen, _m("得闲射击", "100 抽 5 件套")) == []
    gen.llm_client.content = '{"replies":["@相缺 在哒"]}'
    assert _run(gen, _m("相缺", "@『宵桜』囡囡 在吗"))


@pytest.mark.parametrize("quote", ["李桑: 今天手气不错", "今天手气不错", "李桑: 囡囡今天手气不错"])
def test_quoting_others_does_not_bypass_cooldown(quote):
    gen = _gen('{"replies":["@相缺 好哒"]}')
    gen.chime_in_last["群"] = time.time()
    msg = _m("相缺", "确实")
    msg.quoted_text = quote
    assert _run(gen, msg) == []
    assert gen.followup.candidates("群") == []


@pytest.mark.parametrize("quote", ["『宵桜』囡囡: 今天手气不错", "今天手气不错"])
def test_quoting_self_bypasses_cooldown_and_opens_followup(quote):
    gen = _gen('{"replies":["@相缺 好哒"]}')
    gen.chime_in_last["群"] = time.time()
    before = gen.chime_in_last["群"]
    own = ChatMessage(text="今天手气不错", sender="自己", sender_type=SenderType.SELF, chat_name="群")
    msg = _m("相缺", "确实")
    msg.quoted_text = quote
    assert gen.generate([msg], [own, msg], is_group=True)
    assert gen.chime_in_last["群"] == before
    assert dict(gen.followup.candidates("群"))["相缺"] >= 2


def test_quote_by_other_author_is_not_self_even_with_identical_content():
    gen = _gen('{"replies":["@相缺 好哒"]}')
    gen.chime_in_last["群"] = time.time()
    own = ChatMessage(text="今天手气不错", sender="自己", sender_type=SenderType.SELF, chat_name="群")
    msg = _m("相缺", "确实")
    msg.quoted_text = "李桑: 今天手气不错"
    assert gen.generate([msg], [own, msg], is_group=True) == []


def test_quoting_others_when_allowed_starts_chime_cooldown():
    gen = _gen('{"replies":["@相缺 好哒"]}')
    msg = _m("相缺", "确实")
    msg.quoted_text = "李桑: 今天手气不错"
    assert _run(gen, msg)
    assert not gen.chime_in_allowed("群")


@pytest.mark.parametrize(
    "quote,own_text,own_chat,expected",
    [
        ("今天又打了好多关还是没出货", "今天又打了好多关还是没出货呀", "群", True),
        ("今天手气不错", "今天手气不错", "别的群", False),
        ("你好", "你好吗", "群", False),
    ],
)
def test_unattributed_quote_matches_only_reliable_self_history(quote, own_text, own_chat, expected):
    from src.reply.generator import _quotes_self

    msg = _m("相缺", "确实")
    msg.quoted_text = quote
    own = ChatMessage(text=own_text, sender="自己", sender_type=SenderType.SELF, chat_name=own_chat)
    assert _quotes_self(msg, [own]) is expected


@pytest.mark.parametrize("chime,direct,expected", [
    ("0", "1", ["甲", "乙"]),
    ("1", "0", ["丙"]),
    ("0", "0", []),
    ("1", "1", ["甲", "乙", "丙"]),
])
def test_group_reply_switches_filter_incoming_routes(monkeypatch, chime, direct, expected):
    monkeypatch.setenv("ENABLE_CHIME_IN", chime)
    monkeypatch.setenv("ENABLE_DIRECT_GROUP_REPLY", direct)
    monkeypatch.setenv("BOT_AT_KEYWORD", "囡囡")
    monkeypatch.setenv("BOT_NAME", "囡囡")
    gen = _gen()
    mention = _m("甲", "@囡囡 在吗")
    quote = _m("乙", "说得对")
    quote.quoted_text = "囡囡：上一条"
    ordinary = _m("丙", "今天运气不错")
    messages = [mention, quote, ordinary]
    assert [m.sender for m in gen.group_reply_candidates("群", messages, [])] == expected


def test_chime_off_is_enforced_even_after_cooldown(monkeypatch):
    monkeypatch.setenv("ENABLE_CHIME_IN", "0")
    gen = _gen()
    assert not gen.chime_in_allowed("群")
    assert _run(gen, _m("得闲射击", "100 抽 5 件套")) == []
    assert gen.followup.candidates("群") == []


def test_chime_followup_rounds_and_blocked_after_other_messages():
    gen = _gen()
    gen.followup.randint = lambda a, b: 1
    first = _m("得闲射击", "100 抽 5 件套")
    assert _run(gen, first)
    assert gen.followup.candidates("群") == [("得闲射击", 1)]
    # 立即接话消耗完额度即清空；下一轮在冷却期内被拦住
    assert _run(gen, _m("得闲射击", "哈哈 运气好"))
    assert gen.followup.candidates("群") == []
    assert _run(gen, _m("得闲射击", "再来一把")) == []
    assert gen.last_chime_in_dropped is True

    # 重新放行一次插话后，若中间插了超过上限的其他人消息，冷却期内也不允许免 CD 接话
    from src.reply.generator import FOLLOWUP_MAX_OTHER_MESSAGES

    gen.chime_in_last["群"] -= gen.chime_in_cooldown + 1
    assert _run(gen, _m("得闲射击", "又出货了"))
    self_msg = ChatMessage(text="@得闲射击 欧皇", sender="自己", sender_type=SenderType.SELF, chat_name="群")
    next_msg = _m("得闲射击", "顺便问下")
    history = [self_msg] + [_m(f"路人{i}", str(i)) for i in range(FOLLOWUP_MAX_OTHER_MESSAGES + 1)] + [next_msg]
    assert gen.generate([next_msg], history, is_group=True) == []
    assert gen.last_chime_in_dropped is True


def test_chime_in_blocks_at_other_person_without_starting_cooldown():
    gen = _gen('{"replies":["@2kk 快把手机藏好"],"chime_in":true}')
    # 2kk 定向 @别人，主动插话应被拦截，且不消耗冷却时间
    at_other = _m("2kk", "@余乐无忧（心享）\u2005主管来了")
    assert _run(gen, at_other) == []
    assert gen.last_chime_in_dropped is True
    assert "群" not in gen.chime_in_last

    # 包含邮箱（非 @别人）的普通消息不受影响，正常放行主动插话
    email_msg = _m("2kk", "联系 test@example.com 抽到红装了")
    assert _run(gen, email_msg) == ["快把手机藏好"]

    # @Bot 自己仍正常直接回复，不被拦截
    gen2 = _gen('{"replies":["@2kk 在呀"]}')
    at_bot = _m("2kk", "@『宵桜』囡囡 在吗")
    assert _run(gen2, at_bot) == ["在呀"]


def test_chime_in_quiet_hours_blocks_only_chime_in(monkeypatch):
    from datetime import datetime

    monkeypatch.setenv("CHIME_IN_QUIET_HOURS", "00:30-07:30")
    gen = _gen()

    # 深夜静默时段内：不允许主动插话，且不消耗冷却
    assert gen.in_chime_in_quiet_hours("群", now=datetime(2026, 11, 26, 1, 23)) is True
    assert gen.chime_in_allowed("群", now=datetime(2026, 11, 26, 1, 23)) is False
    assert gen.chime_in_allowed("群", now=datetime(2026, 11, 26, 3, 31)) is False
    # 白天/晚间正常时段：允许主动插话
    assert gen.in_chime_in_quiet_hours("群", now=datetime(2026, 11, 26, 0, 29)) is False
    assert gen.chime_in_allowed("群", now=datetime(2026, 11, 26, 7, 30)) is True
    assert gen.chime_in_allowed("群", now=datetime(2026, 11, 26, 18, 10)) is True

    # 强制模拟当前处于深夜静默时段：主动插话被拦截，但 @Bot 直接回复照常放行
    monkeypatch.setattr(gen, "in_chime_in_quiet_hours", lambda chat_name="", now=None: True)
    msg = _m("既梦", "红歪了一个")
    assert "<chime_in_allowed>false</chime_in_allowed>" in gen._build_user_prompt([msg], [msg], is_group=True)
    assert _run(gen, msg) == []
    assert gen.last_chime_in_dropped is True
    assert "群" not in gen.chime_in_last

    gen.llm_client.content = '{"replies":["@相缺 在哒"],"chime_in":true}'
    assert _run(gen, _m("相缺", "@『宵桜』囡囡 在吗")) == ["在哒"]


def test_chime_in_quiet_hours_default_and_group_override(monkeypatch):
    from datetime import datetime

    # 未配置环境变量时默认启用 00:30-07:30
    monkeypatch.delenv("CHIME_IN_QUIET_HOURS", raising=False)
    gen_default = _gen()
    assert gen_default.chime_in_allowed("群", now=datetime(2026, 11, 26, 2, 0)) is False
    assert gen_default.chime_in_allowed("群", now=datetime(2026, 11, 26, 12, 0)) is True

    # 支持通过 group_config 按群关闭或自定义静默时段
    class _GroupCfg:
        def is_enabled(self, chat_name, key, default=True):
            return default

        def get(self, chat_name, key, default=None):
            if key == "chime_in_quiet_hours":
                if chat_name == "不眠群":
                    return "off"
                if chat_name == "早睡群":
                    return "23:00-08:00"
            return default

    gen_default.group_config = _GroupCfg()
    assert gen_default.chime_in_allowed("不眠群", now=datetime(2026, 11, 26, 2, 0)) is True
    assert gen_default.chime_in_allowed("早睡群", now=datetime(2026, 11, 26, 23, 15)) is False
    assert gen_default.chime_in_allowed("早睡群", now=datetime(2026, 11, 26, 8, 5)) is True
