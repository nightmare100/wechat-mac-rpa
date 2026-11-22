"""OPT-014：记忆按稳定人物卡 + 当前话题相关性筛选，避免简单问候带入旧事与多人串用。"""

from unittest.mock import Mock

from src.models.base import ChatMessage, SenderType
from src.reply.generator import ReplyGenerator
from src.reply.memory_selector import extract_topic_terms, select_relevant_memory
from src.tools import get_registry, register_builtin_tools

_REG = get_registry()
register_builtin_tools(_REG)

USER_WIKI = """# 李桑

## 基本信息
- 性别：男

## 与 Bot 的关系
- 同公会群友

## 近期动态
- 2026-11-04 讨论了上海出差和机票改签
- 2026-11-02 吐槽抽卡四次大保底
- 2026-10-28 买了一把新机械键盘
"""

GROUP_WIKI = """# 弓箭2战神公会

## 群简介
- 手游《弓箭传说2》战神公会交流群

## 近期动态
- 2026-11-05 大家在讨论魔植防守配装
- 2026-11-01 群里聊了台风停课
"""


def test_pure_greeting_keeps_stable_card_and_drops_history():
    assert extract_topic_terms("@囡囡 在吗") == set()
    selected = select_relevant_memory(USER_WIKI, "@囡囡 在吗")
    assert "性别：男" in selected
    assert "同公会群友" in selected
    assert "上海出差" not in selected
    assert "机械键盘" not in selected


def test_topic_query_recalls_only_matching_history_lines():
    selected = select_relevant_memory(USER_WIKI, "上次上海出差机票改签顺利吗")
    assert "性别：男" in selected
    assert "上海出差" in selected
    assert "机械键盘" not in selected
    assert "四次大保底" not in selected


def test_group_senders_filter_memory_independently_without_cross_talk():
    mem = Mock()
    mem.get_group_memory.return_value = GROUP_WIKI
    mem.get_user_brief.side_effect = lambda name, max_chars=400: (
        "基本信息：\n- 性别：男\n近期动态：\n- 2026-11-05 打魔植通关\n- 2026-11-01 买键盘"
        if name == "Alice"
        else "基本信息：\n- 性别：女\n近期动态：\n- 2026-11-05 打魔植翻车\n- 2026-11-02 去杭州玩"
    )
    gen = ReplyGenerator(llm_client=None, memory_engine=mem, tool_registry=_REG)
    unread = [
        ChatMessage(text="魔植怎么打", sender="Alice", sender_type=SenderType.OTHER, chat_name="战神公会"),
        ChatMessage(text="在吗", sender="Bob", sender_type=SenderType.OTHER, chat_name="战神公会"),
    ]
    prompt = gen._build_user_prompt(unread, unread, is_group=True)

    # 群记忆按批内话题召回魔植，不带无关的台风旧事
    assert "魔植防守配装" in prompt
    assert "台风停课" not in prompt

    # Alice 聊魔植 → 召回 Alice 的魔植动态，不带买键盘
    alice_block = prompt.split('<participant_memory subject="Alice">')[1].split("</participant_memory>")[0]
    assert "打魔植通关" in alice_block
    assert "买键盘" not in alice_block

    # Bob 仅发“在吗” → 只留 Bob 的稳定信息，不因 Alice 聊魔植而召回 Bob 的魔植/杭州旧事
    bob_block = prompt.split('<participant_memory subject="Bob">')[1].split("</participant_memory>")[0]
    assert "性别：女" in bob_block
    assert "打魔植翻车" not in bob_block
    assert "去杭州玩" not in bob_block
