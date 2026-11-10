#!/usr/bin/env python3
"""L5 Bot Orchestrator 单元测试"""

from unittest.mock import Mock

import pytest

from src.action.login_recovery import LoginRecoveryResult, LoginRecoveryStatus
from src.bot.wechat_bot import WeChatBot
from src.capture.window_capture import WeChatNotReadyError
from src.layout.profile import PROFILE_WECHAT_MAC_1760X1280
from src.models.base import ActionResult, ChatListItem, ChatMessage, PerceptionResult, Rect, SenderType


class TestWeChatBot:
    @pytest.fixture
    def bot(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("scripts.sync_knowledge.sync", lambda: False)
        monkeypatch.setattr("src.bot.wechat_bot.GlobalStore", Mock)
        monkeypatch.setattr("src.bot.wechat_bot.MemoryEngine", Mock)
        db = Mock()
        db._get_conn.return_value = Mock()
        monkeypatch.setattr("src.badcase.case_db.get_db", lambda: db)
        bot = WeChatBot(PROFILE_WECHAT_MAC_1760X1280, llm_client=Mock(), perception=Mock())
        bot.global_store.chats = {}
        bot.memory_engine.get_user_memory.return_value = ""
        bot.debug_logger = Mock(current=None)
        return bot

    def test_tick_no_new_messages(self, bot, tmp_path):
        """没有新消息时不发送回复"""
        mock_result = PerceptionResult(
            chat_name="测试群",
            messages=[ChatMessage(text="旧消息", sender="A", sender_type=SenderType.OTHER, chat_name="测试群")],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "1.png")
        )
        bot.perception = Mock()
        bot.perception.perceive.return_value = mock_result
        bot.policy = Mock()
        bot.policy.should_reply.return_value = False

        # mock global_store 返回空未回复列表
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (Mock(), [])

        bot.tick()

        bot.perception.perceive.assert_called_once()
        bot.global_store.merge_tick.assert_called_once()
        bot.global_store.mark_replied.assert_not_called()

    def test_tick_replies_to_new_message(self, bot, tmp_path):
        """有新消息且 policy 允许时发送回复"""
        msg = ChatMessage(text="在吗", sender="A", sender_type=SenderType.OTHER, chat_name="测试群")
        mock_result = PerceptionResult(
            chat_name="测试群",
            messages=[msg],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "2.png")
        )
        bot.perception = Mock()
        bot.perception.perceive.return_value = mock_result

        bot.policy = Mock()
        bot.policy.should_reply.return_value = True

        bot.generator = Mock()
        bot.generator.generate.return_value = ["在的"]

        bot.sender = Mock()
        bot.sender.send.return_value = ActionResult(success=True, sent_text="在的")

        mock_state = Mock()
        mock_state.pending_self_messages = []
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (mock_state, [msg])
        bot.global_store.chats = {"测试群": mock_state}

        bot.tick()

        bot.generator.generate.assert_called_once()
        bot.sender.send.assert_called_once_with("在的", chat_name="测试群")
        bot.global_store.mark_replied.assert_called_once()

    def test_tick_perception_none(self, bot):
        """perceive 返回 None 时直接跳过"""
        bot.perception = Mock()
        bot.perception.perceive.return_value = None
        bot.logger = Mock()

        bot.tick()

        bot.perception.perceive.assert_called_once()
        warning = bot.logger.warning.call_args.args[0]
        assert "实际启动 Bot 的应用" in warning
        assert "屏幕录制权限" in warning
        assert "自动化开发环境" in warning

    def test_tick_recovers_when_perception_reports_not_ready(self, bot, tmp_path):
        result = PerceptionResult(
            chat_name="测试群",
            messages=[],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "recovered.png"),
        )
        bot.perception = Mock()
        bot.perception.perceive.side_effect = [WeChatNotReadyError("需要登录"), result]
        bot._login_handler = Mock()
        bot._login_handler.handle.return_value = LoginRecoveryResult(
            LoginRecoveryStatus.SUCCESS, "已恢复"
        )
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (Mock(), [])

        bot.tick()

        assert bot.perception.perceive.call_count == 2
        bot._login_handler.handle.assert_called_once()

    def test_chat_switch_can_be_disabled(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("scripts.sync_knowledge.sync", lambda: False)
        monkeypatch.setattr("src.bot.wechat_bot.GlobalStore", Mock)
        monkeypatch.setattr("src.bot.wechat_bot.MemoryEngine", Mock)
        disabled_bot = WeChatBot(
            PROFILE_WECHAT_MAC_1760X1280,
            llm_client=Mock(),
            perception=Mock(),
            enable_chat_switch=False,
        )

        assert disabled_bot.enable_chat_switch is False
        assert disabled_bot._try_switch_to_unread_chat(Mock()) == ""

    def test_tick_policy_declines(self, bot, tmp_path):
        """policy 返回 False 时不生成回复"""
        msg = ChatMessage(text="在吗", sender="A", sender_type=SenderType.OTHER, chat_name="测试群")
        mock_result = PerceptionResult(
            chat_name="测试群",
            messages=[msg],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "3.png")
        )
        bot.perception = Mock()
        bot.perception.perceive.return_value = mock_result

        bot.policy = Mock()
        bot.policy.should_reply.return_value = False

        bot.generator = Mock()
        bot.sender = Mock()

        mock_state = Mock()
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (mock_state, [msg])

        bot.tick()

        bot.generator.generate.assert_not_called()
        bot.sender.send.assert_not_called()

    def test_on_message_callback(self, bot, tmp_path):
        """on_message 回调被正确触发"""
        callback = Mock()
        bot.on_message = callback

        msg = ChatMessage(text="hi", sender="A", sender_type=SenderType.OTHER, chat_name="测试群")
        mock_result = PerceptionResult(
            chat_name="测试群",
            messages=[msg],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "4.png")
        )
        bot.perception = Mock()
        bot.perception.perceive.return_value = mock_result

        bot.policy = Mock()
        bot.policy.should_reply.return_value = False
        mock_state = Mock()
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (mock_state, [msg])

        bot.tick()

        callback.assert_called_once_with(msg, mock_state)

    def test_send_to_chat(self, bot):
        """send_to_chat 主动发送接口"""
        bot.sender = Mock()
        bot.sender.send.return_value = ActionResult(success=True, sent_text="hello")

        result = bot.send_to_chat("测试群", "hello")

        assert result.success is True
        bot.sender.send.assert_called_once_with("hello", chat_name="测试群")

    def test_run_auto_keeps_fixed_interval(self, monkeypatch):
        """tick 耗时应从轮询间隔中扣除，避免周期持续漂移。"""
        bot = WeChatBot.__new__(WeChatBot)
        bot._tick_id = 1
        bot.logger = Mock()
        bot.tick = Mock(side_effect=lambda: setattr(bot, "running", False))
        monotonic = Mock(side_effect=[10.0, 12.0])
        sleep = Mock()
        monkeypatch.setattr("src.bot.wechat_bot.time.monotonic", monotonic)
        monkeypatch.setattr("src.bot.wechat_bot.time.sleep", sleep)

        bot.run_auto(interval=5.0)

        sleep.assert_called_once_with(3.0)

    def test_no_reply_chat_skips_llm_and_marks_processed(self, bot, tmp_path):
        msg = ChatMessage(text="推送", sender="公众号", sender_type=SenderType.OTHER, chat_name="公众号")
        bot.perception = Mock()
        bot.perception.perceive.return_value = PerceptionResult(
            chat_name="公众号", messages=[msg], chat_list_items=[], screenshot_path=str(tmp_path / "skip.png")
        )
        bot.policy = Mock()
        bot.policy.should_reply.return_value = True
        bot.generator = Mock()
        bot.sender = Mock(silent_mode=False)
        state = Mock(messages=[msg])
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (state, [msg])

        bot.tick()

        bot.generator.generate.assert_not_called()
        bot.global_store.mark_replied.assert_called_once_with("公众号", msg, "")

    def test_partial_send_failure_does_not_retry_delivered_reply(self, bot, tmp_path, monkeypatch):
        bot._tick_id = 1
        msg = ChatMessage(text="分两条回", sender="A", sender_type=SenderType.OTHER, chat_name="测试群")
        bot.perception = Mock()
        bot.perception.perceive.return_value = PerceptionResult(
            chat_name="测试群", messages=[msg], chat_list_items=[], screenshot_path=str(tmp_path / "partial.png")
        )
        bot.policy = Mock()
        bot.policy.should_reply.return_value = True
        bot.generator = Mock()
        bot.generator.generate.return_value = ["第一条", "第二条"]
        bot.generator.text_for_logging.side_effect = lambda text: text
        bot.generator.messages_for_logging.side_effect = lambda messages: messages
        bot.sender = Mock(silent_mode=False)
        bot.sender.send.side_effect = [
            ActionResult(success=True, sent_text="第一条"),
            ActionResult(success=False, error="发送失败"),
        ]
        state = Mock(messages=[msg], pending_self_messages=[])
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (state, [msg])
        bot.global_store.chats = {"测试群": state}
        bot._update_tick_send_result = Mock()
        monkeypatch.setattr("src.bot.wechat_bot.time.sleep", Mock())

        bot.tick()

        bot.global_store.mark_replied.assert_called_once_with("测试群", msg, "第一条")
        bot._update_tick_send_result.assert_called_once_with(2, ["第一条"], success=False)

    def test_generation_failure_keeps_message_unreplied(self, bot, tmp_path):
        msg = ChatMessage(
            text="在吗",
            sender="A",
            sender_type=SenderType.OTHER,
            chat_name="测试群",
        )
        bot.perception = Mock()
        bot.perception.perceive.return_value = PerceptionResult(
            chat_name="测试群",
            messages=[msg],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "failed.png"),
        )
        bot.policy = Mock()
        bot.policy.should_reply.return_value = True
        bot.generator = Mock(last_generation_failed=True)
        bot.generator.generate.return_value = []
        bot.generator.text_for_logging.side_effect = lambda text: text
        bot.generator.messages_for_logging.side_effect = lambda messages: messages
        bot.sender = Mock(silent_mode=False)
        state = Mock(messages=[msg])
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (state, [msg])
        bot.global_store.chats = {"测试群": state}

        bot.tick()

        bot.global_store.mark_replied.assert_not_called()

    def test_explicit_no_reply_marks_message_replied(self, bot, tmp_path):
        msg = ChatMessage(
            text="不用回",
            sender="A",
            sender_type=SenderType.OTHER,
            chat_name="测试群",
        )
        bot.perception = Mock()
        bot.perception.perceive.return_value = PerceptionResult(
            chat_name="测试群",
            messages=[msg],
            chat_list_items=[],
            screenshot_path=str(tmp_path / "no_reply.png"),
        )
        bot.policy = Mock()
        bot.policy.should_reply.return_value = True
        bot.generator = Mock(last_generation_failed=False)
        bot.generator.generate.return_value = []
        bot.generator.text_for_logging.side_effect = lambda text: text
        bot.generator.messages_for_logging.side_effect = lambda messages: messages
        bot.sender = Mock(silent_mode=False)
        state = Mock(messages=[msg])
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (state, [msg])
        bot.global_store.chats = {"测试群": state}

        bot.tick()

        bot.global_store.mark_replied.assert_called_once_with(
            "测试群", msg, "(未回复)"
        )

    def test_chime_in_dropped_logs_cooldown_reason(self, bot, tmp_path):
        msg = ChatMessage(text="100 抽 5 件套", sender="A", sender_type=SenderType.OTHER, chat_name="测试群")
        bot.perception = Mock()
        bot.perception.perceive.return_value = PerceptionResult(
            chat_name="测试群", messages=[msg], chat_list_items=[], screenshot_path=str(tmp_path / "chime.png"),
        )
        bot.policy = Mock()
        bot.policy.should_reply.return_value = True
        bot.generator = Mock(last_generation_failed=False, last_chime_in_dropped=True)
        bot.generator.generate.return_value = []
        bot.generator.text_for_logging.side_effect = lambda text: text
        bot.generator.messages_for_logging.side_effect = lambda messages: messages
        bot.sender = Mock(silent_mode=False)
        state = Mock(messages=[msg])
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (state, [msg])
        bot.global_store.chats = {"测试群": state}
        bot.logger.log_decision = Mock()

        bot.tick()

        reasons = [c.kwargs.get("reason", "") for c in bot.logger.log_decision.call_args_list]
        assert any("主动插话冷却中" in r for r in reasons)
        bot.global_store.mark_replied.assert_called_once_with("测试群", msg, "(未回复)")


