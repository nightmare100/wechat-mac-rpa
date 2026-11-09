"""答应朧月上线后的静默期：只放行朧月 @ 囡囡的消息，其余标记已处理。"""

import time
from types import SimpleNamespace
from unittest.mock import Mock

from src.bot.wechat_bot import WeChatBot, _is_oborozuki
from src.reply.generator import ReplyGenerator
from src.tools import get_registry


def _msg(sender, at=False):
    return SimpleNamespace(sender=sender, is_at_me=at, text="x")


def _bot():
    return SimpleNamespace(logger=Mock(), global_store=Mock(), _online_until={})


def test_oborozuki_name_variants():
    for name in ["Mr丿朧月", "Mrゞ胧月", "Mr✕朧月", "Mr乚胧月"]:
        assert _is_oborozuki(name)
    assert not _is_oborozuki("李桑")
    assert not _is_oborozuki("")


def test_start_requires_oborozuki_in_batch():
    bot = _bot()
    WeChatBot._start_online_silence(bot, "群", [_msg("李桑")])
    assert bot._online_until == {}
    WeChatBot._start_online_silence(bot, "群", [_msg("Mrゞ胧月")])
    remaining = bot._online_until["群"] - time.monotonic()
    assert 20 * 60 - 5 <= remaining <= 30 * 60


def test_no_silence_passes_through():
    bot = _bot()
    msgs = [_msg("李桑", at=True)]
    assert WeChatBot._filter_online_silence(bot, "群", msgs, msgs) == msgs
    bot.global_store.mark_replied.assert_not_called()


def test_silence_only_allows_oborozuki_at():
    bot = _bot()
    bot._online_until["群"] = time.monotonic() + 600
    obo_at = _msg("Mr丿朧月", at=True)
    obo_plain = _msg("Mr丿朧月")
    other_at = _msg("李桑", at=True)
    unreplied = [obo_plain, other_at, obo_at]
    assert WeChatBot._filter_online_silence(bot, "群", unreplied, unreplied) == [obo_at]
    marked = [c.args[1] for c in bot.global_store.mark_replied.call_args_list]
    assert marked == [obo_plain, other_at]


def test_silence_expires():
    bot = _bot()
    bot._online_until["群"] = time.monotonic() - 1
    msgs = [_msg("李桑", at=True)]
    assert WeChatBot._filter_online_silence(bot, "群", msgs, msgs) == msgs
    assert "群" not in bot._online_until


class _LLM:
    def __init__(self, content):
        self.content = content

    def chat(self, *args, **kwargs):
        return self.content


def _go_online_after(content):
    from src.models.base import ChatMessage, SenderType
    gen = ReplyGenerator(llm_client=_LLM(content), tool_registry=get_registry())
    msg = ChatMessage(text="@囡囡 上号", sender="Mr丿朧月", sender_type=SenderType.OTHER, chat_name="群")
    replies = gen.generate([msg], [msg], is_group=True)
    return replies, gen.last_go_online


def test_generator_parses_go_online():
    replies, flag = _go_online_after('{"replies":["好呀 马上上"],"go_online":true}')
    assert replies and flag is True


def test_generator_go_online_defaults_false():
    replies, flag = _go_online_after('{"replies":["在的"]}')
    assert replies and flag is False
    _, flag = _go_online_after('{"replies":["好呀"],"go_online":"yes"}')
    assert flag is False
