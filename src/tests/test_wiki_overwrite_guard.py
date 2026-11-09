"""wiki 覆盖保护：覆盖前备份旧版（保留最近 5 份）；新版明显变短时不覆盖。"""

from unittest.mock import Mock

import pytest

from src.memory.engine import MemoryEngine


@pytest.fixture
def engine(tmp_path):
    e = MemoryEngine()
    e.wiki_dir = tmp_path
    (tmp_path / "users").mkdir()
    (tmp_path / "groups").mkdir()
    e._facts, e._corrections, e._entity_corrections, e._aliases = {}, {}, {}, {}
    e.llm_client = Mock()
    return e


def _wiki(n: int) -> str:
    return "# 甲\n\n## 近期动态\n" + "\n".join(f"- 2026-11-0{i % 9 + 1} 事件{i:03d}" for i in range(n))


def _history(engine, kind="users", name="甲"):
    d = engine.wiki_dir / "history" / kind / name
    return sorted(d.glob("*.md")) if d.exists() else []


def test_new_wiki_saved_without_backup(engine):
    path = engine.wiki_dir / "users" / "甲.md"
    engine.llm_client.chat.return_value = _wiki(10)
    assert engine._try_generate_wiki("p", path)
    assert path.read_text(encoding="utf-8") == _wiki(10)
    assert _history(engine) == []


def test_overwrite_backs_up_old_version(engine):
    path = engine.wiki_dir / "users" / "甲.md"
    path.write_text(_wiki(40), encoding="utf-8")
    engine.llm_client.chat.return_value = _wiki(45)
    assert engine._try_generate_wiki("p", path)
    assert path.read_text(encoding="utf-8") == _wiki(45)
    backups = _history(engine)
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == _wiki(40)


def test_shrunk_output_rejected(engine):
    path = engine.wiki_dir / "users" / "甲.md"
    old = _wiki(60)
    assert len(old) >= 500
    path.write_text(old, encoding="utf-8")
    engine.llm_client.chat.return_value = _wiki(10)
    assert engine._try_generate_wiki("p", path) == ""
    assert path.read_text(encoding="utf-8") == old
    rejected = [p for p in _history(engine) if p.name.endswith(".rejected.md")]
    assert len(rejected) == 1 and rejected[0].read_text(encoding="utf-8") == _wiki(10)


def test_small_old_wiki_may_shrink(engine):
    path = engine.wiki_dir / "users" / "甲.md"
    path.write_text(_wiki(20), encoding="utf-8")  # < 500 字
    engine.llm_client.chat.return_value = _wiki(5)
    assert engine._try_generate_wiki("p", path)
    assert path.read_text(encoding="utf-8") == _wiki(5)


def test_keep_latest_five_backups(engine):
    path = engine.wiki_dir / "groups" / "群.md"
    path.write_text(_wiki(40), encoding="utf-8")
    for i in range(7):
        engine.llm_client.chat.return_value = _wiki(41 + i)
        assert engine._try_generate_wiki("p", path, is_group=True)
    backups = _history(engine, "groups", "群")
    assert len(backups) == 5
    assert backups[-1].read_text(encoding="utf-8") == _wiki(46)
