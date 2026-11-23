import json

import scripts.admin as admin
from scripts.admin import _expand_persona_few_shots


def test_expand_persona_few_shots_from_ids(tmp_path, monkeypatch):
    examples = tmp_path / "persona_examples.jsonl"
    examples.write_text(json.dumps({
        "id": "style_one",
        "context": ["在吗"],
        "reply": ["咋啦"],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setenv("PERSONA_FEW_SHOT_PATH", str(examples))

    result = _expand_persona_few_shots("【persona few-shot 正文已省略；ids=style_one】")

    assert "对方：在吗" in result
    assert "本人：咋啦" in result


def test_expand_persona_few_shots_uses_dotenv_path(tmp_path, monkeypatch):
    examples = tmp_path / "persona_examples.jsonl"
    examples.write_text(json.dumps({
        "id": "style_v4",
        "context": ["旧日志"],
        "reply": ["读当前库"],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"PERSONA_FEW_SHOT_PATH={examples}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PERSONA_FEW_SHOT_PATH", raising=False)
    monkeypatch.setattr(admin, "WORK_DIR", str(tmp_path))

    result = _expand_persona_few_shots("【persona few-shot 正文已省略；ids=style_v4】")

    assert "对方：旧日志" in result
    assert "本人：读当前库" in result


def test_relationship_dashboard_empty_and_with_data(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.reply.mood_engine import MoodEngine, MoodStore
    from src.reply.relationship_store import RelationshipEngine, RelationshipStore

    monkeypatch.setattr(admin, "WORK_DIR", str(tmp_path))
    client = TestClient(admin.app)

    # 空库时页面正常渲染不报错
    resp_empty = client.get("/relationship")
    assert resp_empty.status_code == 200
    assert "尚未检测到" in resp_empty.text

    # 写入测试数据
    rel_store = RelationshipStore(db_path=tmp_path / "data" / "db" / "relationship.db")
    rel_engine = RelationshipEngine(store=rel_store, user_wiki_dir=tmp_path / "wiki")
    rel_engine.apply_event("room_1", "张三", "sincere_care", sender_wxid="wxid_z")
    rel_engine.apply_event("room_1", "李四", "boundary_push", sender_wxid="wxid_l")
    rel_store.close()

    mood_store = MoodStore(db_path=tmp_path / "data" / "db" / "mood.db")
    mood_engine = MoodEngine(store=mood_store)
    mood_engine.apply_event("room_1", "sincere_care", user_key="wxid:wxid_z")
    mood_engine.apply_event("room_1", "boundary_push", user_key="wxid:wxid_l")
    mood_store.close()

    # 总览页与 JSON API
    resp = client.get("/relationship")
    assert resp.status_code == 200
    assert "张三" in resp.text and "李四" in resp.text and "不耐烦" in resp.text

    ov = client.get("/api/relationship/overview", params={"chat": "room_1", "q": "李四"}).json()
    assert len(ov["users"]) == 1
    assert ov["users"][0]["effective_level"] == "guarded"
    assert ov["users"][0]["mood"] == "不耐烦"

    # 用户详情页
    detail = client.get("/relationship/user", params={"chat_key": "room_1", "user_key": "wxid:wxid_l"})
    assert detail.status_code == 200
    assert "boundary_push" in detail.text

    # 解除警戒 + 重置心情 API
    assert client.post("/api/relationship/clear-guard", json={"chat_key": "room_1", "user_key": "wxid:wxid_l"}).status_code == 200
    assert client.post("/api/relationship/reset-mood", json={"chat_key": "room_1", "user_key": "wxid:wxid_l"}).status_code == 200
    ov2 = client.get("/api/relationship/overview", params={"chat": "room_1", "q": "李四"}).json()
    assert ov2["users"][0]["is_guarded"] is False
    assert ov2["users"][0]["mood"] == "开心"  # 个人负面已清，回落群级氛围
