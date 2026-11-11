"""定时推送：配置解析、到点判断、每天只发一次、失败重试、打开失败不发送。"""

import json
from datetime import datetime
from unittest.mock import Mock

from src.action.chat_opener import ChatOpener
from src.bot.scheduled_push import ScheduledPusher, due_schedules, load_schedules, load_voice_plans, realize_voice_line
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


def test_sends_one_random_message_once_per_day(tmp_path):
    send = Mock(return_value=ActionResult(success=True))
    pusher = _pusher(tmp_path, [{"time": "08:00", "messages": ["a", "b"]}], send=send, groups=["群1", "群2"])
    pusher.run_due(datetime(2026, 11, 3, 8, 0))
    pusher.run_due(datetime(2026, 11, 3, 8, 5))
    calls = [c.args for c in send.call_args_list]
    assert [g for g, _ in calls] == ["群1", "群2"]
    assert all(t in ("a", "b") for _, t in calls)

    # 重启后读取发送记录，不重复发送；第二天照常发送
    restarted = _pusher(tmp_path, [{"time": "08:00", "messages": ["a", "b"]}], send=send, groups=["群1", "群2"])
    restarted.run_due(datetime(2026, 11, 3, 8, 6))
    assert send.call_count == 2
    restarted.run_due(datetime(2026, 11, 4, 8, 0))
    assert send.call_count == 4


def test_random_pick_covers_all_messages(tmp_path):
    send = Mock(return_value=ActionResult(success=True))
    pusher = _pusher(tmp_path, [{"time": "08:00", "messages": ["a", "b", "c"]}], send=send)
    for day in range(1, 31):
        pusher.run_due(datetime(2026, 12, day, 8, 0))
    assert send.call_count == 30
    assert {c.args[1] for c in send.call_args_list} == {"a", "b", "c"}


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
    assert len(texts) == 1
    assert texts[0].split("：", 1)[1] in ("a", "b")
    assert texts[0].split("：", 1)[0] in ("P1", "P2")


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
    assert send.call_count == 3  # 首次 + 重试 2 次
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


# ---- 语音 ----

def _voice_pusher(tmp_path, cfg, open_chat=None, send=None, send_voice=None, groups=("群",)):
    path = tmp_path / "scheduled_messages.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return ScheduledPusher(list(groups), open_chat or Mock(return_value=True),
                           send or Mock(return_value=ActionResult(success=True)),
                           config_path=path, state_path=tmp_path / "state.json", sleep=Mock(),
                           send_voice=send_voice or Mock(return_value=ActionResult(success=True)))


def test_voice_lines_parsed_and_invalid_skipped(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"voice_probability": 0.3, "schedules": [
        {"time": "08:00", "messages": ["a"], "voice_messages": [
            "纯文字", {"text": "开心", "mood": "开心", "rate": 1.1, "volume": 60, "style": "上扬"},
            {"text": "多风格", "style": ["a", "b"]}, {"text": "空风格", "style": []},
            {"text": "坏心情", "mood": "暴躁"}, {"text": "太快", "rate": 3}, {"mood": "开心"}]},
        {"time": "09:00", "messages": ["b"], "voice_messages": ["x"], "voice_probability": 0},
        {"time": "10:00", "messages": ["c"]},
        {"time": "11:00", "weekdays": [5], "messages": ["d"], "voice_messages": ["y"], "voice_probability": 1},
    ]}, ensure_ascii=False), encoding="utf-8")
    plans = load_voice_plans(path)
    assert plans == {
        ("08:00", frozenset()): (0.3, [{"text": "纯文字", "mood": "平静"},
                                       {"text": "开心", "mood": "开心", "rate": 1.1, "volume": 60, "style": ["上扬"]},
                                       {"text": "多风格", "mood": "平静", "style": ["a", "b"]}]),
        ("11:00", frozenset({5})): (1.0, [{"text": "y", "mood": "平静"}]),
    }


