"""Source selection and UI routing without starting the bot."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import run_bot
from src.bot.wechat_bot import WeChatBot
from src.models.base import ChatMessage, SenderType


def test_cli_source_default_and_explicit_modes():
    assert run_bot.parse_args([]).source == "tracememo"
    assert run_bot.parse_args(["--source", "vision"]).source == "vision"
    assert run_bot.parse_args(["--source", "tracememo"]).source == "tracememo"
    with pytest.raises(SystemExit):
        run_bot.parse_args(["--source", "typo"])


def bot_stub(source="tracememo"):
    bot = WeChatBot.__new__(WeChatBot)
    bot.perception = SimpleNamespace(source_name=source, started_at=1000)
    bot.sender = Mock(silent_mode=False)
    bot.global_store = Mock(chats={})
    bot.logger = Mock()
    return bot


def test_failed_target_check_blocks_external_send(monkeypatch):
    bot = bot_stub()
    opener = Mock()
    opener.open.return_value = False
    monkeypatch.setattr("src.bot.wechat_bot.ChatOpener", Mock(return_value=opener))
    result = bot.send_to_chat("群甲", "回复")
    assert not result.success
    bot.sender.send.assert_not_called()


def test_target_opened_before_send_and_exception_blocks(monkeypatch):
    bot = bot_stub()
    opener = Mock()
    monkeypatch.setattr("src.bot.wechat_bot.ChatOpener", Mock(return_value=opener))
    calls = []
    opener.open.side_effect = lambda name: calls.append(("open", name)) or True
    bot.sender.send.side_effect = lambda text, chat_name: calls.append(("send", chat_name)) or SimpleNamespace(success=False)
    bot.send_to_chat("群乙", "回复")
    assert calls == [("open", "群乙"), ("send", "群乙")]
    opener.open.side_effect = RuntimeError("窗口不可用")
    assert not bot._prepare_send_target("群乙")


def test_vision_send_preserves_existing_flow(monkeypatch):
    bot = bot_stub("vision")
    opener = Mock()
    monkeypatch.setattr("src.bot.wechat_bot.ChatOpener", opener)
    bot.sender.send.return_value = SimpleNamespace(success=False)
    bot.send_to_chat("群甲", "回复")
    opener.assert_not_called()
    bot.sender.send.assert_called_once()


def test_discards_preexisting_db_backlog_without_skipping_new_messages():
    bot = bot_stub()
    old = ChatMessage("旧", "甲", SenderType.OTHER, "群甲", create_time=999)
    new = ChatMessage("新", "甲", SenderType.OTHER, "群甲", create_time=1001)
    legacy = ChatMessage("旧 OCR", "甲", SenderType.OTHER, "群甲")
    assert bot._skip_source_startup_backlog("群甲", [old, new, legacy]) == [new]
    assert bot.global_store.mark_replied.call_count == 2


def test_source_factory_selects_only_requested_receiver(monkeypatch):
    tm_factory, vision_factory = Mock(), Mock()
    monkeypatch.setattr("src.perception.tracememo_pipeline.TraceMemoPipeline", tm_factory)
    monkeypatch.setattr("src.perception.smart_pipeline.SmartPerceptionPipeline", vision_factory)
    profile = object()
    assert run_bot.create_perception("tracememo", profile) is tm_factory.return_value
    vision_factory.assert_not_called()
    assert run_bot.create_perception("vision", profile) is vision_factory.return_value
