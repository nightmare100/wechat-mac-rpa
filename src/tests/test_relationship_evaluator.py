"""关系引擎 Phase 2（后台低频批量 LLM 事件评估器）单元测试。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.models.base import ChatMessage, SenderType
from src.reply.relationship_evaluator import RelationshipEvaluator
from src.reply.relationship_store import RelationshipEngine, RelationshipStore


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses) if isinstance(responses, list) else [responses]
        self.calls = []

    def chat(self, messages=None, **kwargs):
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        resp = self.responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _msg(text: str, sender: str = "甲", wxid: str = "wxid_a", chat_name: str = "测试群", chatroom_id: str = "room_1"):
    return ChatMessage(
        text=text,
        sender=sender,
        sender_type=SenderType.OTHER,
        chat_name=chat_name,
        sender_wxid=wxid,
        chatroom_id=chatroom_id,
    )


def _make_engine(tmp_path: Path, clock: list[float]) -> RelationshipEngine:
    store = RelationshipStore(db_path=tmp_path / "relationship.db")
    return RelationshipEngine(store=store, user_wiki_dir=tmp_path / "wiki", now=lambda: clock[0])


def test_debounce_by_batch_turns_and_idle_timeout(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    llm = _FakeLLM('{"events":[{"event_type":"sincere_care","intensity":0.8,"confidence":0.9,"summary":"关心作息"}]}')
    evaluator = RelationshipEvaluator(
        engine, llm, batch_turns=3, idle_seconds=300.0, now=lambda: clock[0]
    )

    # 第 1、2 轮未达批量上限且未空闲超时，不触发 LLM
    evaluator.enqueue_replies("测试群", [_msg("早点休息")], ["@甲 知道啦"])
    evaluator.enqueue_replies("测试群", [_msg("别熬夜")], ["@甲 嗯嗯"])
    assert evaluator.flush_due() == 0
    assert len(llm.calls) == 0

    # 第 3 轮触发批量结算，仅调用 1 次 LLM
    evaluator.enqueue_replies("测试群", [_msg("喝点热水")], ["@甲 谢谢呀"])
    assert evaluator.flush_due() == 1
    assert len(llm.calls) == 1
    rec = engine.store.get("room_1", "wxid:wxid_a")
    assert rec.affection == pytest.approx(0.8)  # 1.0 * 0.8

    # 单轮入队后经过空闲超时（>= 300s）也会结算
    evaluator.enqueue_replies("测试群", [_msg("今天打完帮大家整理攻略")], ["@甲 辛苦啦"])
    clock[0] += 301.0
    assert evaluator.flush_due() == 1
    assert len(llm.calls) == 2
    engine.store.close()


def test_filters_invalid_low_confidence_duplicate_and_daily_chat(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    raw = """```json
    {
      "events": [
        {"event_type": "daily_chat", "intensity": 1.0, "confidence": 1.0, "summary": "应被忽略"},
        {"event_type": "unknown_type", "intensity": 1.0, "confidence": 1.0, "summary": "非法类型"},
        {"event_type": "sincere_care", "intensity": 1.0, "confidence": 0.4, "summary": "置信度不足"},
        {"event_type": "helpful_share", "intensity": 0.5, "confidence": 0.8, "summary": "重复低分"},
        {"event_type": "helpful_share", "intensity": 1.0, "confidence": 0.9, "summary": "分享攻略"}
      ]
    }
    ```"""
    llm = _FakeLLM(raw)
    evaluator = RelationshipEvaluator(engine, llm, batch_turns=1, now=lambda: clock[0])
    evaluator.enqueue_replies("测试群", [_msg("攻略写好了")], ["@甲 厉害"])
    applied = evaluator.flush_due()
    assert applied == 1
    events = engine.store.list_events("room_1", "wxid:wxid_a")
    assert len(events) == 1
    assert events[0]["event_type"] == "helpful_share"
    assert events[0]["intensity"] == pytest.approx(1.0)
    engine.store.close()


def test_llm_error_and_malformed_json_degrade_silently(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    llm = _FakeLLM([RuntimeError("network down"), "not json at all"])
    evaluator = RelationshipEvaluator(engine, llm, batch_turns=1, now=lambda: clock[0])

    evaluator.enqueue_replies("测试群", [_msg("你好")], ["@甲 你好"])
    assert evaluator.flush_due() == 0
    evaluator.enqueue_replies("测试群", [_msg("在吗")], ["@甲 在哒"])
    assert evaluator.flush_due() == 0
    assert engine.store.get("room_1", "wxid:wxid_a") is None
    engine.store.close()


def test_multi_user_bucketing_and_background_worker(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    llm = _FakeLLM([
        '{"events":[{"event_type":"boundary_push","intensity":1.0,"confidence":0.95,"summary":"反复索要照片"}]}',
        '{"events":[{"event_type":"friendly_banter","intensity":1.0,"confidence":0.85,"summary":"友好接梗"}]}',
    ])
    evaluator = RelationshipEvaluator(
        engine, llm, batch_turns=1, idle_seconds=60.0, poll_interval=0.05, now=lambda: clock[0]
    )
    evaluator.start()
    try:
        m_a = _msg("发张照片", sender="甲", wxid="wxid_a")
        m_b = _msg("哈哈笑死", sender="乙", wxid="wxid_b")
        evaluator.enqueue_replies("测试群", [m_a, m_b], ["@甲 不方便", "@乙 哈哈哈"])
        deadline = time.time() + 3.0
        while evaluator.pending_count() > 0 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
    finally:
        evaluator.stop()

    assert len(llm.calls) == 2
    _, level_a, _ = engine.describe_person("room_1", "甲", sender_wxid="wxid_a")
    rec_b = engine.store.get("room_1", "wxid:wxid_b")
    assert level_a == "guarded"
    assert rec_b is not None and rec_b.affection == pytest.approx(0.6)
    engine.store.close()
