"""TraceMemo contract and polling regressions; no live API or UI calls."""
import json
from unittest.mock import Mock

import pytest

from src.models.base import ChatMessage, SenderType
from src.perception import tracememo_pipeline as tm
from src.session.global_store import GlobalStore


def row(local_id=1, created=1001, **extra):
    return dict(id=str(local_id), localId=local_id, serverId=str(10000 + local_id),
                createTime=created, sessionId="a@chatroom", senderId="wxid_a",
                name="成员甲", isSender=False, type="普通文本", content="相同内容", **extra)


class API:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.fail = False
        self.groups = [dict(wxid="a@chatroom", m_nsNickName="群甲")]

    def request(self, path, params=None):
        self.calls.append((path, params))
        if path == "/health":
            return dict(ok=True, ready=True, skill={"version": "1.4.0"})
        if path == "/chatroom":
            return {"chatrooms": self.groups}
        if self.fail:
            raise tm.TraceMemoError("请求失败")
        return {"messages": self.messages, "count": len(self.messages)}


def pipeline(api=None, targets=None, clock=None):
    return tm.TraceMemoPipeline(client=api or API(), targets=targets or ["群甲"],
                                clock=clock or (lambda: 1000), ui=Mock())


def test_maps_real_chatlog_fields_and_self():
    api = API()
    api.messages = [row(2, 1002), {**row(1, 1001), "isSender": True}]
    result = pipeline(api).perceive()
    assert result.chat_name == "群甲" and result.is_group
    assert not result.screenshot_path and not result.chat_list_items
    assert [m.local_id for m in result.messages] == [1, 2]
    own, other = result.messages
    assert own.sender_type == SenderType.SELF and own.replied
    assert other.sender == "成员甲" and other.sender_wxid == "wxid_a"
    assert other.chatroom_id == "a@chatroom" and other.server_id == "10002"
    assert other.create_time == 1002 and not other.replied


def test_startup_history_context_and_system_are_not_reply_targets():
    api = API()
    api.messages = [row(created=999), {**row(2), "from": "system", "type": "系统消息"}]
    assert all(m.replied for m in pipeline(api).perceive().messages)


def test_failed_poll_does_not_advance_range_and_overlaps_success():
    api = API()
    now = [1000]
    p = pipeline(api, clock=lambda: now[0])
    p.perceive()
    now[0] = 1200
    api.fail = True
    with pytest.raises(tm.TraceMemoError):
        p.perceive()
    failed_start = api.calls[-1][1]["startTime"]
    now[0] = 1500
    api.fail = False
    p.perceive()
    assert api.calls[-1][1]["startTime"] == failed_start
    now[0] = 1510
    p.perceive()
    assert api.calls[-1][1]["startTime"] <= 1500 - 120


def test_requires_unique_group_and_refuses_normalized_name_collisions():
    api = API()
    api.groups.append(dict(wxid="b@chatroom", m_nsNickName="群甲"))
    with pytest.raises(tm.TraceMemoError, match="同名|歧义"):
        pipeline(api)
    with pytest.raises(tm.TraceMemoError, match="同名|歧义"):
        pipeline(api, targets=["a@chatroom"])


def test_empty_targets_and_unknown_group_fail_closed(monkeypatch):
    monkeypatch.delenv("TRACEMEMO_CHATS", raising=False)
    monkeypatch.delenv("SILENT_WHITELIST", raising=False)
    with pytest.raises(tm.TraceMemoError):
        tm.TraceMemoPipeline(client=API(), ui=Mock())
    with pytest.raises(tm.TraceMemoError):
        pipeline(targets=["不存在"])


def test_round_robin_retains_empty_chats_for_pending_replies():
    api = API()
    api.groups.append(dict(wxid="b@chatroom", m_nsNickName="群乙"))
    p = pipeline(api, targets=["群甲", "群乙"])
    assert [p.perceive().chat_name for _ in range(3)] == ["群甲", "群乙", "群甲"]


def test_media_placeholder_and_cached_voice_do_not_invent_content():
    api = API()
    api.messages = [{**row(), "type": "图片", "content": "[图片]"},
                    {**row(2), "type": "语音", "content": "[语音消息]",
                     "voiceTranscriptState": "transcribed", "voiceTranscript": "语音正文"}]
    image, voice = pipeline(api).perceive().messages
    assert image.message_type == "image" and not image.image_description
    assert "未解析" in image.text
    assert voice.text == "语音正文"


def test_exact_ids_keep_repeated_content_and_ignore_overlap(tmp_path):
    store = GlobalStore(state_file=str(tmp_path / "state.json"))
    first = ChatMessage("重复", "甲", SenderType.OTHER, "群甲", local_id=1, server_id="11", create_time=1001)
    second = ChatMessage("重复", "甲", SenderType.OTHER, "群甲", local_id=2, server_id="12", create_time=1002)
    own = ChatMessage("重复", "自己", SenderType.SELF, "群甲", local_id=3, server_id="13", create_time=1003)
    store.merge_tick("群甲", [first, second, own], mode="tracememo", is_group=True)
    state, unreplied = store.merge_tick("群甲", [first, second, own], mode="tracememo", is_group=True)
    assert len(state.messages) == 3 and len(unreplied) == 2
    store.mark_replied("群甲", state.messages[0], "收到")
    assert not state.messages[1].replied


