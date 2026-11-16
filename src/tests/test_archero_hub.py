from unittest.mock import Mock
import pytest
from src.bot.archero_hub import ArcheroHub, HubError

HOME = '<nav><a href="/zh/event/fishing/guide"><span class="truncate">钓鱼攻略</span><p>玩法介绍</p></a><a href="/zh/event/fishing/shop">钓鱼商店</a><a href="https://evil.test/zh/a">钓鱼</a><a href="/zh/event/fishing/shop">钓鱼商店</a></nav>'


def test_titles_links_only():
    hub = ArcheroHub()
    hub._get = Mock(return_value=HOME)
    assert hub.lookup('钓鱼') == [
        {'title': '钓鱼攻略', 'url': 'https://archero2hub.com/zh/event/fishing/guide'},
        {'title': '钓鱼商店', 'url': 'https://archero2hub.com/zh/event/fishing/shop'}]
    hub._get.assert_called_once_with('https://archero2hub.com/zh')


def test_no_match():
    hub = ArcheroHub()
    hub._get = Mock(return_value=HOME)
    assert hub.lookup('不存在') == []


def test_invalid_index():
    hub = ArcheroHub()
    hub._get = Mock(return_value='<html>challenge</html>')
    with pytest.raises(HubError):
        hub.lookup('钓鱼')


def test_excludes_description_only_matches_and_orders_titles():
    hub = ArcheroHub()
    hub._get = Mock(return_value='''<nav>
      <a href="/zh/guide/diamond"><span class="truncate">钻石使用指南</span><div>符文遗迹购买建议</div></a>
      <a href="/zh/guide/twin"><span class="truncate">双生符文指南</span></a>
      <a href="/zh/rune/tier"><span class="truncate">符文等级列表</span></a>
      <a href="/zh/rune"><span class="truncate">符文</span></a>
    </nav>''')
    assert [p['title'] for p in hub.lookup('符文')] == ['符文', '符文等级列表', '双生符文指南']


def test_blank_keyword_does_not_fetch():
    hub = ArcheroHub()
    hub._get = Mock()
    assert hub.lookup('  ') == []
    hub._get.assert_not_called()
