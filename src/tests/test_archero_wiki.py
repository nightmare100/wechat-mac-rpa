from unittest.mock import Mock
import pytest
from src.bot.archero_wiki import ArcheroWiki, WikiError


def test_filters_first_game_before_translation():
    translate = Mock(side_effect=['釣り', '钓鱼活动'])
    wiki = ArcheroWiki(translate=translate)
    wiki._search = Mock(return_value=[
        {'path': 'ステージ/イベント/釣りフェスタ', 'title': '釣りフェスタ', 'locale': 'ja'},
        {'path': 'アーチャー伝説2/釣り', 'title': '釣り', 'locale': 'ja'},
        {'path': 'アーチャー伝説20/釣り', 'title': '釣り', 'locale': 'ja'},
        {'path': 'アーチャー伝説2/../装備', 'title': '装備', 'locale': 'ja'},
    ])
    pages = wiki.lookup('钓鱼')
    assert len(pages) == 1
    assert pages[0]['title'] == '钓鱼活动'
    assert pages[0]['url'].startswith('https://archero.wiki/ja/')
    assert translate.call_count == 2
    wiki._search.assert_called_once_with('釣り')


def test_empty_second_game_results():
    wiki = ArcheroWiki(translate=Mock(return_value='釣り'))
    wiki._search = Mock(return_value=[{'path': '装備', 'title': '装備', 'locale': 'ja'}])
    assert wiki.lookup('钓鱼') == []


def test_translation_failure():
    wiki = ArcheroWiki(translate=Mock(return_value=''))
    with pytest.raises(WikiError):
        wiki.lookup('钓鱼')


def test_rejects_home_and_unrelated_second_game_pages():
    translate = Mock(side_effect=['ルーン', '符文指南'])
    wiki = ArcheroWiki(translate=translate)
    wiki._search = Mock(return_value=[
        {'path': 'アーチャー伝説2', 'title': 'アーチャー伝説2', 'locale': 'ja'},
        {'path': 'アーチャー伝説2/魔王召喚', 'title': '魔王召喚', 'locale': 'ja'},
        {'path': 'アーチャー伝説2/ルーン', 'title': 'ルーンガイド', 'locale': 'ja'},
    ])
    pages = wiki.lookup('符文')
    assert len(pages) == 1
    assert pages[0]['title'] == '符文指南'
    assert translate.call_count == 2


def test_home_only_is_no_result():
    translate = Mock(return_value='ルーン')
    wiki = ArcheroWiki(translate=translate)
    wiki._search = Mock(return_value=[
        {'path': 'アーチャー伝説2', 'title': 'アーチャー伝説2', 'locale': 'ja'}])
    assert wiki.lookup('符文') == []
    translate.assert_called_once_with('符文', '日语')
