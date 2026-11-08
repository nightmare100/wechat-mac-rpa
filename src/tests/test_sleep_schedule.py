"""休眠时段：解析、跨午夜判断、主循环进出休眠与醒来后积压消息不回复。"""

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.utils.time_window import in_sleep_window, parse_sleep_hours
from src.bot.wechat_bot import WAKE_BACKLOG_SECONDS, WeChatBot


def _at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 8, h, m)


class TestParse:
    def test_empty_disables(self):
        assert parse_sleep_hours("") is None
        assert parse_sleep_hours("  ") is None

    @pytest.mark.parametrize("value", ["23:30", "23:30-", "25:00-07:00", "07:00-07:00", "a-b"])
    def test_invalid_raises(self, value):
        with pytest.raises(ValueError):
            parse_sleep_hours(value)


class TestWindow:
    window = parse_sleep_hours("23:30-07:30")

    @pytest.mark.parametrize("hhmm", ["23:30", "23:59", "00:00", "03:00", "07:29"])
    def test_inside_across_midnight(self, hhmm):
        assert in_sleep_window(self.window, _at(hhmm))

    @pytest.mark.parametrize("hhmm", ["07:30", "12:00", "23:00", "23:29"])
    def test_outside(self, hhmm):
        assert not in_sleep_window(self.window, _at(hhmm))

    def test_same_day_window(self):
        window = parse_sleep_hours("01:00-06:00")
        assert in_sleep_window(window, _at("03:00"))
        assert not in_sleep_window(window, _at("23:45"))

    def test_none_never_sleeps(self):
        assert not in_sleep_window(None, _at("03:00"))


def _fake_bot():
    return SimpleNamespace(
        sleep_window=parse_sleep_hours("23:30-07:30"),
        logger=Mock(),
        global_store=Mock(),
        _asleep=False,
        _woke_at=None,
        _backlog_skipped_chats=set(),
    )


class TestBotSleep:
    def test_enter_and_wake(self):
        bot = _fake_bot()
        assert WeChatBot._check_sleep(bot, _at("23:00")) is False
        assert WeChatBot._check_sleep(bot, _at("23:30")) is True
        assert WeChatBot._check_sleep(bot, _at("03:00")) is True
        assert bot._woke_at is None
        assert WeChatBot._check_sleep(bot, _at("07:30")) is False
        assert bot._woke_at is not None

    def test_backlog_skipped_once_per_chat_after_wake(self):
        bot = _fake_bot()
        WeChatBot._check_sleep(bot, _at("03:00"))
        WeChatBot._check_sleep(bot, _at("07:30"))
        night = [Mock(), Mock()]
        assert WeChatBot._skip_wake_backlog(bot, "人格测试", night) == []
        assert bot.global_store.mark_replied.call_count == 2
        later = [Mock()]
        assert WeChatBot._skip_wake_backlog(bot, "人格测试", later) == later

    def test_no_skip_without_sleep(self):
        bot = _fake_bot()
        msgs = [Mock()]
        assert WeChatBot._skip_wake_backlog(bot, "人格测试", msgs) == msgs

    def test_no_skip_after_backlog_window(self):
        bot = _fake_bot()
        bot._woke_at = time.monotonic() - WAKE_BACKLOG_SECONDS - 1
        msgs = [Mock()]
        assert WeChatBot._skip_wake_backlog(bot, "人格测试", msgs) == msgs
