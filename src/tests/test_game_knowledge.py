"""《弓箭传说2》关键词游戏知识库按需注入单元测试与集成测试。"""

import os
from pathlib import Path

import pytest

from src.models.base import ChatMessage, SenderType
from src.reply.game_knowledge import GameKnowledgeBase, parse_game_knowledge
from src.reply.generator import ReplyGenerator
from src.tools import get_registry, register_builtin_tools

_REG = get_registry()
register_builtin_tools(_REG)

_REAL_KB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "game_knowledge.md"


def _msg(text: str, sender: str = "Alice", chat_name: str = "战神公会") -> ChatMessage:
    return ChatMessage(text=text, sender=sender, sender_type=SenderType.OTHER, chat_name=chat_name)


def test_parse_real_knowledge_deduplicates_and_skips_vlm_only_sections():
    text = _REAL_KB_PATH.read_text(encoding="utf-8")
    entries = parse_game_knowledge(text)
    titles = [e.title for e in entries]

    # 魔植在文档中出现两次，应去重并保留首条更完整的描述
    assert titles.count("魔植") == 1
    mozhi = next(e for e in entries if e.title == "魔植")
    assert "2 个人一起防守" in mozhi.description

    # 一、二节及三.1 品阶应被解析
    assert "公会飞船" in titles
    assert "神铸" in titles
    assert "品阶与边框颜色" in titles

    # VLM 专属段落（数值单位、截图类型、防幻觉守则）不应混入
    assert not any("只述所见" in t or "原样保留单位" in t for t in titles)
    assert not any("公会成绩单" in t for t in titles)

    # 所有提取的关键词长度必须 >= 2，避免单字误触发
    for entry in entries:
        assert all(len(kw) >= 2 for kw in entry.keywords), f"发现单字关键词: {entry}"


def test_missing_and_empty_file_fallback_gracefully(tmp_path: Path):
    missing = GameKnowledgeBase(path=tmp_path / "does_not_exist.md")
    assert missing.select([_msg("魔植怎么打")]) == ""

    empty_file = tmp_path / "empty.md"
    empty_file.write_text("", encoding="utf-8")
    empty_kb = GameKnowledgeBase(path=empty_file)
    assert empty_kb.select([_msg("魔植怎么打")]) == ""


def test_mtime_auto_reload_and_deleted_file_recovery(tmp_path: Path):
    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "## 一、基础玩法与公会日常\n\n1. **魔植**：旧说明。\n",
        encoding="utf-8",
    )
    kb = GameKnowledgeBase(path=kb_file)
    first = kb.select([_msg("接魔植")])
    assert "旧说明" in first

    # 更新内容并显式推进 mtime，验证自动热重载
    kb_file.write_text(
        "## 一、基础玩法与公会日常\n\n"
        "1. **魔植**：新说明内容。\n"
        "2. **星穹试炼**：全新限时挑战玩法。\n",
        encoding="utf-8",
    )
    st = kb_file.stat()
    os.utime(kb_file, (st.st_atime, st.st_mtime + 10))

    second = kb.select([_msg("接魔植")])
    assert "新说明内容" in second
    assert "旧说明" not in second
    assert "全新限时挑战玩法" in kb.select([_msg("今天打星穹试炼吗")])

    # 文件被删除后应平滑返回空串，不抛异常
    kb_file.unlink()
    assert kb.select([_msg("接魔植")]) == ""


@pytest.mark.parametrize(
    "utterance,expected_title_substr",
    [
        ("今天抽到神域武器了", "三套 S 装"),
        ("有人帮打魔值吗", "魔植"),
        ("爬塔卡在300层了", "通天塔"),
        ("刚合了个红圣", "圣物"),
        ("我的S装终于红了", "三套 S 装"),
        ("刚出了一件 s 装", "三套 S 装"),
        ("双生核心怎么用", "双生"),
        ("神铸1级要多少材料", "神铸"),
        ("符文铭刻和附魔先搞哪个", "铭刻"),
        ("缺狗粮升阶", "狗粮"),
        ("封印之战英杰段位好卷", "封印之战"),
        ("火龙boss怎么打", "火龙"),
        ("镰刀怪老是秒人", "镰刀怪"),
        ("神眷者伤害太高了", "神眷者"),
        ("公会远征走哪路", "公会远征"),
        ("竞技场乱斗打不过", "竞技场"),
        ("今天扫荡票用完了", "扫荡"),
        ("飞船几票上船，要捐票吗", "公会飞船"),
        ("卡关坐牢三天了", "坐牢"),
        ("刚才一碰就暴毙翻车了", "暴毙"),
        ("这期活动拉满还是白嫖", "拉满"),
        ("最近太肝了真肝不动", "拉满"),
        ("出红了没歪，大保底出的", "出金"),
        ("混沌装备比红装高一阶吧", "品阶与边框颜色"),
    ],
)
def test_typo_alias_and_term_matching(utterance: str, expected_title_substr: str):
    kb = GameKnowledgeBase(path=_REAL_KB_PATH)
    block = kb.select([_msg(utterance)])
    assert block.startswith("<game_knowledge>")
    assert block.endswith("</game_knowledge>")
    assert "仅供参考" in block
    assert expected_title_substr in block


