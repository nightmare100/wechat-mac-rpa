#!/usr/bin/env python3
"""Tests for L4 Reply modules."""

import xml.etree.ElementTree as ET

from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.reply.policy import ReplyPolicy
from src.session.global_store import ChatState
from src.tools import get_registry, register_builtin_tools

# 测试用全局工具注册表
_TEST_TOOL_REGISTRY = get_registry()
register_builtin_tools(_TEST_TOOL_REGISTRY)


class TestReplyPolicy:
    def test_self_message_returns_false(self):
        policy = ReplyPolicy()
        session = ChatState(chat_id="c1", chat_name="Friend")
        msg = ChatMessage(
            text="hello",
            sender="me",
            sender_type=SenderType.SELF,
            chat_name="Friend",
        )
        assert policy.should_reply(msg, session) is False

    def test_system_message_returns_false(self):
        policy = ReplyPolicy()
        session = ChatState(chat_id="c1", chat_name="Friend")
        msg = ChatMessage(
            text="system alert",
            sender="system",
            sender_type=SenderType.SYSTEM,
            chat_name="Friend",
        )
        assert policy.should_reply(msg, session) is False

    def test_group_chat_with_at_returns_true(self):
        policy = ReplyPolicy()
        from src.session.global_store import ChatState
        session = ChatState(chat_id="c1", chat_name="Group (3)")
        msg = ChatMessage(
            text="@me hello",
            sender="friend",
            sender_type=SenderType.OTHER,
            chat_name="Group (3)",
            is_at_me=True,
        )
        assert policy.should_reply(msg, session) is True

    def test_normal_private_chat_returns_true(self):
        policy = ReplyPolicy()
        from src.session.global_store import ChatState
        session = ChatState(chat_id="c1", chat_name="Alice")
        msg = ChatMessage(
            text="hello",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="Alice",
        )
        assert policy.should_reply(msg, session) is True


