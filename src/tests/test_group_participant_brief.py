"""群聊回复时带上发言人的精简 wiki：精简规则、最多 3 人、私聊不变。"""

from unittest.mock import Mock

import pytest

from src.memory.engine import MemoryEngine
from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.tools import get_registry, register_builtin_tools

_REG = get_registry()
register_builtin_tools(_REG)

WIKI = """# 相缺

## 基本信息
- 姓名/本名：
- 性别：男（来源：弓箭2战神公会，2026-11-03）
- 现居城市：

## 别名
- 别名：缺缺

## 共同群聊
- 弓箭2战神公会

## 与 Bot 的关系
（暂无）

## MBTI
- 可能 ENFP

## 近期动态
- 2026-11-04 夸囡囡（来源：弓箭2战神公会）

## 说过的话（短期）
- 2026-11-04 “你真棒”

## 交互风格
- 语气直接
"""


@pytest.fixture
def engine(tmp_path):
    e = MemoryEngine()
    e.wiki_dir = tmp_path
    (tmp_path / "users").mkdir()
    (tmp_path / "groups").mkdir()
    e._facts, e._corrections, e._entity_corrections, e._aliases = {}, {}, {}, {}
    return e


def test_brief_rules(engine):
    (engine.wiki_dir / "users" / "相缺.md").write_text(WIKI, encoding="utf-8")
    brief = engine.get_user_brief("相缺")
    assert "性别：男" in brief
    assert "来源" not in brief and "姓名/本名" not in brief and "暂无" not in brief
    for dropped in ("缺缺", "共同群聊", "ENFP", "说过的话", "你真棒", "# 相缺"):
        assert dropped not in brief
    assert "近期动态" in brief and "夸囡囡" in brief and "语气直接" in brief


def test_brief_cut_by_whole_lines(engine):
    lines = "\n".join(f"- 2026-11-0{i % 9 + 1} 第{i}件事情比较长的描述内容" for i in range(60))
    (engine.wiki_dir / "users" / "甲.md").write_text(f"# 甲\n\n## 近期动态\n{lines}\n", encoding="utf-8")
    brief = engine.get_user_brief("甲", max_chars=400)
    assert len(brief) <= 400
    assert brief.splitlines()[-1].endswith("描述内容")


def test_brief_empty_or_placeholder(engine):
    assert engine.get_user_brief("没有这个人") == ""
    assert engine.get_user_brief("未知") == ""


def _msg(sender, text="hi", chat="弓箭2战神公会"):
    return ChatMessage(text=text, sender=sender, sender_type=SenderType.OTHER, chat_name=chat)


def _gen():
    mem = Mock()
    mem.get_group_memory.return_value = "群记忆"
    mem.get_user_memory.return_value = "完整记忆"
    mem.get_user_brief.side_effect = lambda name, max_chars=400: f"{name}的简介"
    return ReplyGenerator(llm_client=None, memory_engine=mem, tool_registry=_REG), mem


def test_group_prompt_includes_up_to_three_recent_senders():
    gen, mem = _gen()
    unread = [_msg("A"), _msg("B"), _msg("A"), _msg("C"), _msg("D")]
    prompt = gen._build_user_prompt(unread, unread, is_group=True)
    assert "<group_memory>群记忆</group_memory>" in prompt
    for name in ("D", "C", "A"):
        assert f'<participant_memory subject="{name}">{name}的简介</participant_memory>' in prompt
    assert 'subject="B"' not in prompt
    assert prompt.index('subject="D"') < prompt.index('subject="C"') < prompt.index('subject="A"')
    mem.get_user_memory.assert_not_called()


def test_private_prompt_unchanged():
    gen, mem = _gen()
    unread = [_msg("A", chat="A")]
    prompt = gen._build_user_prompt(unread, unread, is_group=False)
    assert '<participant_memory subject="A">完整记忆</participant_memory>' in prompt
    mem.get_user_brief.assert_not_called()
