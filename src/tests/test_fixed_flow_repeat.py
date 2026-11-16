from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.bot.wechat_bot import WeChatBot
from src.models.base import ActionResult, ChatMessage, SenderType


def message(sender='甲', keyword='符文', chat='群', wxid='wx_a'):
    return ChatMessage(text='@囡囡 '+keyword, sender=sender, sender_type=SenderType.OTHER,
                       chat_name=chat, sender_wxid=wxid)


def bot():
    b = WeChatBot.__new__(WeChatBot)
    b.fixed_flow = Mock()
    b.fixed_flow.match.side_effect = lambda m, **kwargs: m.text.split()[-1]
    b.fixed_flow.reply.return_value = '符文攻略\nhttps://archero2hub.com/zh/rune'
    b.generator = Mock()
    b.logger = Mock()
    b.debug_logger = Mock()
    b.global_store = Mock(chats={})
    b._send_text = Mock(return_value=ActionResult(success=True))
    return b


def test_repeat_and_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('src.bot.wechat_bot.time.monotonic', lambda: now[0])
    b = bot()
    m = message()
    b._handle_fixed_flow('群', [m], 1)
    now[0] += 299
    b._handle_fixed_flow('群', [m], 2)
    b._send_text.assert_called_with('群', '回复过啦，请看上面的消息')
    assert b.fixed_flow.reply.call_count == 1
    now[0] += 1
    b._handle_fixed_flow('群', [m], 3)
    assert b.fixed_flow.reply.call_count == 2


@pytest.mark.parametrize('change', ['chat', 'keyword'])
def test_scope_isolation(change):
    b = bot()
    b._handle_fixed_flow('群', [message()], 1)
    kwargs = {'sender': '乙', 'wxid': 'wx_b'} if change == 'sender' else {change: '别的'}
    m = message(**kwargs)
    b._handle_fixed_flow(m.chat_name, [m], 2)
    assert b.fixed_flow.reply.call_count == 2


@pytest.mark.parametrize('kind', ['failed', 'empty'])
def test_failed_or_empty_is_not_cached(kind):
    b = bot()
    if kind == 'failed':
        b._send_text.return_value = ActionResult(success=False)
    else:
        b.fixed_flow.reply.return_value = '未找到二代相关内容'
    b._handle_fixed_flow('群', [message()], 1)
    b._send_text.return_value = ActionResult(success=True)
    b._handle_fixed_flow('群', [message()], 2)
    assert b.fixed_flow.reply.call_count == 2


def test_other_sender_same_group_is_repeat():
    b = bot()
    b._handle_fixed_flow('群', [message()], 1)
    b._handle_fixed_flow('群', [message(sender='乙', wxid='wx_b')], 2)
    b._send_text.assert_called_with('群', '回复过啦，请看上面的消息')
    assert b.fixed_flow.reply.call_count == 1