def test_voice_sent_after_text_when_probability_hits(tmp_path):
    order = []
    send = Mock(side_effect=lambda g, t: order.append("text") or ActionResult(success=True))
    send_voice = Mock(side_effect=lambda line: order.append(line["text"]) or ActionResult(success=True))
    _voice_pusher(tmp_path, {"schedules": [{"time": "08:00", "messages": ["a"], "voice_probability": 1,
                                            "voice_messages": [{"text": "语音", "mood": "开心"}]}]},
                  send=send, send_voice=send_voice).run_due(datetime(2026, 11, 3, 8, 0))
    assert order == ["text", "语音"]
    sent = send_voice.call_args.args[0]
    assert sent["text"] == "语音" and sent["mood"] == "开心" and 0 <= sent["seed"] <= 65535
    assert "rate" not in sent  # 未配 voice_rate_jitter 时不浮动


def test_no_voice_by_default_probability(tmp_path):
    send_voice = Mock()
    _voice_pusher(tmp_path, {"schedules": [{"time": "08:00", "messages": ["a"], "voice_messages": ["v"]}]},
                  send_voice=send_voice).run_due(datetime(2026, 11, 3, 8, 0))
    send_voice.assert_not_called()


def test_no_voice_when_text_failed_or_reopen_failed(tmp_path):
    cfg = {"voice_probability": 1, "schedules": [{"time": "08:00", "messages": ["a"], "voice_messages": ["v"]}]}
    send_voice = Mock()
    _voice_pusher(tmp_path, cfg, send=Mock(return_value=ActionResult(success=False, error="x")),
                  send_voice=send_voice).run_due(datetime(2026, 11, 3, 8, 0))
    send_voice.assert_not_called()

    (tmp_path / "state.json").unlink()
    _voice_pusher(tmp_path, cfg, open_chat=Mock(side_effect=[True, False]),
                  send_voice=send_voice).run_due(datetime(2026, 11, 3, 8, 0))
    send_voice.assert_not_called()  # 发语音前重新校验群失败


def test_voice_failure_or_exception_does_not_break(tmp_path):
    cfg = {"voice_probability": 1, "schedules": [{"time": "08:00", "messages": ["a"], "voice_messages": ["v"]}]}
    send = Mock(return_value=ActionResult(success=True))
    _voice_pusher(tmp_path, cfg, send=send, send_voice=Mock(side_effect=RuntimeError("boom")),
                  groups=["群1", "群2"]).run_due(datetime(2026, 11, 3, 8, 0))
    assert [c.args[0] for c in send.call_args_list] == ["群1", "群2"]


def test_realize_voice_line_randomness():
    import random as _r
    rng = _r.Random(1)
    line = {"text": "t", "mood": "开心", "rate": 1.1, "style": ["a", "b", "c"]}
    outs = [realize_voice_line(line, 0.05, rng) for _ in range(200)]
    assert all(1.05 <= o["rate"] <= 1.15 for o in outs)
    assert {o["style"] for o in outs} == {"a", "b", "c"}
    assert len({o["seed"] for o in outs}) > 150
    assert line["style"] == ["a", "b", "c"]  # 不改原配置
    # 未配语速时以 1.0 为基准，且不超出 0.5～2.0
    assert all(0.95 <= realize_voice_line({"text": "t", "mood": "平静"}, 0.05, rng)["rate"] <= 1.05 for _ in range(50))
    assert realize_voice_line({"text": "t", "mood": "平静", "rate": 2.0}, 0.5, _r.Random(0))["rate"] <= 2.0


def test_rate_jitter_applied_when_configured(tmp_path):
    send_voice = Mock(return_value=ActionResult(success=True))
    _voice_pusher(tmp_path, {"voice_probability": 1, "voice_rate_jitter": 0.05, "schedules": [
        {"time": "08:00", "messages": ["a"], "voice_messages": [{"text": "v", "rate": 1.2, "style": ["x", "y"]}]}]},
        send_voice=send_voice).run_due(datetime(2026, 11, 3, 8, 0))
    sent = send_voice.call_args.args[0]
    assert 1.15 <= sent["rate"] <= 1.25 and sent["style"] in ("x", "y")