def test_http_auth_preference_and_error_redaction(monkeypatch):
    monkeypatch.setenv("TRACEMEMO_API_TOKEN", "new-secret")
    monkeypatch.setenv("WECHATEXPLORER_API_TOKEN", "old-secret")
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"chatrooms": []}).encode()
    opener = Mock(return_value=response)
    client = tm.TraceMemoClient(opener=opener)
    client.request("/chatroom")
    assert opener.call_args.args[0].get_header("Authorization") == "Bearer new-secret"
    client.request("/health")
    assert opener.call_args.args[0].get_header("Authorization") is None
    opener.side_effect = ValueError("new-secret")
    with pytest.raises(tm.TraceMemoError) as error:
        client.request("/chatroom")
    assert "new-secret" not in str(error.value)


def test_identical_messages_in_same_second_survive_save_reload(tmp_path):
    path = str(tmp_path / "state.json")
    store = GlobalStore(state_file=path)
    api = API()
    api.messages = [row(1), row(2)]
    messages = pipeline(api).perceive().messages
    store.merge_tick("群甲", messages, mode="tracememo", is_group=True)
    store.save()
    restored = GlobalStore(state_file=path)
    assert len(restored.chats["群甲"].messages) == 2
    assert all(m.sender_wxid == "wxid_a" for m in restored.chats["群甲"].messages)
    state, _ = restored.merge_tick("群甲", messages, mode="tracememo", is_group=True)
    assert len(state.messages) == 2
    # Switching back to vision must not change the persisted identity of API rows.
    restored.merge_tick("群甲", [], mode="ocr", is_group=True)
    restored.mark_replied("群甲", state.messages[0], "已处理")
    restored.save()
    assert len(GlobalStore(state_file=path).chats["群甲"].messages) == 2


def test_marking_legacy_backlog_does_not_mark_same_text_new_api_message(tmp_path):
    store = GlobalStore(state_file=str(tmp_path / "state.json"))
    old = ChatMessage("重复", "甲", SenderType.OTHER, "群甲")
    store.merge_tick("群甲", [old], is_group=True)
    new = ChatMessage("重复", "甲", SenderType.OTHER, "群甲", local_id=2, server_id="12", create_time=1002)
    state, _ = store.merge_tick("群甲", [new], mode="tracememo", is_group=True)
    store.mark_replied("群甲", state.messages[0], "跳过旧消息")
    assert not state.messages[1].replied


def test_missing_self_flag_or_wrong_session_rejects_batch():
    api = API()
    api.messages = [{**row(), "isSender": None}]
    p = pipeline(api)
    with pytest.raises(tm.TraceMemoError):
        p.perceive()
    api.messages = [{**row(), "sessionId": "other@chatroom"}]
    with pytest.raises(tm.TraceMemoError):
        p.perceive()


def test_structured_quote_preserves_author_for_self_quote_cooldown(monkeypatch):
    from src.reply.generator import _quotes_self
    monkeypatch.setenv("BOT_NAME", "机器人昵称")
    api = API()
    api.messages = [{**row(), "content": "渲染后的引用文本", "contentData": {
        "type": "quote", "title": "新的回复", "quotedSender": "机器人昵称", "quotedContent": "原文"}}]
    message = pipeline(api).perceive().messages[0]
    assert message.text == "新的回复"
    assert message.quoted_text == "机器人昵称：原文"
    assert _quotes_self(message, [])
    api.messages[0]["contentData"]["quotedSender"] = "其他人"
    assert not _quotes_self(pipeline(api).perceive().messages[0], [])


def test_unhealthy_or_changed_contract_stops_startup():
    api = API()
    original = api.request
    api.request = lambda path, params=None: {"ok": True, "ready": False} if path == "/health" else original(path, params)
    with pytest.raises(tm.TraceMemoError, match="未就绪"):
        pipeline(api)
    api.request = lambda path, params=None: {"ok": True, "ready": True, "skill": {"version": "9.0"}} if path == "/health" else original(path, params)
    with pytest.raises(tm.TraceMemoError, match="版本"):
        pipeline(api)


def test_existing_database_adds_source_column_without_losing_rows(tmp_path):
    import sqlite3
    from src.db import init_db
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, sender_display_name TEXT, is_self BOOLEAN, source_file TEXT)")
        conn.execute("INSERT INTO messages VALUES (1, '甲', 0, NULL)")
    init_db(path)
    init_db(path)
    with sqlite3.connect(path) as conn:
        assert "source" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert conn.execute("SELECT sender_display_name FROM messages WHERE id=1").fetchone() == ("甲",)


def test_zero_server_ids_use_local_id_for_reply_marking(tmp_path):
    store = GlobalStore(state_file=str(tmp_path / "state.json"))
    messages = [ChatMessage("相同", "甲", SenderType.OTHER, "群甲", server_id="0", local_id=i) for i in (1, 2)]
    state, _ = store.merge_tick("群甲", messages, mode="tracememo", is_group=True)
    store.mark_replied("群甲", state.messages[0], "回复")
    assert not state.messages[1].replied


def test_overlap_dedup_survives_runtime_context_trim(tmp_path):
    store = GlobalStore(state_file=str(tmp_path / "state.json"), max_messages=2)
    api = API()
    api.messages = [row(i, 1000 + i) for i in range(1, 5)]
    batch = pipeline(api).perceive().messages
    state, pending = store.merge_tick("群甲", batch, mode="tracememo", is_group=True)
    for msg in pending:
        store.mark_replied("群甲", msg, "处理完毕")
    state, pending = store.merge_tick("群甲", batch, mode="tracememo", is_group=True)
    assert not pending
    assert len(state.messages) == 2
