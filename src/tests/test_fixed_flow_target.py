from unittest.mock import Mock
from src.bot.wechat_bot import WeChatBot
from src.reply.generator import FollowupTracker
from src.models.base import ChatMessage, SenderType


def test_only_active_target_or_quote_of_bot(monkeypatch):
    monkeypatch.setenv('BOT_NAME', '囡囡')
    now = [1000.0]
    tracker = FollowupTracker(now=lambda: now[0], randint=lambda a,b: 2)
    tracker.threads = {'群': {'甲': [2, 1000]}}
    tracker.keyword_targets = {'群': {'甲'}}
    b = WeChatBot.__new__(WeChatBot)
    b.generator = Mock(followup=tracker)
    m = ChatMessage(text='符文怎么选', sender='甲', sender_type=SenderType.OTHER, chat_name='群')
    assert b._fixed_flow_addressed('群', m, [])
    assert not b._fixed_flow_addressed('别群', m, [])
    m.sender = '乙'
    assert not b._fixed_flow_addressed('群', m, [])
    m.quoted_text = '囡囡：之前的回答'
    assert b._fixed_flow_addressed('群', m, [])
    m.quoted_text = '别人：之前的回答'
    assert not b._fixed_flow_addressed('群', m, [])
    m.sender = '甲'
    m.quoted_text = ''
    now[0] += 1801
    assert not b._fixed_flow_addressed('群', m, [])


def test_followup_keyword_takes_fixed_path_and_consumes_round(monkeypatch):
    from src.bot.fixed_flow import FixedFlow
    from src.models.base import ActionResult
    monkeypatch.setenv('BOT_AT_KEYWORD', '囡囡')
    tracker = FollowupTracker(now=lambda: 1000, randint=lambda a,b: 2)
    tracker.threads = {'群': {'甲': [2, 1000]}}
    tracker.keyword_targets = {'群': {'甲'}}
    b = WeChatBot.__new__(WeChatBot)
    b.generator = Mock(followup=tracker)
    b.global_store = Mock(chats={})
    b.fixed_flow = FixedFlow(('符文',))
    monkeypatch.setattr(FixedFlow, 'reply', lambda self, message, keyword: '符文攻略 https://archero2hub.com/zh/rune')
    b._send_text = Mock(return_value=ActionResult(success=True))
    b.logger = Mock()
    b.debug_logger = Mock()
    m = ChatMessage(text='符文怎么选', sender='甲', sender_type=SenderType.OTHER, chat_name='群')
    assert b._handle_fixed_flow('群', [m], 1)
    b.generator.generate.assert_not_called()
    b._send_text.assert_called_once()
    assert tracker.candidates('群') == [('甲', 1)]
    m.sender = '乙'
    assert not b._handle_fixed_flow('群', [m], 2)


def test_chime_target_never_enables_keyword_route(monkeypatch):
    from src.bot.fixed_flow import FixedFlow
    monkeypatch.setenv('BOT_AT_KEYWORD', '囡囡')
    tracker = FollowupTracker(now=lambda: 1000, randint=lambda a,b: 2)
    initial = ChatMessage(text='符文怎么选', sender='甲', sender_type=SenderType.OTHER, chat_name='群')
    tracker.record('群', [initial], ['@甲 我也在研究'], fresh_targets={'甲'})
    b = WeChatBot.__new__(WeChatBot)
    b.generator = Mock(followup=tracker)
    assert tracker.candidates('群') == [('甲', 2)]
    assert not b._fixed_flow_addressed('群', initial, [])
    assert FixedFlow(('符文',)).match(initial, addressed=b._fixed_flow_addressed('群', initial, [])) is None
    # 普通接话继续消耗额度，仍不产生检索资格。
    tracker.record('群', [initial], ['@甲 聊聊'])
    assert not b._fixed_flow_addressed('群', initial, [])
    # 用户重新明确 @Bot 后，才允许后续关键词优先。
    initial.text = '@囡囡 你好'
    tracker.record('群', [initial], ['@甲 你好'])
    initial.text = '符文怎么选'
    assert b._fixed_flow_addressed('群', initial, [])


def test_keyword_permission_expires_and_chime_does_not_restore(monkeypatch):
    monkeypatch.setenv('BOT_AT_KEYWORD', '囡囡')
    now = [1000]
    tracker = FollowupTracker(now=lambda: now[0], randint=lambda a,b: 2)
    m = ChatMessage(text='@囡囡 你好', sender='甲', sender_type=SenderType.OTHER, chat_name='群')
    tracker.record('群', [m], ['@甲 你好'])
    assert tracker.keyword_candidates('群') == [('甲', 2)]
    now[0] += 1801
    assert tracker.keyword_candidates('群') == []
    m.text = '符文'
    tracker.record('群', [m], ['@甲 聊聊'], fresh_targets={'甲'})
    assert tracker.candidates('群') == [('甲', 2)]
    assert tracker.keyword_candidates('群') == []
