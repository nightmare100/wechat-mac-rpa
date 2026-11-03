"""SmartPerceptionPipeline._merge_chat_list：API 只返回未读条目，未读数只采信 API。"""

from src.models.base import ChatListItem, Rect
from src.perception.smart_pipeline import SmartPerceptionPipeline


def _local(nickname, y, unread=""):
    return ChatListItem(nickname=nickname, last_message_preview="", unread_count=unread,
                        timestamp="", rect=Rect(x=0, y=y, width=100, height=60))


def _merge(local, api):
    pipeline = object.__new__(SmartPerceptionPipeline)
    return pipeline._merge_chat_list(local, api)


def test_api_unread_applied_to_matching_local_item():
    local = [_local("腾讯新闻", 120), _local("人格测试", 0), _local("『宵楼』囡囡", 60)]
    result = _merge(local, [{"nickname": "『宵桜』囡囡", "unread_count": "3"}])
    assert [i.nickname for i in result] == ["人格测试", "『宵桜』囡囡", "腾讯新闻"]
    assert [i.unread_count for i in result] == ["", "3", ""]
    assert result[1].rect.y == 60


def test_local_unread_is_cleared_when_api_reports_none():
    local = [_local("人格测试", 0), _local("腾讯新闻", 60, unread="140")]
    result = _merge(local, [])
    assert [i.unread_count for i in result] == ["", ""]
    assert local[1].unread_count == "140"  # 不修改本地 layout 对象


def test_unmatched_api_item_is_ignored():
    local = [_local("人格测试", 0)]
    result = _merge(local, [{"nickname": "完全不同的名字", "unread_count": "2"}])
    assert [(i.nickname, i.unread_count) for i in result] == [("人格测试", "")]


def test_no_local_list_returns_empty():
    assert _merge([], [{"nickname": "人格测试", "unread_count": "1"}]) == []