class TestReplyGenerator:
    def test_prompt_is_one_xml_request_without_unread_duplication(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        history = ChatMessage(
            text="旧消息",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="A&B",
        )
        unread = ChatMessage(
            text="新消息 <确认>",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="A&B",
        )

        prompt = gen._build_user_prompt([unread], [history, unread])
        root = ET.fromstring(prompt)

        assert root.tag == "request"
        assert [message.text for message in root.findall("./history/message")] == ["Alice：旧消息"]
        assert [message.text for message in root.findall("./unread/message")] == ["Alice：新消息 <确认>"]

    def test_self_history_is_interleaved_and_also_forbidden_after_unread(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        other = ChatMessage(
            text="真人旧消息",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="测试群",
        )
        self_reply = ChatMessage(
            text="Bot 已用旧梗",
            sender="我",
            sender_type=SenderType.SELF,
            chat_name="测试群",
        )
        unread = ChatMessage(
            text="真人新消息",
            sender="Bob",
            sender_type=SenderType.OTHER,
            chat_name="测试群",
        )

        prompt = gen._build_user_prompt([unread], [other, self_reply, unread], is_group=True)
        root = ET.fromstring(prompt)

        # bot 自己的回复按原顺序留在 history 里（role="self"），模型能看到完整的一来一回
        history = root.findall("./history/message")
        assert [m.text for m in history] == ["Alice：真人旧消息", "我：Bot 已用旧梗"]
        assert [m.attrib["role"] for m in history] == ["other", "self"]
        assert [m.attrib["index"] for m in history] == ["1", "2"]
        consumed = root.find("./consumed_self_replies")
        assert consumed is not None
        assert consumed.attrib["reuse"] == "forbidden"
        assert [message.text for message in consumed.findall("./message")] == ["我：Bot 已用旧梗"]
        guard = root.find("./reply_guard")
        assert guard is not None
        assert guard.attrib == {"priority": "final", "enforcement": "hard"}
        assert "不得照抄其中的句子" in "".join(guard.itertext())
        assert "不得在已用的梗上继续加戏" in "".join(guard.itertext())
        assert "明确 @ 你时必须回复，不得输出空 replies" in "".join(guard.itertext())
        assert [child.tag for child in root][-3:] == ["unread", "consumed_self_replies", "reply_guard"]

    def test_history_excludes_unread_by_identity_when_system_message_is_last(self):
        """未读不在末尾（末尾是系统消息）时，history 不能重复未读、也不能丢系统消息。"""
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        old = ChatMessage(text="旧话", sender="Alice", sender_type=SenderType.OTHER, chat_name="群")
        ask = ChatMessage(text="问bot一句", sender="Alice", sender_type=SenderType.OTHER, chat_name="群")
        system = ChatMessage(text="Bob加入了群聊", sender="系统", sender_type=SenderType.SYSTEM, chat_name="群")

        root = ET.fromstring(gen._build_user_prompt([ask], [old, ask, system], is_group=True))

        assert [m.text for m in root.findall("./history/message")] == ["Alice：旧话", "系统：Bob加入了群聊"]
        assert [m.text for m in root.findall("./unread/message")] == ["Alice：问bot一句"]

    def test_history_keeps_self_message_between_unread(self):
        """未读中间夹着「我」的消息时，history 要保留这条，且不重复未读。"""
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        old = ChatMessage(text="旧话", sender="Alice", sender_type=SenderType.OTHER, chat_name="群")
        first = ChatMessage(text="A先问", sender="Alice", sender_type=SenderType.OTHER, chat_name="群")
        mine = ChatMessage(text="手动回了一句", sender="我", sender_type=SenderType.SELF, chat_name="群")
        second = ChatMessage(text="A追问", sender="Alice", sender_type=SenderType.OTHER, chat_name="群")

        root = ET.fromstring(gen._build_user_prompt([first, second], [old, first, mine, second], is_group=True))

        assert [m.text for m in root.findall("./history/message")] == ["Alice：旧话", "我：手动回了一句"]
        assert [m.text for m in root.findall("./unread/message")] == ["Alice：A先问", "Alice：A追问"]

    def test_reply_guard_exists_without_self_history(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        unread = ChatMessage(
            text="真人新消息",
            sender="Bob",
            sender_type=SenderType.OTHER,
            chat_name="测试群",
        )

        root = ET.fromstring(gen._build_user_prompt([unread], [unread], is_group=True))

        assert [child.tag for child in root][-1] == "reply_guard"
        guard_text = "".join(root.find("./reply_guard").itertext())
        assert "要求停止交流" in guard_text
        # OCR 常把昵称识别错，不再强制"纠正错误称呼"，避免把识别错误放大成回复
        assert "identity_provocation" not in guard_text

    def test_system_prompt_does_not_embed_skill_catalog(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)

        prompt = gen._system_prompt()

        assert ET.fromstring(prompt).tag == "instructions"
        assert "<available_skills>" not in prompt
        assert "<active_skill" not in prompt

    def test_appended_sections_stay_before_final_reply_guard(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        self_reply = ChatMessage(
            text="Bot 已用旧梗",
            sender="我",
            sender_type=SenderType.SELF,
            chat_name="测试群",
        )
        unread = ChatMessage(
            text="真人新消息",
            sender="Bob",
            sender_type=SenderType.OTHER,
            chat_name="测试群",
        )
        prompt = gen._build_user_prompt([unread], [self_reply, unread], is_group=True)

        prompt = gen._append_request_sections(
            prompt,
            [
                '<active_skill name="group_banter"><skill /></active_skill>',
                '<style_examples source="verified_human"><example /></style_examples>',
            ],
        )
        root = ET.fromstring(prompt)

        assert [child.tag for child in root][-3:] == [
            "active_skill",
            "style_examples",
            "reply_guard",
        ]

    def test_returns_non_empty_string(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        msg = ChatMessage(
            text="hello",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="Alice",
        )
        reply = gen.generate([msg], [msg])
        assert isinstance(reply, list)
        assert len(reply) >= 0

    def test_handles_none_llm_client_gracefully(self):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        msg = ChatMessage(
            text="hello",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="Alice",
        )
        reply = gen.generate([msg], [msg])
        assert isinstance(reply, list)

    def test_fallback_when_llm_fails(self):
        class FailingLLM:
            def chat(self, *args, **kwargs):
                raise RuntimeError("LLM down")

        gen = ReplyGenerator(llm_client=FailingLLM(), tool_registry=_TEST_TOOL_REGISTRY)
        msg = ChatMessage(
            text="hello",
            sender="Alice",
            sender_type=SenderType.OTHER,
            chat_name="Alice",
        )
        reply = gen.generate([msg], [msg])
        assert reply == []
        assert gen.last_generation_failed is True


class TestBedtimeContext:
    def _prompt(self, window):
        gen = ReplyGenerator(llm_client=None, tool_registry=_TEST_TOOL_REGISTRY)
        gen.bedtime_window = window
        unread = ChatMessage(text="还在吗", sender="Bob", sender_type=SenderType.OTHER, chat_name="测试群")
        return ET.fromstring(gen._build_user_prompt([unread], [unread], is_group=True))

    def test_bedtime_added_inside_window(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        window = ((now - timedelta(minutes=5)).time(), (now + timedelta(minutes=5)).time())
        bedtime = self._prompt(window).find("./session/bedtime")
        assert bedtime is not None
        assert f"你 {window[1]:%H:%M} 睡觉" in bedtime.text

    def test_no_bedtime_outside_window(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        window = ((now + timedelta(hours=1)).time(), (now + timedelta(hours=2)).time())
        assert self._prompt(window).find("./session/bedtime") is None
        assert self._prompt(None).find("./session/bedtime") is None