@pytest.mark.parametrize(
    "casual_text",
    [
        "中午吃红烧肉配米饭",
        "买了一张回家的车票",
        "今天吃啥好呢",
        "金钱不是万能的",
        "孩子期末试卷发下来了",
        "体检报告说肝功能正常",
        "早安大家",
        "新年祝福送给你",
        "这事确实有点困难",
        "明天去买机票出差",
        "哈哈哈哈笑死我了",
    ],
)
def test_non_game_casual_chat_has_zero_false_positives(casual_text: str):
    kb = GameKnowledgeBase(path=_REAL_KB_PATH)
    assert kb.select([_msg(casual_text)]) == ""


def test_unread_ranked_higher_than_recent_and_recent_limited_to_last_3():
    kb = GameKnowledgeBase(path=_REAL_KB_PATH)
    recent = [
        _msg("很久前聊过钓鱼"),      # 第 -4 条，应被忽略
        _msg("刚才在聊海岛"),
        _msg("也在说扫荡"),
        _msg("还有坐牢"),
    ]
    unread = [_msg("我刚打了魔植")]

    block = kb.select(unread, recent, max_items=2, max_chars=700)
    # 未读命中的魔植必须排在最前面
    item_lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(item_lines) == 2
    assert item_lines[0].startswith("- 魔植：")
    assert "钓鱼" not in block

    # 若未读无命中，仅倒数第 4 条的钓鱼不应触发
    assert kb.select([_msg("早安")], recent[:1] + [_msg("哈哈"), _msg("嗯嗯"), _msg("好的")]) == ""


def test_budget_cap_and_xml_escaping(tmp_path: Path):
    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "## 二、高频黑话与核心概念\n\n"
        "- **神铸**：当战力 <100k & 装备 >红装 时触发的超长说明" + "测试" * 80 + "\n"
        "- **狗粮**：同部位升阶材料" + "说明" * 80 + "\n"
        "- **双生**：合并符文" + "内容" * 80 + "\n",
        encoding="utf-8",
    )
    kb = GameKnowledgeBase(path=kb_file)
    block = kb.select([_msg("神铸狗粮双生")], max_items=5, max_chars=260)
    assert block
    assert len(block) <= 260
    assert "&lt;100k &amp; 装备 &gt;红装" in block

    # max_items 上限生效
    block_one = kb.select([_msg("神铸狗粮双生")], max_items=1, max_chars=1000)
    assert len([line for line in block_one.splitlines() if line.startswith("- ")]) == 1


def test_env_var_switches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    custom = tmp_path / "custom_kb.md"
    custom.write_text(
        "## 一、基础玩法与公会日常\n\n1. **秘境试炼**：测试自定义路径。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GAME_KNOWLEDGE_PATH", str(custom))
    monkeypatch.setenv("ENABLE_GAME_KNOWLEDGE", "1")
    kb = GameKnowledgeBase()
    assert "测试自定义路径" in kb.select([_msg("打秘境试炼")])

    monkeypatch.setenv("ENABLE_GAME_KNOWLEDGE", "0")
    assert kb.select([_msg("打秘境试炼")]) == ""


