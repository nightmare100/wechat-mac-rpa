"""发送失败退避、单批重试上限与全局连续失败自动退出单测。"""

from unittest.mock import Mock

from src.bot.wechat_bot import WeChatBot
from src.models.base import ChatMessage, SenderType


def _stub_bot(retry_limit: int = 3, max_total: int = 5) -> WeChatBot:
    bot = WeChatBot.__new__(WeChatBot)
    bot.running = True
    bot.logger = Mock()
    bot.global_store = Mock()
    bot._consecutive_send_failures = 0
    bot._chat_send_failures = {}
    bot._chat_send_backoff_until = {}
    bot._send_retry_limit = retry_limit
    bot._max_consecutive_send_failures = max_total
    return bot


def _msg(text: str = "hi", chat: str = "人格测试") -> ChatMessage:
    return ChatMessage(text=text, sender="李桑", sender_type=SenderType.OTHER, chat_name=chat)


def test_backoff_and_per_chat_retry_limit():
    bot = _stub_bot(retry_limit=3, max_total=5)
    m = _msg()

    # 第 1 次失败：退避 15s，不放弃未读，不退出
    assert bot._record_send_outcome("人格测试", succeeded=False, send_failed=True, unreplied=[m], now=100.0) is False
    assert bot._in_send_backoff("人格测试", now=110.0) is True
    assert bot._in_send_backoff("人格测试", now=116.0) is False
    bot.global_store.mark_replied.assert_not_called()
    assert bot.running is True

    # 第 2 次失败：退避 30s
    assert bot._record_send_outcome("人格测试", succeeded=False, send_failed=True, unreplied=[m], now=120.0) is False
    assert bot._in_send_backoff("人格测试", now=145.0) is True
    assert bot._in_send_backoff("人格测试", now=151.0) is False

    # 第 3 次失败：触发单批重试上限，自动 mark_replied 放弃该批并清理该群退避
    assert bot._record_send_outcome("人格测试", succeeded=False, send_failed=True, unreplied=[m], now=200.0) is True
    bot.global_store.mark_replied.assert_called_once_with("人格测试", m, "")
    assert bot._in_send_backoff("人格测试", now=201.0) is False
    assert bot.running is True


def test_global_consecutive_failures_exits_bot():
    bot = _stub_bot(retry_limit=3, max_total=5)
    m1 = _msg(chat="群A")
    m2 = _msg(chat="群B")

    for i in range(4):
        bot._record_send_outcome("群A" if i < 3 else "群B", succeeded=False, send_failed=True, unreplied=[m1 if i < 3 else m2], now=100.0 + i)
        assert bot.running is True

    # 第 5 次连续失败：触发全局上限，自动停止主循环
    bot._record_send_outcome("群B", succeeded=False, send_failed=True, unreplied=[m2], now=200.0)
    assert bot.running is False


def test_success_resets_all_failure_counters():
    bot = _stub_bot(retry_limit=3, max_total=5)
    m = _msg()

    bot._record_send_outcome("人格测试", succeeded=False, send_failed=True, unreplied=[m], now=100.0)
    bot._record_send_outcome("人格测试", succeeded=False, send_failed=True, unreplied=[m], now=120.0)
    assert bot._consecutive_send_failures == 2

    bot._record_send_outcome("人格测试", succeeded=True, send_failed=False, unreplied=[m], now=160.0)
    assert bot._consecutive_send_failures == 0
    assert bot._in_send_backoff("人格测试", now=160.0) is False
    assert bot.running is True
