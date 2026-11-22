"""群聊表情包策略（StickerPolicy）与 ReplyGenerator / GroupConfig 接线测试。"""

import json
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

from src.bot.group_config import GroupConfig
from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.reply.sticker_policy import (
    StickerPolicy,
    StickerReplyConfig,
    load_sticker_reply_config,
    parse_sticker_meta,
    render_sticker_prompt,
)


def _make_stickers(tmp_path: Path) -> Path:
    root = tmp_path / "stickers"
    for tag, files in {
        "开心": ["a.gif", "b.png"],
        "谢谢": ["t1.webp", "t2.jpg"],
        "空目录": [],
    }.items():
        d = root / tag
        d.mkdir(parents=True)
        for name in files:
            (d / name).write_bytes(b"img")
    (root / "开心" / ".hidden.png").write_bytes(b"x")
    (root / "开心" / "note.txt").write_text("ignore", encoding="utf-8")
    return root


def test_load_config_disabled_and_valid(tmp_path):
    cfg_path = tmp_path / "sticker_reply.json"
    assert load_sticker_reply_config(cfg_path) is None
    cfg_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert load_sticker_reply_config(cfg_path) is None

    cfg_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "dry_run": False,
                "stickers_dir": str(tmp_path / "stickers"),
                "append_probability": 0.4,
                "replace_probability": 0.2,
                "cooldown_replies": 2,
                "cooldown_minutes": 5,
                "recent_no_repeat": 3,
                "replace_max_chars": 6,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_sticker_reply_config(cfg_path)
    assert cfg is not None
    assert cfg.dry_run is False
    assert cfg.append_probability == 0.4
    assert cfg.replace_max_chars == 6


def test_parse_meta_and_prompt_template():
    assert parse_sticker_meta({"tag": "谢谢", "mode": "replace"}) == {"tag": "谢谢", "mode": "replace"}
    assert parse_sticker_meta({"tag": "开心"}) == {"tag": "开心", "mode": "append"}
    assert parse_sticker_meta({"tag": ""}) is None
    assert parse_sticker_meta("invalid") is None

    tpl = Path("prompts/sticker_reply.md").read_text(encoding="utf-8")
    rendered = render_sticker_prompt(tpl, ["开心", "谢谢"])
    assert "开心、谢谢" in rendered
    assert ET.fromstring(rendered).tag == "sticker_annotation"
    assert render_sticker_prompt(tpl, []) == ""


def test_policy_voice_exclusion_and_replace_downgrade(tmp_path):
    root = _make_stickers(tmp_path)
    cfg = StickerReplyConfig(
        dry_run=False,
        stickers_dir=root,
        append_probability=1.0,
        replace_probability=1.0,
        cooldown_replies=0,
        cooldown_minutes=0,
        recent_no_repeat=5,
        replace_max_chars=6,
    )
    policy = StickerPolicy(cfg, rng=lambda: 0.0)
    assert policy.available_tags() == ["开心", "谢谢"]

    # 已发语音互斥
    d = policy.decide("群A", ["@李桑 谢谢"], {"tag": "谢谢", "mode": "append"}, voiced=True)
    assert d.send_sticker is False

    # 短回复允许 replace
    d = policy.decide("群A", ["@李桑 谢谢呀"], {"tag": "谢谢", "mode": "replace"}, voiced=False)
    assert d.send_sticker is True
    assert d.mode == "replace"

    # 长回复自动降级为 append
    d = policy.decide("群A", ["@李桑 这段解释很长不能只发一张表情包"], {"tag": "谢谢", "mode": "replace"}, voiced=False)
    assert d.send_sticker is True
    assert d.mode == "append"


def test_policy_cooldown_and_no_repeat_per_chat(tmp_path):
    root = _make_stickers(tmp_path)
    cfg = StickerReplyConfig(
        dry_run=False,
        stickers_dir=root,
        append_probability=1.0,
        replace_probability=1.0,
        cooldown_replies=2,
        cooldown_minutes=3.0,
        recent_no_repeat=5,
        replace_max_chars=8,
    )
    policy = StickerPolicy(cfg, rng=lambda: 0.0)
    t0 = datetime(2026, 11, 22, 12, 0, 0)

    d1 = policy.decide("群A", ["哈哈"], {"tag": "开心", "mode": "append"}, now=t0)
    assert d1.send_sticker is True
    policy.record_sent("群A", image_path=d1.image_path, sent=True, now=t0)

    # 群B 独立不受群A冷却影响
    d_other = policy.decide("群B", ["哈哈"], {"tag": "开心", "mode": "append"}, now=t0)
    assert d_other.send_sticker is True

    # 群A 回复条数不足 → 冷却拦截
    assert policy.decide("群A", ["哈哈"], {"tag": "开心", "mode": "append"}, now=t0 + timedelta(minutes=10)).send_sticker is False
    policy.record_sent("群A", sent=False)
    policy.record_sent("群A", sent=False)

    # 群A 条数够了但时间不足 → 冷却拦截
    assert policy.decide("群A", ["哈哈"], {"tag": "开心", "mode": "append"}, now=t0 + timedelta(minutes=1)).send_sticker is False

    # 群A 条数和时间都满足 → 放行且换另一张图（不重复 d1）
    d2 = policy.decide("群A", ["哈哈"], {"tag": "开心", "mode": "append"}, now=t0 + timedelta(minutes=5))
    assert d2.send_sticker is True
    assert d2.image_path != d1.image_path


def test_generator_injects_sticker_prompt_and_respects_group_config(tmp_path):
    class _LLM:
        def __init__(self, responses):
            self.responses = list(responses)
            self.idx = 0

        def chat(self, messages, tools=None, **kwargs):
            r = self.responses[self.idx]
            self.idx += 1
            return r

    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps({"groups": {"群A": {"sticker_reply": True}, "群B": {"sticker_reply": False}}}),
        encoding="utf-8",
    )
    tpl = Path("prompts/sticker_reply.md").read_text(encoding="utf-8")
    llm = _LLM([
        '{"replies": ["@李桑 谢谢呀"], "sticker": {"tag": "谢谢", "mode": "append"}}',
        '{"replies": ["@李桑 谢谢呀"]}',
    ])
    gen = ReplyGenerator(
        llm_client=llm,
        group_config=GroupConfig(cfg_path),
        sticker_prompt=tpl,
        sticker_tags_provider=lambda: ["开心", "谢谢"],
    )
    gen.enable_skills = False
    gen.enable_self_refine = False
    gen.enable_react_tools = False
    gen.enable_chime_in = True

    msg_a = ChatMessage(text="夸你", sender="李桑", sender_type=SenderType.OTHER, chat_name="群A")
    gen.generate([msg_a], [msg_a], is_group=True)
    assert "<sticker_annotation>" in gen.last_system_prompt
    assert "开心、谢谢" in gen.last_system_prompt
    assert gen.last_sticker_meta == {"tag": "谢谢", "mode": "append"}

    msg_b = ChatMessage(text="夸你", sender="李桑", sender_type=SenderType.OTHER, chat_name="群B")
    gen.generate([msg_b], [msg_b], is_group=True)
    assert "<sticker_annotation>" not in gen.last_system_prompt
    assert gen.last_sticker_meta is None