def test_reply_generator_injects_game_knowledge_only_when_matched(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_GAME_KNOWLEDGE", "1")
    gen = ReplyGenerator(
        llm_client=None,
        memory_engine=None,
        tool_registry=_REG,
        game_knowledge=GameKnowledgeBase(path=_REAL_KB_PATH),
    )

    # 命中游戏术语：注入 <game_knowledge>
    game_unread = [_msg("有人帮打魔值吗，爬塔坐牢了")]
    game_prompt = gen._build_user_prompt(game_unread, game_unread, is_group=True)
    assert "<game_knowledge>" in game_prompt
    assert "</game_knowledge>" in game_prompt
    assert "魔植" in game_prompt
    assert "通天塔" in game_prompt

    # 日常非游戏闲聊：零注入
    casual_unread = [_msg("今天中午吃红烧肉，顺便买张车票")]
    casual_prompt = gen._build_user_prompt(casual_unread, casual_unread, is_group=True)
    assert "<game_knowledge>" not in casual_prompt

    # 环境变量关闭时：即使命中术语也不注入
    monkeypatch.setenv("ENABLE_GAME_KNOWLEDGE", "0")
    disabled_prompt = gen._build_user_prompt(game_unread, game_unread, is_group=True)
    assert "<game_knowledge>" not in disabled_prompt


def test_per_group_config_private_chat_and_hit_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    import json
    import logging
    from src.bot.group_config import GroupConfig

    monkeypatch.setenv("ENABLE_GAME_KNOWLEDGE", "1")
    cfg_path = tmp_path / "group_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "groups": {
                    "弓箭2战神公会": {"game_knowledge": True},
                    "戰神管理群": {"game_knowledge": False},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    gen = ReplyGenerator(
        llm_client=None,
        memory_engine=None,
        tool_registry=_REG,
        group_config=GroupConfig(cfg_path),
        game_knowledge=GameKnowledgeBase(path=_REAL_KB_PATH),
    )
    unread_enabled = [_msg("神铸1级要多少材料", chat_name="弓箭2战神公会")]
    with caplog.at_level(logging.INFO, logger="src.reply.generator"):
        enabled_prompt = gen._build_user_prompt(
            unread_enabled, unread_enabled, is_group=True
        )
    assert "<game_knowledge>" in enabled_prompt
    assert any(
        "[GameKnowledge]" in rec.message and "弓箭2战神公会" in rec.message and "神铸" in rec.message
        for rec in caplog.records
    )

    # 关闭该群的 game_knowledge 后不注入
    unread_disabled = [_msg("神铸1级要多少材料", chat_name="戰神管理群")]
    disabled_group_prompt = gen._build_user_prompt(
        unread_disabled, unread_disabled, is_group=True
    )
    assert "<game_knowledge>" not in disabled_group_prompt

    # 私聊不注入游戏知识库
    unread_private = [_msg("神铸1级要多少材料", chat_name="Alice")]
    private_prompt = gen._build_user_prompt(
        unread_private, unread_private, is_group=False
    )
    assert "<game_knowledge>" not in private_prompt


def test_xml_entity_safe_truncation_system_filter_and_no_typo_alias(tmp_path: Path):
    import xml.etree.ElementTree as ET

    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "## 二、高频黑话与核心概念\n\n"
        "- **神铸**：" + "A&B<C>" * 40 + "\n",
        encoding="utf-8",
    )
    kb = GameKnowledgeBase(path=kb_file)
    # 截断点落在含大量 & / < / > 的文本上时，必须是合法 XML
    for budget in (150, 151, 152, 153, 154, 155, 160):
        block = kb.select([_msg("神铸")], max_items=1, max_chars=budget)
        assert block
        assert len(block) <= budget
        ET.fromstring(block)  # 非法实体会抛出 ParseError

    # recent_messages 中的系统消息不应触发知识库
    sys_msg = ChatMessage(
        text="魔植提醒：记得打魔植",
        sender="系统",
        sender_type=SenderType.SYSTEM,
        chat_name="弓箭2战神公会",
    )
    real_kb = GameKnowledgeBase(path=_REAL_KB_PATH)
    assert real_kb.select([_msg("早安")], [sys_msg]) == ""

    # 已移除临时笔误别名「神华」
    assert real_kb.select([_msg("神华集团今天股价怎么样")]) == ""


def test_guild_slang_aliases_and_no_guess_boundary(tmp_path: Path):
    """验证公会群新增黑话别名命中，且 <note> 明确禁止望文生义猜词。"""
    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "## 一、基础玩法与公会日常\n\n"
        "1. **公会飞船**：喊低战有核心/发工会了是指去抢劫低战船拿核心，不是让低战领福利。\n"
        "2. **魔植**：2 人防守副本。\n"
        "3. **协助**：帮打或带推。\n\n"
        "## 二、高频黑话与核心概念\n\n"
        "1. **三套 S 装**：神谕、格里芬、龙骑三套装备，支持混搭和置换宝珠。\n"
        "2. **出金 / 歪了**：抽卡或附魔出高品质或歪掉。\n",
        encoding="utf-8",
    )
    kb = GameKnowledgeBase(path=kb_file)

    cases = [
        ("快，低战有核心，发工会了", "公会飞船"),
        ("好几天没看见大船了，明天刷核心船", "公会飞船"),
        ("自己偏偏没有抢劫次数", "公会飞船"),
        ("3混是3混 就是没混对", "三套 S 装"),
        ("一直买打折置换宝珠", "三套 S 装"),
        ("怎么你们深夜附魔都出红", "出金 / 歪了"),
        ("红歪了一个 浪费了", "出金 / 歪了"),
        ("有没有滴滴带推 累死我了", "协助"),
        ("喊宝哥带刷魔植 六票扫荡", "魔植"),
    ]
    for utterance, expected_title in cases:
        block, titles = kb.select_with_matches([_msg(utterance)])
        assert expected_title in titles, f"{utterance!r} 未命中 {expected_title!r}, 实际={titles}"
        assert "严禁望文生义" in block
        # 确认所有提取的关键词长度均 >= 2
        for entry in parse_game_knowledge(kb_file.read_text(encoding="utf-8")):
            assert all(len(kw) >= 2 for kw in entry.keywords)
