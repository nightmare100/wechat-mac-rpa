"""心情引擎（Mood Engine V1）单元测试。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.models.base import ChatMessage, SenderType
from src.reply.current_state import current_state
from src.reply.generator import ReplyGenerator
from src.reply.mood_engine import MoodEngine, MoodStore
from src.reply.relationship_evaluator import RelationshipEvaluator
from src.reply.relationship_store import RelationshipEngine, RelationshipStore
from src.tools import get_registry, register_builtin_tools

_REGISTRY = get_registry()
register_builtin_tools(_REGISTRY)


def _make_mood_engine(tmp_path: Path, clock: list[float]) -> MoodEngine:
    store = MoodStore(db_path=tmp_path / "mood.db")
    return MoodEngine(store=store, now=lambda: clock[0])


def test_negative_mood_isolated_per_user_and_never_bleeds(tmp_path):
    clock = [1_700_000_000.0]
    mood = _make_mood_engine(tmp_path, clock)
    # 甲越界 -> 仅对甲不耐烦，绝不迁怒同群的乙
    mood.apply_event("room_1", "boundary_push", user_key="wxid:a")
    m_a, e_a, _ = mood.get_state("room_1", user_key="wxid:a")
    m_b, e_b, _ = mood.get_state("room_1", user_key="wxid:b")
    assert m_a == "不耐烦" and e_a == "平常"
    assert m_b == "平静" and e_b == "平常"
    assert mood.allows_upbeat_voice("room_1", user_key="wxid:a") is False
    assert mood.allows_upbeat_voice("room_1", user_key="wxid:b") is True
    mood.store.close()


def test_positive_mood_affects_group_and_decays_naturally(tmp_path):
    clock = [1_700_000_000.0]
    mood = _make_mood_engine(tmp_path, clock)
    mood.apply_event("room_1", "sincere_care", user_key="wxid:a", relationship_level="friend")
    # 正面情绪同时感染群级氛围
    m_a, _, _ = mood.get_state("room_1", user_key="wxid:a")
    m_group, _, _ = mood.get_state("room_1", user_key="wxid:b")
    assert m_a == "开心"
    assert m_group == "开心"

    # 经过足够长时间（3 小时），自然衰减回到「平静」
    clock[0] += 3 * 3600.0
    assert mood.get_state("room_1", user_key="wxid:a")[0] == "平静"
    assert mood.get_state("room_1", user_key="wxid:b")[0] == "平静"
    mood.store.close()


def test_mood_persistence_across_restart(tmp_path):
    clock = [1_700_000_000.0]
    m1 = _make_mood_engine(tmp_path, clock)
    m1.apply_event("room_1", "friendly_banter", user_key="wxid:a")
    m1.store.close()

    m2 = _make_mood_engine(tmp_path, clock)
    assert m2.get_state("room_1", user_key="wxid:a")[0] == "调皮"
    m2.store.close()


def test_bedtime_fatigue_and_apology_inertia(tmp_path):
    clock = [1_700_000_000.0]
    mood = _make_mood_engine(tmp_path, clock)

    # 睡前时段覆盖普通状态为犯困 + 低精力
    assert mood.get_state("room_1", user_key="wxid:a", in_bedtime=True)[:2] == ("犯困", "低")
    assert mood.allows_upbeat_voice("room_1", user_key="wxid:a", in_bedtime=True) is False

    # 15 分钟内连续回复 8 次 -> 进入低精力
    for _ in range(8):
        mood.record_bot_reply("room_1")
    assert mood.get_state("room_1", user_key="wxid:a")[1] == "低"

    # 情绪惯性：严重不耐烦后一句真诚道歉软化为轻微「小郁闷」，不会瞬间跳成开心
    mood.apply_event("room_1", "repeated_annoyance", user_key="wxid:c")
    mood.apply_event("room_1", "sincere_apology", user_key="wxid:c")
    assert mood.get_state("room_1", user_key="wxid:c")[0] == "小郁闷"

    # 异向抵消惯性：不耐烦(55) 遇到普通关心(45)，不会一步翻成开心，先缓和或回到平静
    mood.apply_event("room_1", "boundary_push", user_key="wxid:d")
    mood.apply_event("room_1", "sincere_care", user_key="wxid:d")
    assert mood.get_state("room_1", user_key="wxid:d")[0] == "平静"
    mood.store.close()


def test_voice_mood_feedback_and_prompt_injection(tmp_path):
    clock = [1_700_000_000.0]
    mood = _make_mood_engine(tmp_path, clock)
    mood.apply_voice_mood("room_1", "调皮", user_key="wxid:a")

    gen = ReplyGenerator(llm_client=None, tool_registry=_REGISTRY, mood_engine=mood)
    msg = ChatMessage(
        text="哈哈", sender="甲", sender_type=SenderType.OTHER,
        chat_name="测试群", chatroom_id="room_1", sender_wxid="a",
    )
    prompt = gen._build_user_prompt([msg], [msg], is_group=True)
    state = ET.fromstring(prompt).find("./session/current_state")
    assert state.attrib["mood"] == "调皮"
    # 严禁在规则文案里出现动作描写或分数
    assert "动作" not in (state.text or "")
    mood.store.close()


def test_relationship_evaluator_feeds_mood_engine(tmp_path):
    class _FakeLLM:
        def chat(self, messages=None, **kwargs):
            return '{"events":[{"event_type":"remembered_detail","intensity":1.0,"confidence":0.9,"summary":"记得上次的事"}]}'

    clock = [1_700_000_000.0]
    rel_engine = RelationshipEngine(
        store=RelationshipStore(db_path=tmp_path / "rel.db"),
        user_wiki_dir=tmp_path / "wiki",
        now=lambda: clock[0],
    )
    mood = _make_mood_engine(tmp_path, clock)
    evaluator = RelationshipEvaluator(rel_engine, _FakeLLM(), mood_engine=mood, batch_turns=1, now=lambda: clock[0])

    msg = ChatMessage(
        text="你还记得上次说的吗", sender="甲", sender_type=SenderType.OTHER,
        chat_name="测试群", chatroom_id="room_1", sender_wxid="a",
    )
    evaluator.enqueue_replies("测试群", [msg], ["@甲 记得呀"])
    assert evaluator.flush_due() == 1
    assert mood.get_state("room_1", user_key="wxid:a")[0] == "开心"

    # 无 mood_engine 时的 current_state 向后兼容
    assert current_state(False)[:2] == ("平静", "平常")
    assert current_state(True)[:2] == ("犯困", "低")
    rel_engine.store.close()
    mood.store.close()
