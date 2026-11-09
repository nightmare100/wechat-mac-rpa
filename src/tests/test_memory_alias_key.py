"""别名匹配用统一后的名字：去 [待验证]、去符号；占位名（未知等）不进记忆。"""

import pytest

from src.memory.engine import MemoryEngine, _name_key


@pytest.fixture
def engine(tmp_path):
    engine = MemoryEngine()
    engine.wiki_dir = tmp_path
    (tmp_path / "users").mkdir(parents=True, exist_ok=True)
    (tmp_path / "groups").mkdir(parents=True, exist_ok=True)
    engine._facts = {}
    engine._corrections = {}
    engine._entity_corrections = {}
    engine._aliases = {"Mr乂朧月": ["别名", "Mrㄨ朧月[待验证]", "Mr乇朧月[待验证]"]}
    return engine


def test_name_key():
    assert _name_key("Mrㄨ朧月[待验证]") == _name_key("Mrㄨ朧月")
    assert _name_key("『宵桜』囡囡") == _name_key("宵桜囡囡")
    assert _name_key("★ 小明 ·") == "小明"
    assert _name_key("Paul") == _name_key("paul")
    assert _name_key("Mr乂朧月") != _name_key("Mrㄨ朧月")  # 不同的字仍是不同的名字


def test_unverified_alias_resolves_to_main(engine):
    assert engine._resolve_alias("Mrㄨ朧月") == "Mr乂朧月"
    assert engine._resolve_alias("Mr乇朧月") == "Mr乂朧月"
    assert engine._resolve_alias("Mr乂朧月") == "Mr乂朧月"
    assert engine._resolve_alias("相缺") == "相缺"


def test_user_memory_merges_variant_files(engine):
    users = engine.wiki_dir / "users"
    (users / "Mr乂朧月.md").write_text("主档内容", encoding="utf-8")
    (users / "Mrㄨ朧月.md").write_text("变体一内容", encoding="utf-8")
    (users / "Mr乇朧月.md").write_text("变体二内容", encoding="utf-8")
    text = engine.get_user_memory("Mrㄨ朧月")
    assert "主档内容" in text and "变体一内容" in text and "变体二内容" in text


@pytest.mark.parametrize("name", ["未知", "未知昵称", "匿名", "对方", "我", "自己", "Bot", "『』", "★ ·"])
def test_placeholder_main_name_rejected(engine, name):
    assert not engine._is_valid_main_name(name)
    engine.update_user_wiki(name, "群", [], [])
    assert engine._update_queue == []


def test_real_name_still_valid(engine):
    assert engine._is_valid_main_name("破军、斩月")
    assert engine._is_valid_main_name("Mr乂朧月")


def test_alias_label_rejected(engine):
    wiki = "# 甲\n\n## 别名\n- 别名\n- 真别名\n\n## 基本信息\n- x\n"
    assert engine._extract_aliases_from_user_wiki(wiki, "甲") == ["真别名"]
