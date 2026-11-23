"""按群功能开关配置（GroupConfig）及 ReplyGenerator / WeChatBot 接线测试。"""

import json

from src.bot.group_config import GroupConfig, normalize_group_key
from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator


def test_normalize_group_key_strips_unread_and_normalizes():
    assert normalize_group_key("战神公会群 (45)") == "战神公会群"
    assert normalize_group_key("  战神公会群（测试）  (3) ") == "战神公会群(测试)"


def test_group_config_missing_file_falls_back_to_default(tmp_path):
    cfg = GroupConfig(tmp_path / "missing.json")
    assert cfg.is_enabled("战神公会群", "treasure_map", default=False) is False
    assert cfg.is_enabled("战神公会群", "scheduled_push", default=True) is True
    assert cfg.any_group_enabled("treasure_map", default=False) is False
    assert cfg.any_group_enabled("scheduled_push", default=True) is True


def test_group_config_precedence_and_hot_reload(tmp_path):
    path = tmp_path / "group_config.json"
    path.write_text(
        json.dumps(
            {
                "defaults": {"scheduled_push": False, "voice_reply": True},
                "groups": {
                    "战神公会群": {"scheduled_push": True, "treasure_map": True},
                    "测试群": {"voice_reply": False, "chime_in": True},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = GroupConfig(path)
    assert cfg.is_enabled("战神公会群 (12)", "scheduled_push", default=False) is True
    assert cfg.is_enabled("战神公会群", "treasure_map", default=False) is True
    assert cfg.is_enabled("战神公会群", "voice_reply", default=False) is True
    assert cfg.is_enabled("测试群", "scheduled_push", default=True) is False
    assert cfg.is_enabled("测试群", "voice_reply", default=True) is False
    assert cfg.is_enabled("未配置群", "scheduled_push", default=True) is False
    assert cfg.any_group_enabled("treasure_map", default=False) is True
    assert cfg.any_group_enabled("guild_excel", default=False) is False

    # 热加载：修改文件后自动生效
    path.write_text(
        json.dumps({"groups": {"测试群": {"voice_reply": True}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 确保 mtime 发生变化
    cfg._mtime_ns = -1
    assert cfg.is_enabled("测试群", "voice_reply", default=False) is True


def test_generator_respects_per_group_chime_in_direct_and_voice(tmp_path):
    path = tmp_path / "group_config.json"
    path.write_text(
        json.dumps(
            {
                "groups": {
                    "群A": {"chime_in": True, "direct_group_reply": True, "voice_reply": True},
                    "群B": {"chime_in": False, "direct_group_reply": False, "voice_reply": False},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = GroupConfig(path)
    gen = ReplyGenerator(llm_client=None, voice_prompt="VOICE_PROMPT", group_config=cfg)
    gen.enable_chime_in = False
    gen.enable_direct_group_reply = True

    assert gen.is_chime_in_enabled("群A") is True
    assert gen.is_chime_in_enabled("群B") is False
    assert gen.is_direct_group_reply_enabled("群A") is True
    assert gen.is_direct_group_reply_enabled("群B") is False
    assert gen.is_voice_reply_enabled("群A") is True
    assert gen.is_voice_reply_enabled("群B") is False

    msg_a = ChatMessage(text="哈哈", sender="Alice", sender_type=SenderType.OTHER, chat_name="群A")
    msg_b = ChatMessage(text="哈哈", sender="Alice", sender_type=SenderType.OTHER, chat_name="群B")
    assert gen.group_reply_candidates("群A", [msg_a], [msg_a]) == [msg_a]
    assert gen.group_reply_candidates("群B", [msg_b], [msg_b]) == []


def test_generator_per_group_persona_path_override(tmp_path, monkeypatch):
    persona_b = tmp_path / "persona_b.md"
    persona_b.write_text("<instructions><identity>测试群专属人设</identity></instructions>", encoding="utf-8")
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps({"groups": {"群B": {"persona_path": str(persona_b)}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    gen = ReplyGenerator(llm_client=None, group_config=GroupConfig(cfg_path))
    prompt = gen._system_prompt(is_group=True, chat_name="群B")
    assert "测试群专属人设" in prompt


def test_relationship_evaluator_per_group_override(tmp_path):
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "groups": {
                    "弓箭2战神公会": {"relationship_evaluator": True},
                    "戰神管理群": {"relationship_evaluator": False},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = GroupConfig(cfg_path)
    assert cfg.any_group_enabled("relationship_evaluator", default=False) is True
    assert cfg.is_enabled("弓箭2战神公会", "relationship_evaluator", default=False) is True
    assert cfg.is_enabled("戰神管理群", "relationship_evaluator", default=True) is False
    assert cfg.is_enabled("未配置群", "relationship_evaluator", default=True) is True