def test_mood_styles_fill_lines_without_style(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"voice_probability": 1, "voice_styles": {
        "开心": ["元气", "笑意"], "暴躁": ["x"], "撒娇": []},
        "schedules": [{"time": "08:00", "messages": ["a"], "voice_messages": [
            {"text": "1", "mood": "开心"}, {"text": "2", "mood": "开心", "style": "自带"},
            {"text": "3", "mood": "撒娇"}, {"text": "4"}]}]}, ensure_ascii=False), encoding="utf-8")
    _, lines = load_voice_plans(path)[("08:00", frozenset())]
    assert lines[0]["style"] == ["元气", "笑意"]      # 用心情候选
    assert lines[1]["style"] == ["自带"]              # 句子自己的优先
    assert "style" not in lines[2] and "style" not in lines[3]  # 空列表被跳过 / 没配候选


# ---- 时间浮动 / 按条 @所有人 / 等聊天停一下 ----

def _cfg_pusher(tmp_path, cfg, send=None, last_activity=None, groups=("群",)):
    path = tmp_path / "scheduled_messages.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return ScheduledPusher(list(groups), Mock(return_value=True),
                           send or Mock(return_value=ActionResult(success=True)),
                           config_path=path, state_path=tmp_path / "state.json", sleep=Mock(),
                           last_activity=last_activity)


def _sweep(pusher, day, start, end):
    """从 start 到 end 每 15 秒调一次 run_due。"""
    from datetime import timedelta
    now = datetime.combine(day, start)
    stop = datetime.combine(day, end)
    while now <= stop:
        pusher.run_due(now)
        now += timedelta(seconds=15)


def test_jitter_sends_once_within_range_and_spreads_over_days(tmp_path):
    from datetime import date, time as dtime
    cfg = {"schedules": [{"time": "08:00", "jitter_minutes": 20, "messages": ["a"]}]}
    send = Mock(return_value=ActionResult(success=True))
    pusher = _cfg_pusher(tmp_path, cfg, send=send)
    starts = set()
    for d in range(1, 11):
        day = date(2026, 12, d)
        start = pusher.planned_start("08:00", frozenset(), day)
        assert datetime.combine(day, dtime(7, 40)) <= start <= datetime.combine(day, dtime(8, 20))
        starts.add(start.time())
        before = send.call_count
        _sweep(pusher, day, dtime(7, 30), dtime(8, 40))
        assert send.call_count == before + 1
    assert len(starts) > 1


def test_jitter_start_stable_across_restart(tmp_path):
    from datetime import date
    cfg = {"schedules": [{"time": "08:00", "jitter_minutes": 20, "messages": ["a"]}]}
    a = _cfg_pusher(tmp_path, cfg).planned_start("08:00", frozenset(), date(2026, 12, 1))
    b = _cfg_pusher(tmp_path, cfg).planned_start("08:00", frozenset(), date(2026, 12, 1))
    assert a == b


def test_jitter_stays_within_same_day(tmp_path):
    from datetime import date, time as dtime
    cfg = {"schedules": [{"time": "00:05", "jitter_minutes": 30, "messages": ["a"]},
                         {"time": "23:55", "jitter_minutes": 30, "messages": ["b"]}]}
    pusher = _cfg_pusher(tmp_path, cfg)
    for d in range(1, 31):
        day = date(2026, 12, d)
        assert pusher.planned_start("00:05", frozenset(), day).date() == day
        late = pusher.planned_start("23:55", frozenset(), day)
        assert late.date() == day and late.time() <= dtime(23, 49)


def test_no_jitter_keeps_exact_time(tmp_path):
    from datetime import date
    pusher = _cfg_pusher(tmp_path, {"schedules": [{"time": "08:00", "messages": ["a"]}]})
    assert pusher.planned_start("08:00", frozenset(), date(2026, 12, 1)) == datetime(2026, 12, 1, 8, 0)


