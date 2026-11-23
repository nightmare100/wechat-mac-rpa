"""持久化关系引擎（Relationship Engine V1）单元测试。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.reply.relationship import describe_relationships
from src.reply.relationship_store import (
    DAILY_MAX_AFFECTION_GAIN,
    DAILY_MAX_FAMILIARITY_GAIN,
    DAILY_MAX_TRUST_GAIN,
    RelationshipEngine,
    RelationshipStore,
    compute_stage,
)
from src.tools import get_registry, register_builtin_tools

_REGISTRY = get_registry()
register_builtin_tools(_REGISTRY)


@pytest.fixture(autouse=True)
def _bot_env(monkeypatch):
    monkeypatch.setenv("BUDDY_MATCH", "朧月,胧月")
    monkeypatch.setenv("BOT_NAME", "『宵桜』囡囡")
    monkeypatch.setenv("BOT_AT_KEYWORD", "囡囡")


def _msg(
    text: str,
    sender: str = "甲",
    kind: SenderType = SenderType.OTHER,
    chat_name: str = "测试群",
    wxid: str | None = None,
    chatroom_id: str | None = "room_1",
) -> ChatMessage:
    return ChatMessage(
        text=text,
        sender=sender,
        sender_type=kind,
        chat_name=chat_name,
        sender_wxid=wxid,
        chatroom_id=chatroom_id,
    )


def _make_engine(tmp_path: Path, clock: list[float], wiki_users: list[str] = ()) -> RelationshipEngine:
    wiki_dir = tmp_path / "wiki_users"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    for name in wiki_users:
        (wiki_dir / f"{name}.md").write_text(f"# {name}\n老群友", encoding="utf-8")
    store = RelationshipStore(db_path=tmp_path / "relationship.db")
    return RelationshipEngine(store=store, user_wiki_dir=wiki_dir, now=lambda: clock[0])


def test_persistence_across_restart_and_wxid_rename(tmp_path):
    clock = [1_700_000_000.0]
    engine1 = _make_engine(tmp_path, clock)
    engine1.apply_event("room_1", "张三", "sincere_care", sender_wxid="wxid_zhang")
    engine1.store.close()

    # 模拟重启并更换群昵称，通过 wxid 仍能继承同一份关系
    engine2 = _make_engine(tmp_path, clock)
    rec = engine2.get_or_create("room_1", "张三改名了", sender_wxid="wxid_zhang", persist=True)
    assert rec.user_key == "wxid:wxid_zhang"
    assert rec.affection == pytest.approx(1.0)
    assert "张三" in rec.aliases and rec.display_name == "张三改名了"
    engine2.store.close()


def test_legacy_name_record_migrates_to_wxid(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    # Vision 模式先按昵称建档并积累事件
    engine.apply_event("room_1", "李四", "helpful_share", sender_wxid=None)
    assert engine.store.get("room_1", "name:李四") is not None

    # 后续 TraceMemo 模式带 wxid 出现，自动合并迁移
    rec = engine.get_or_create("room_1", "李四", sender_wxid="wxid_li", persist=True)
    assert rec.user_key == "wxid:wxid_li"
    assert rec.affection == pytest.approx(0.8)
    assert engine.store.get("room_1", "name:李四") is None
    assert len(engine.store.list_events("room_1", "wxid:wxid_li")) == 1
    engine.store.close()


def test_old_friend_cold_start_from_user_wiki(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock, wiki_users=["老王"])
    _, level_old, _ = engine.describe_person("room_1", "老王", sender_wxid="wxid_wang")
    _, level_new, _ = engine.describe_person("room_1", "新人", sender_wxid="wxid_new")
    assert level_old == "acquaintance"
    assert level_new == "stranger"
    engine.store.close()


def test_repeat_decay_and_daily_positive_cap(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    # 同一天连续 4 次 sincere_care（基础 affection=1.0）：衰减为 1.0, 0.5, 0.2, 0.0 -> 合计 1.7
    for _ in range(4):
        engine.apply_event("room_1", "甲", "sincere_care", sender_wxid="wxid_a")
    rec = engine.store.get("room_1", "wxid:wxid_a")
    assert rec.affection == pytest.approx(1.7)

    # 再叠加多种其他正面事件，当日正向总增幅不得超过日上限
    for ev in ("remembered_detail", "helpful_share", "friendly_banter", "daily_chat"):
        engine.apply_event("room_1", "甲", ev, sender_wxid="wxid_a")
    rec = engine.store.get("room_1", "wxid:wxid_a")
    assert rec.affection <= DAILY_MAX_AFFECTION_GAIN + 1e-6
    assert rec.familiarity <= DAILY_MAX_FAMILIARITY_GAIN + 1e-6
    assert rec.trust <= DAILY_MAX_TRUST_GAIN + 1e-6
    engine.store.close()


def test_stage_hysteresis_and_friend_cap():
    assert compute_stage("stranger", 9.9, 0, 0, 1) == "stranger"
    assert compute_stage("stranger", 10.0, 0, 0, 1) == "acquaintance"
    # 缓冲带：acquaintance 降回 stranger 需要 familiarity < 5
    assert compute_stage("acquaintance", 6.0, 0, 0, 1) == "acquaintance"
    assert compute_stage("acquaintance", 4.9, 0, 0, 1) == "stranger"
    # familiar 升级需同时满足三维与互动天数
    assert compute_stage("acquaintance", 40, 30, 25, 4) == "acquaintance"
    assert compute_stage("acquaintance", 40, 30, 25, 5) == "familiar"
    # 缓冲带：affection=20 仍保持 familiar，跌破 18 才降回 acquaintance
    assert compute_stage("familiar", 40, 20, 20, 5) == "familiar"
    assert compute_stage("familiar", 40, 17.5, 20, 5) == "acquaintance"
    # 最高封顶 friend
    assert compute_stage("familiar", 99, 99, 99, 30) == "friend"


def test_negative_events_guarded_isolation_and_apology(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock, wiki_users=["甲", "乙"])
    m_a = _msg("发个语音听听", sender="甲", wxid="wxid_a")
    m_b = _msg("今天出货了", sender="乙", wxid="wxid_b")
    engine.record_replies("测试群", [m_a, m_b], ["@甲 不发语音", "@乙 运气不错"], chatroom_id="room_1")

    _, level_a, _ = engine.describe_person("room_1", "甲", sender_wxid="wxid_a")
    _, level_b, _ = engine.describe_person("room_1", "乙", sender_wxid="wxid_b")
    assert level_a == "guarded"
    assert level_b == "acquaintance"  # 不误伤乙

    # 真诚道歉可缩短剩余警戒期，但不会立刻解除且不会完全恢复信任损失
    rec_before = engine.store.get("room_1", "wxid:wxid_a")
    before_until = rec_before.guarded_until
    engine.apply_event("room_1", "甲", "sincere_apology", sender_wxid="wxid_a")
    rec_after = engine.store.get("room_1", "wxid:wxid_a")
    assert rec_after.guarded_until < before_until
    assert rec_after.trust < 15.0  # 初始 15 - 3 + 0.5 = 12.5
    engine.store.close()


def test_record_replies_respects_daily_chat_interval(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock)
    m = _msg("在吗", sender="甲", wxid="wxid_a")
    engine.record_replies("测试群", [m], ["@甲 在哒"], chatroom_id="room_1")
    rec1 = engine.store.get("room_1", "wxid:wxid_a")
    assert rec1.familiarity == pytest.approx(0.8)

    # 3 分钟后再回同一人，不重复计 daily_chat
    clock[0] += 180.0
    engine.record_replies("测试群", [m], ["@甲 嗯嗯"], chatroom_id="room_1")
    rec2 = engine.store.get("room_1", "wxid:wxid_a")
    assert rec2.familiarity == pytest.approx(0.8)

    # 超过 10 分钟后再回，按 24h 衰减（50%）计入
    clock[0] += 601.0
    engine.record_replies("测试群", [m], ["@甲 来啦"], chatroom_id="room_1")
    rec3 = engine.store.get("room_1", "wxid:wxid_a")
    assert rec3.familiarity == pytest.approx(1.2)
    engine.store.close()


def test_prompt_injects_multiple_relationships_without_scores(tmp_path):
    clock = [1_700_000_000.0]
    engine = _make_engine(tmp_path, clock, wiki_users=["老王"])
    gen = ReplyGenerator(llm_client=None, tool_registry=_REGISTRY, relationship_engine=engine)
    unreplied = [
        _msg("早呀", sender="老王", wxid="wxid_wang"),
        _msg("你好", sender="新人", wxid="wxid_new"),
        _msg("上线吗", sender="朧月", wxid="wxid_buddy"),
    ]
    prompt = gen._build_user_prompt(unreplied, unreplied, is_group=True)
    root = ET.fromstring(prompt)
    rels = root.findall("./session/relationship")
    assert [(r.attrib["person"], r.attrib["level"]) for r in rels] == [
        ("老王", "acquaintance"),
        ("新人", "stranger"),
        ("朧月", "buddy"),
    ]
    # 严禁暴露好感度数字
    for r in rels:
        assert "20" not in (r.text or "")
        assert "15" not in (r.text or "")
    engine.store.close()


def test_stateless_fallback_refused_does_not_bleed_to_other_person():
    history = [
        _msg("发个语音呗", sender="甲"),
        _msg("@乙 别闹", sender="自己", kind=SenderType.SELF),
    ]
    rels = describe_relationships([_msg("在吗", sender="甲")], history)
    assert rels[0][1] == "known"
