import pytest
from src.bot.fixed_flow import FixedFlow
from src.models.base import ChatMessage, SenderType


def msg(text):
    return ChatMessage(text=text, sender='A', sender_type=SenderType.OTHER, chat_name='群')


def test_route(monkeypatch):
    monkeypatch.setenv('BOT_AT_KEYWORD', '囡囡')
    monkeypatch.setenv('FIXED_FLOW_KEYWORDS', '["宝藏", "截图"]')
    flow = FixedFlow.from_env()
    assert flow.match(msg('@囡囡 帮看宝藏')) == '宝藏'
    assert flow.match(msg('@囡囡 截图')) == '截图'
    assert flow.match(msg('宝藏')) is None
    assert flow.match(msg('@别人 宝藏')) is None
    assert flow.match(msg('@囡囡 聊天')) is None
    assert flow.match(msg('@囡囡宝藏 哈哈')) is None
    assert flow.match(msg('@囡囡 麻烦帮我查看一下宝藏攻略，谢谢')) == '宝藏'
    assert flow.match(msg('@囡囡 截图和宝藏')) == '宝藏'



def test_disabled(monkeypatch):
    monkeypatch.delenv('FIXED_FLOW_KEYWORDS', raising=False)
    assert FixedFlow.from_env().match(msg('@囡囡 宝藏')) is None


@pytest.mark.parametrize('value', ['oops', '{}', '[1]', '[""]', '[" "]'])
def test_invalid(monkeypatch, value):
    monkeypatch.setenv('FIXED_FLOW_KEYWORDS', value)
    with pytest.raises(ValueError, match='FIXED_FLOW_KEYWORDS'):
        FixedFlow.from_env()


def test_reply_links(monkeypatch):
    from unittest.mock import Mock
    from src.bot.archero_hub import ArcheroHub, HubError
    from src.bot.archero_wiki import ArcheroWiki
    monkeypatch.setattr(ArcheroWiki, 'lookup', Mock(return_value=[]))
    lookup = Mock(return_value=[{'title': '钓鱼攻略', 'url': 'https://archero2hub.com/zh/event/fishing/guide'}])
    monkeypatch.setattr(ArcheroHub, 'lookup', lookup)
    reply = FixedFlow(('钓鱼',)).reply(msg('@囡囡 钓鱼怎么玩'), '钓鱼')
    assert '钓鱼攻略' in reply and 'https://archero2hub.com/zh/event/fishing/guide' in reply
    assert '日文 Wiki' not in reply
    lookup.assert_called_once_with('钓鱼')
    lookup.return_value = []
    assert '未找到' in FixedFlow().reply(msg('x'), 'x')
    lookup.side_effect = HubError('failed')
    assert '暂时读取失败' in FixedFlow().reply(msg('x'), 'x')


def test_japanese_source_failure_preserves_chinese(monkeypatch):
    from unittest.mock import Mock
    from src.bot.archero_hub import ArcheroHub
    from src.bot.archero_wiki import ArcheroWiki, WikiError
    monkeypatch.setattr(ArcheroHub, 'lookup', Mock(return_value=[{'title': '钓鱼攻略', 'url': 'https://archero2hub.com/zh/event/fishing/guide'}]))
    monkeypatch.setattr(ArcheroWiki, 'lookup', Mock(side_effect=WikiError('translation failed')))
    reply = FixedFlow().reply(msg('@囡囡 钓鱼'), '钓鱼')
    assert '钓鱼攻略' in reply
    assert '暂时读取失败' in reply
