"""群里没 @ 我的连发消息：等对方说完再一起回；@ / 提到我 / 引用不等。"""

from types import SimpleNamespace

from src.bot.wechat_bot import BURST_MAX_WAIT_SECONDS, BURST_QUIET_SECONDS, WeChatBot
from src.models.base import ChatMessage, SenderType


def _m(text, quoted=""):
    return ChatMessage(text=text, sender="许愿大王", sender_type=SenderType.OTHER, chat_name="g", quoted_text=quoted)


def _wait(bot, msgs, now):
    return WeChatBot._wait_for_burst(bot, "g", msgs, now=now)


def test_waits_until_quiet_then_replies_to_all():
    bot = SimpleNamespace(_first_seen={})
    a = _m("生气了")
    assert _wait(bot, [a], 0)
    b = _m("还是过不去这关")
    assert _wait(bot, [a, b], 10)
    assert _wait(bot, [a, b], 10 + BURST_QUIET_SECONDS - 1)
    assert not _wait(bot, [a, b], 10 + BURST_QUIET_SECONDS)


def test_max_wait_caps_nonstop_chatter():
    bot = SimpleNamespace(_first_seen={})
    msgs, t = [], 0
    while t < BURST_MAX_WAIT_SECONDS:
        msgs.append(_m(f"第{t}条"))
        assert _wait(bot, list(msgs), t)
        t += 10
    msgs.append(_m("还在说"))
    assert not _wait(bot, msgs, BURST_MAX_WAIT_SECONDS)


def test_at_named_or_quote_does_not_wait(monkeypatch):
    monkeypatch.setenv("BOT_AT_KEYWORD", "囡囡")
    bot = SimpleNamespace(_first_seen={})
    assert not _wait(bot, [_m("@『宵桜』囡囡 在吗")], 0)
    assert not _wait(bot, [_m("囡囡呢")], 0)
    assert not _wait(bot, [_m("哈哈", quoted="『宵桜』囡囡: 欧皇")], 0)


def test_works_without_attribute_on_fake_bot():
    assert _wait(SimpleNamespace(), [_m("生气了")], 0)