# ---- 回复语音 ----
from src.reply.voice_policy import DEFAULTS as VOICE_DEFAULTS  # noqa: E402
from src.reply.voice_policy import VoiceDecision  # noqa: E402

VOICE_YES = VoiceDecision(True, "概率命中", "被夸感谢", "哼，算你有眼光", "调皮", "带点笑意", 1.0)


class TestVoiceReply:
    @pytest.fixture
    def bot(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("scripts.sync_knowledge.sync", lambda: False)
        monkeypatch.setattr("src.bot.wechat_bot.GlobalStore", Mock)
        monkeypatch.setattr("src.bot.wechat_bot.MemoryEngine", Mock)
        monkeypatch.setattr("src.bot.wechat_bot.time.sleep", Mock())
        db = Mock()
        db._get_conn.return_value = Mock()
        monkeypatch.setattr("src.badcase.case_db.get_db", lambda: db)
        self.opener = Mock()
        self.opener.return_value.is_current.return_value = True
        monkeypatch.setattr("src.bot.wechat_bot.ChatOpener", self.opener)
        bot = WeChatBot(PROFILE_WECHAT_MAC_1760X1280, llm_client=Mock(), perception=Mock())
        bot.debug_logger = Mock(current=None)
        bot.memory_engine = None
        bot.policy = Mock()
        bot.policy.should_reply.return_value = True
        bot.generator = Mock(last_voice_meta={"scene": "被夸感谢", "mood": "调皮"})
        bot.generator.generate.return_value = ["@A 哼，算你有眼光"]
        bot.generator.text_for_logging.side_effect = lambda text: text
        bot.generator.messages_for_logging.side_effect = lambda messages: messages
        bot.sender = Mock(silent_mode=False)
        bot.sender.send.return_value = ActionResult(success=True)
        bot._update_tick_send_result = Mock()
        bot.voice_policy = Mock()
        bot.voice_policy.decide.return_value = VOICE_YES
        bot.voice_reply_config = VOICE_DEFAULTS.__class__(**{**VOICE_DEFAULTS.__dict__, "dry_run": False})
        bot._voice_sender = Mock(last_pressed=False)
        bot._voice_sender.send.return_value = ActionResult(success=True)
        return bot

    def _tick(self, bot, tmp_path, chat="测试群", is_group=True):
        msg = ChatMessage(text="囡囡好可爱", sender="A", sender_type=SenderType.OTHER, chat_name=chat)
        bot.perception.perceive.return_value = PerceptionResult(
            chat_name=chat, messages=[msg], chat_list_items=[], screenshot_path=str(tmp_path / "v.png"),
            is_group=is_group,
        )
        state = Mock(messages=[msg], pending_self_messages=[])
        bot.global_store = Mock()
        bot.global_store.merge_tick.return_value = (state, [msg])
        bot.global_store.chats = {chat: state}
        bot.tick()
        return msg, state

    def test_dry_run_logs_and_sends_text(self, bot, tmp_path, caplog):
        bot.voice_reply_config = VOICE_DEFAULTS
        with caplog.at_level("INFO"):
            msg, _ = self._tick(bot, tmp_path)
        bot._voice_sender.send.assert_not_called()
        bot.sender.send.assert_called_once_with("@A 哼，算你有眼光", chat_name="测试群")
        assert "[VoiceReply] 本会发语音" in caplog.text

    def test_voice_replaces_text_and_records_tag(self, bot, tmp_path):
        msg, state = self._tick(bot, tmp_path)
        bot._voice_sender.send.assert_called_once_with("哼，算你有眼光", "调皮", rate=1.0, style="带点笑意")
        bot.sender.send.assert_not_called()
        bot.global_store.mark_replied.assert_called_once_with("测试群", msg, "[语音] 哼，算你有眼光")
        assert state.pending_self_messages[0].text == "[语音] 哼，算你有眼光"
        bot.voice_policy.record_sent.assert_called_once_with("测试群", True)
        bot._update_tick_send_result.assert_called_once()
        assert bot._update_tick_send_result.call_args.kwargs["success"] is True

    def test_voice_failure_falls_back_to_text_once(self, bot, tmp_path):
        bot._voice_sender.send.return_value = ActionResult(success=False, error="合成失败")
        self._tick(bot, tmp_path)
        bot.sender.send.assert_called_once_with("@A 哼，算你有眼光", chat_name="测试群")  # 主动场合兜底保留原回复（含 @）
        bot.voice_policy.record_sent.assert_called_once_with("测试群", False)

    def test_chat_check_failure_falls_back_to_text(self, bot, tmp_path):
        self.opener.return_value.is_current.return_value = False
        self._tick(bot, tmp_path)
        self.opener.return_value.open.assert_not_called()  # 只读校验，绝不切换会话
        bot._voice_sender.send.assert_not_called()
        bot.sender.send.assert_called_once_with("@A 哼，算你有眼光", chat_name="测试群")

    def test_request_fallback_sends_accept_text(self, bot, tmp_path):
        bot.generator.generate.return_value = ["哼，才不给你听"]
        bot.voice_policy.decide.return_value = VoiceDecision(True, "答应", "要听语音", "好啦就一句哦", "不情愿", None, 0.95)
        bot._voice_sender.send.return_value = ActionResult(success=False, error="合成失败")
        self._tick(bot, tmp_path)
        bot.sender.send.assert_called_once_with("好啦就一句哦", chat_name="测试群")

    def test_silent_non_whitelist_never_voices(self, bot, tmp_path):
        bot.sender = Mock(silent_mode=True)
        bot.sender.in_silent_whitelist.return_value = False
        bot.sender.send.return_value = ActionResult(success=False, error="静默", skipped=True)
        self._tick(bot, tmp_path)
        bot._voice_sender.send.assert_not_called()

    def test_failure_after_recording_started_skips_text_fallback(self, bot, tmp_path):
        """已按下右 Option 后失败：微信已发出（半截）语音，不能再补文字。"""
        def send(*args, **kwargs):  # 真实情况：send 过程中按下了右 Option 后播放失败
            bot._voice_sender.last_pressed = True
            return ActionResult(success=False, error="播放失败")
        bot._voice_sender.send.side_effect = send
        msg, _ = self._tick(bot, tmp_path)
        bot.sender.send.assert_not_called()
        bot.global_store.mark_replied.assert_called_once_with("测试群", msg, "[语音] 哼，算你有眼光")
        bot.voice_policy.record_sent.assert_called_once_with("测试群", True)

    def test_stale_pressed_flag_does_not_block_fallback(self, bot, tmp_path):
        """上一次成功发送留下 last_pressed=True，本次在窗口校验就失败 → 仍要补文字。"""
        bot._voice_sender.last_pressed = True
        self.opener.return_value.is_current.return_value = False
        self._tick(bot, tmp_path)
        bot.sender.send.assert_called_once_with("@A 哼，算你有眼光", chat_name="测试群")

    def test_private_chat_skips_policy(self, bot, tmp_path):
        self._tick(bot, tmp_path, chat="Alice", is_group=False)
        bot.voice_policy.decide.assert_not_called()
        bot.sender.send.assert_called_once()

    def test_mock_generator_meta_ignored(self, bot, tmp_path):
        from src.reply.voice_policy import VoiceReplyPolicy
        bot.voice_policy = VoiceReplyPolicy(bot.voice_reply_config, {}, 0.0)
        bot.generator.last_voice_meta = Mock()
        self._tick(bot, tmp_path)
        bot._voice_sender.send.assert_not_called()
        bot.sender.send.assert_called_once_with("@A 哼，算你有眼光", chat_name="测试群")


def test_disabled_config_no_prompt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.sync_knowledge.sync", lambda: False)
    monkeypatch.setattr("src.bot.wechat_bot.GlobalStore", Mock)
    monkeypatch.setattr("src.bot.wechat_bot.MemoryEngine", Mock)
    monkeypatch.setattr("src.bot.wechat_bot.load_voice_reply_config", lambda: None)
    bot = WeChatBot(PROFILE_WECHAT_MAC_1760X1280, llm_client=Mock(), perception=Mock())
    assert bot.voice_policy is None and bot.voice_reply_config is None
    assert bot.generator.voice_prompt == ""



class TestSwitchRespectsSilentWhitelist:
    """静默模式下，切换未读聊天只切到白名单里的聊天。"""

    @pytest.fixture
    def bot(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("scripts.sync_knowledge.sync", lambda: False)
        monkeypatch.setattr("src.bot.wechat_bot.GlobalStore", Mock)
        monkeypatch.setattr("src.bot.wechat_bot.MemoryEngine", Mock)
        self.clicked = []
        clicker = Mock()
        clicker.click_item.side_effect = lambda item: self.clicked.append(item.nickname) or True
        monkeypatch.setattr("src.bot.wechat_bot.ChatListClicker", lambda *a, **k: clicker)
        bot = WeChatBot(PROFILE_WECHAT_MAC_1760X1280, llm_client=Mock(), perception=Mock())
        bot._weflow_mode = "ocr"
        bot.debug_logger = Mock(current=None)
        bot.sender = Mock(silent_mode=True)
        bot.sender.in_silent_whitelist.side_effect = lambda name: name.startswith("人格测试")
        self.item = lambda name, unread: ChatListItem(name, "", unread, "", Rect(0, 0, 10, 10))
        return bot

    def _result(self, items):
        return PerceptionResult(chat_name="其他群", messages=[], chat_list_items=items, screenshot_path="",
                                window_rect=Rect(0, 0, 880, 640), scale_factor=2.0)

    def test_skips_non_whitelisted_chat_with_more_unread(self, bot):
        result = self._result([self.item("弓箭2战神公会", "8"), self.item("人格测试", "2")])
        assert bot._try_switch_to_unread_chat(result) == "人格测试"
        assert self.clicked == ["人格测试"]

    def test_no_switch_when_only_non_whitelisted_unread(self, bot):
        result = self._result([self.item("弓箭2战神公会", "8")])
        assert bot._try_switch_to_unread_chat(result) == ""
        assert self.clicked == []

    def test_switches_freely_when_not_silent(self, bot):
        bot.sender.silent_mode = False
        result = self._result([self.item("弓箭2战神公会", "8"), self.item("人格测试", "2")])
        assert bot._try_switch_to_unread_chat(result) == "弓箭2战神公会"
