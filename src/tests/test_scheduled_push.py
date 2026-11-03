"""定时推送：配置解析、到点判断、每天只发一次、失败重试、打开失败不发送。"""

import json
from datetime import datetime
from unittest.mock import Mock

from src.action.chat_opener import ChatOpener
from src.bot.scheduled_push import ScheduledPusher, due_schedules, load_schedules
from src.models.base import ActionResult


def _config(tmp_path, schedules):
    path = tmp_path / "scheduled_messages.json"
    path.write_text(json.dumps({"_示例": [{"time": "01:00", "messages": ["x"]}], "schedules": schedules},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _pusher(tmp_path, schedules, open_chat=None, send=None, groups=("人格测试",)):
    return ScheduledPusher(
        groups=list(groups),
        open_chat=open_chat or Mock(return_value=True),
        send=send or Mock(return_value=ActionResult(success=True)),
        config_path=_config(tmp_path, schedules),
        state_path=tmp_path / "state.json",
        sleep=Mock(),
    )


def test_load_schedules_skips_invalid_and_example(tmp_path):
    path = _config(tmp_path, [
        {"time": "08:00", "messages": ["早", "  "]},
        {"time": "25:00", "messages": ["错时间"]},
        {"time": "09:00", "messages": []},
        {"time": "10:00", "messages": ["周末"], "weekdays": [6, 7]},
        {"time": "11:00", "messages": ["坏星期"], "weekdays": [0, 8]},
        {"time": "12:00", "messages": ["空=每天"], "weekdays": []},
    ])
    assert load_schedules(path) == [
        ("08:00", ["早"], frozenset()),
        ("10:00", ["周末"], frozenset({6, 7})),
        ("12:00", ["空=每天"], frozenset()),
    ]
    assert load_schedules(tmp_path / "missing.json") == []


def test_due_window():
    schedules = [("08:00", ["早"], frozenset())]
    assert due_schedules(schedules, datetime(2026, 11, 3, 7, 59)) == []
    assert due_schedules(schedules, datetime(2026, 11, 3, 8, 0)) == schedules
    assert due_schedules(schedules, datetime(2026, 11, 3, 8, 10)) == schedules
    assert due_schedules(schedules, datetime(2026, 11, 3, 8, 11)) == []


def test_due_only_on_configured_weekdays():
    schedules = [("08:00", ["周一三五"], frozenset({1, 3, 5}))]
    assert due_schedules(schedules, datetime(2026, 11, 2, 8, 0)) == schedules  # 周一
    assert due_schedules(schedules, datetime(2026, 11, 3, 8, 0)) == []  # 周二
    assert due_schedules(schedules, datetime(2026, 11, 8, 8, 0)) == []  # 周日


def test_sends_all_messages_in_order_once_per_day(tmp_path):
    send = Mock(return_value=ActionResult(success=True))
    pusher = _pusher(tmp_path, [{"time": "08:00", "messages": ["a", "b"]}], send=send, groups=["群1", "群2"])
    pusher.run_due(datetime(2026, 11, 3, 8, 0))
    pusher.run_due(datetime(2026, 11, 3, 8, 5))
    assert [c.args for c in send.call_args_list] == [("群1", "a"), ("群1", "b"), ("群2", "a"), ("群2", "b")]

    # 重启后读取发送记录，不重复发送；第二天照常发送
    restarted = _pusher(tmp_path, [{"time": "08:00", "messages": ["a", "b"]}], send=send, groups=["群1", "群2"])
    restarted.run_due(datetime(2026, 11, 3, 8, 6))
    assert send.call_count == 4
    restarted.run_due(datetime(2026, 11, 4, 8, 0))
    assert send.call_count == 8


def test_prefixes_prepended_to_each_message(tmp_path):
    path = tmp_path / "scheduled_messages.json"
    path.write_text(json.dumps({"prefixes": ["P1", " ", "P2"],
                                "schedules": [{"time": "08:00", "messages": ["a", "b"]}]}), encoding="utf-8")
    send = Mock(return_value=ActionResult(success=True))
    pusher = ScheduledPusher(["群"], Mock(return_value=True), send, config_path=path,
                             state_path=tmp_path / "state.json", sleep=Mock())
    assert pusher.prefixes == ["P1", "P2"]
    pusher.run_due(datetime(2026, 11, 3, 8, 0))
    texts = [c.args[1] for c in send.call_args_list]
    assert [t.split("：", 1)[1] for t in texts] == ["a", "b"]
    assert all(t.split("：", 1)[0] in ("P1", "P2") for t in texts)


def test_mention_all_goes_before_prefix(tmp_path):
    path = tmp_path / "scheduled_messages.json"
    path.write_text(json.dumps({"mention_all": True, "prefixes": ["P"],
                                "schedules": [{"time": "08:00", "messages": ["a"]}]}), encoding="utf-8")
    send = Mock(return_value=ActionResult(success=True))
    ScheduledPusher(["群"], Mock(return_value=True), send, config_path=path,
                    state_path=tmp_path / "state.json", sleep=Mock()).run_due(datetime(2026, 11, 3, 8, 0))
    send.assert_called_once_with("群", "@所有人 P：a")


def test_send_failure_retries_twice_then_gives_up(tmp_path):
    send = Mock(return_value=ActionResult(success=False, error="x"))
    pusher = _pusher(tmp_path, [{"time": "08:00", "messages": ["a", "b"]}], send=send)
    pusher.run_due(datetime(2026, 11, 3, 8, 0))
    assert send.call_count == 3  # 首次 + 重试 2 次，"b" 不再发送
    pusher.run_due(datetime(2026, 11, 3, 8, 1))
    assert send.call_count == 3  # 当天已放弃，不再尝试


def test_open_chat_failure_never_sends(tmp_path):
    send = Mock()
    pusher = _pusher(tmp_path, [{"time": "08:00", "messages": ["a"]}], open_chat=Mock(return_value=False), send=send)
    pusher.run_due(datetime(2026, 11, 3, 8, 0))
    send.assert_not_called()


def _opener(titles_sequence):
    perception = Mock()
    perception.layout.parse.side_effect = [
        Mock(chat_name=t, title_elements=[]) for t in titles_sequence
    ]
    automation = Mock()
    automation.get_clipboard_text.return_value = (True, "原剪贴板")
    return ChatOpener(perception, automation=automation), automation


def test_opener_skips_search_when_already_in_chat():
    opener, automation = _opener(["人格测试（3）"])
    assert opener.open("人格测试") is True
    automation.send_keys.assert_not_called()


def test_opener_searches_and_verifies_title(monkeypatch):
    monkeypatch.setattr("src.action.chat_opener.time.sleep", lambda s: None)
    opener, automation = _opener(["其他群", "人格测试（3）"])
    assert opener.open("人格测试") is True
    automation.set_clipboard_text.assert_any_call("人格测试")
    automation.set_clipboard_text.assert_called_with("原剪贴板")


def test_opener_fails_when_title_mismatch(monkeypatch):
    monkeypatch.setattr("src.action.chat_opener.time.sleep", lambda s: None)
    opener, automation = _opener(["其他群", "人格测试二群"])
    assert opener.open("人格测试") is False
    automation.send_keys.assert_called_with("key code 53")
