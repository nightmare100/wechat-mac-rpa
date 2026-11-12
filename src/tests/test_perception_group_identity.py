"""群标题人数被 API 省略时，不能把群成员覆盖成聊天名称。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.models.base import Rect, SenderType
from src.perception.smart_pipeline import SmartPerceptionPipeline


@pytest.mark.parametrize(
    "api_name,local_name,expected_group,expected_sender",
    [
        ("测试群", "测试群（140）", True, "小明"),
        ("测试群", "测试群 (140)", True, "小明"),
        ("测试群（140）", "", True, "小明"),
        ("测试群", "另一个群（140）", False, "测试群"),
        ("小明", "小明", False, "小明"),
        ("小明", "", False, "小明"),
    ],
)
def test_api_path_preserves_group_senders_without_count(
    api_name, local_name, expected_group, expected_sender
):
    pipeline = object.__new__(SmartPerceptionPipeline)
    layout = SimpleNamespace(chat_name=local_name, chat_list_items=[])
    pipeline._run_local_pipeline = Mock(return_value={"layout": layout})
    pipeline._run_api_pipeline = Mock(return_value={
        "chat_name": api_name,
        "messages": [
            {"sender": "小明", "text": "你好"},
            {"sender": "小红", "text": "大家好"},
            {"sender": "自己", "text": "在的"},
        ],
    })
    pipeline._merge_chat_list = Mock(return_value=[])
    pipeline._build_debug_info = Mock(return_value={})

    result = pipeline._run_with_api("unused.png", Rect(0, 0, 1760, 1280), 1.0)

    assert result.messages[0].sender == expected_sender
    assert result.is_group is expected_group
    assert result.chat_name == api_name
    assert result.messages[1].sender == ("小红" if expected_group else api_name)
    assert result.messages[2].sender == "自己"
    assert result.messages[2].sender_type == SenderType.SELF


def test_api_group_identity_survives_local_ocr_failure():
    pipeline = object.__new__(SmartPerceptionPipeline)
    pipeline._run_local_pipeline = Mock(side_effect=RuntimeError("OCR unavailable"))
    pipeline._run_api_pipeline = Mock(return_value={
        "chat_name": "测试群（140）",
        "messages": [{"sender": "小明", "text": "你好"}],
    })
    pipeline._merge_chat_list = Mock(return_value=[])
    pipeline._build_debug_info = Mock(return_value={})

    result = pipeline._run_with_api("unused.png", Rect(0, 0, 1760, 1280), 1.0)

    assert result.is_group is True
    assert result.messages[0].sender == "小明"