def test_invalid_jitter_treated_as_zero(tmp_path):
    from datetime import date
    for bad in (-5, 121, "10", 1.5, True):
        cfg = {"schedules": [{"time": "08:00", "jitter_minutes": bad, "messages": ["a"]}]}
        pusher = _cfg_pusher(tmp_path, cfg)
        assert pusher.planned_start("08:00", frozenset(), date(2026, 12, 1)) == datetime(2026, 12, 1, 8, 0)


def test_mention_all_per_schedule_overrides_top_level(tmp_path):
    cfg = {"mention_all": True, "schedules": [
        {"time": "08:00", "messages": ["早"], "mention_all": False},
        {"time": "21:30", "messages": ["周活动"]},
    ]}
    send = Mock(return_value=ActionResult(success=True))
    pusher = _cfg_pusher(tmp_path, cfg, send=send)
    pusher.run_due(datetime(2026, 11, 3, 8, 0))
    pusher.run_due(datetime(2026, 11, 3, 21, 30))
    assert [c.args[1] for c in send.call_args_list] == ["早", "@所有人 周活动"]

    send2 = Mock(return_value=ActionResult(success=True))
    cfg2 = {"schedules": [{"time": "08:00", "messages": ["早"], "mention_all": True}]}
    (tmp_path / "x").mkdir()
    _cfg_pusher(tmp_path / "x", cfg2, send=send2).run_due(datetime(2026, 11, 3, 8, 0))
    send2.assert_called_once_with("群", "@所有人 早")


def test_waits_while_group_is_chatting_then_sends(tmp_path):
    from datetime import date, timedelta, time as dtime
    cfg = {"schedules": [{"time": "22:00", "messages": ["a"]}]}
    send = Mock(return_value=ActionResult(success=True))
    chat_until = datetime(2026, 11, 3, 22, 3)  # 群里一直聊到 22:03
    clock = {"now": None}

    def last_activity(group):
        now = clock["now"]
        return min(now, chat_until).timestamp()

    pusher = _cfg_pusher(tmp_path, cfg, send=send, last_activity=last_activity)
    now = datetime(2026, 11, 3, 22, 0)
    sent_at = None
    while now <= datetime(2026, 11, 3, 22, 10):
        clock["now"] = now
        pusher.run_due(now)
        if send.called and sent_at is None:
            sent_at = now
        now += timedelta(seconds=15)
    assert send.call_count == 1
    assert sent_at == chat_until + timedelta(seconds=120)


def test_sends_before_window_ends_even_if_still_chatting(tmp_path):
    from datetime import timedelta
    cfg = {"schedules": [{"time": "22:00", "messages": ["a"]}]}
    send = Mock(return_value=ActionResult(success=True))
    clock = {"now": None}
    pusher = _cfg_pusher(tmp_path, cfg, send=send, last_activity=lambda g: clock["now"].timestamp())
    now = datetime(2026, 11, 3, 22, 0)
    sent_at = None
    while now <= datetime(2026, 11, 3, 22, 10):
        clock["now"] = now
        pusher.run_due(now)
        if send.called and sent_at is None:
            sent_at = now
        now += timedelta(seconds=15)
    assert send.call_count == 1
    assert sent_at == datetime(2026, 11, 3, 22, 8)


def test_quiet_or_unknown_activity_sends_immediately(tmp_path):
    cfg = {"schedules": [{"time": "22:00", "messages": ["a"]}]}
    for activity in (None, lambda g: None, lambda g: datetime(2026, 11, 3, 21, 50).timestamp()):
        send = Mock(return_value=ActionResult(success=True))
        sub = tmp_path / str(id(activity))
        sub.mkdir()
        _cfg_pusher(sub, cfg, send=send, last_activity=activity).run_due(datetime(2026, 11, 3, 22, 0))
        assert send.call_count == 1
