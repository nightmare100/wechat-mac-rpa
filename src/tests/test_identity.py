"""bot / 朧月昵称从 .env 读取：persona 占位符替换、@ 我判断、朧月判断。"""

import pytest

from src.reply.generator import ReplyGenerator
from src.utils.identity import is_at_bot, is_buddy, render_persona, unresolved_persona_vars

ENV_KEYS = ("BOT_NAME", "BOT_AT_KEYWORD", "BUDDY_NAME", "BUDDY_MATCH")


@pytest.fixture
def no_identity_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_render_replaces_placeholders_and_keeps_json(no_identity_env):
    no_identity_env.setenv("BOT_NAME", "『宵桜』囡囡")
    no_identity_env.setenv("BUDDY_NAME", "朧月")
    text = '你是「{{BOT_NAME}}」，号在{{BUDDY_NAME}}那。输出 {"replies":[]}'
    assert render_persona(text) == '你是「『宵桜』囡囡」，号在朧月那。输出 {"replies":[]}'


def test_unresolved_lists_missing_env(no_identity_env):
    text = "{{BOT_NAME}} / {{BUDDY_NAME}}"
    assert unresolved_persona_vars(render_persona(text)) == ["BOT_NAME", "BUDDY_NAME"]
    no_identity_env.setenv("BOT_NAME", "囡囡")
    assert unresolved_persona_vars(render_persona(text)) == ["BUDDY_NAME"]


def test_is_at_bot_uses_keyword(no_identity_env):
    no_identity_env.setenv("BOT_AT_KEYWORD", "囡囡")
    assert is_at_bot("@『宵桜』囡囡 上了没")
    assert is_at_bot("@『宵桉』囡囡 好了没")
    assert not is_at_bot("@李桑 来打boss")
    assert not is_at_bot("@所有人 发船了")
    assert not is_at_bot("囡囡在吗")
    no_identity_env.setenv("BOT_AT_KEYWORD", "小美")
    assert is_at_bot("@小美 在吗")
    assert not is_at_bot("@『宵桜』囡囡 在吗")


def test_is_at_bot_false_without_keyword(no_identity_env):
    assert not is_at_bot("@『宵桜』囡囡 在吗")


def test_is_buddy_matches_any_variant(no_identity_env):
    no_identity_env.setenv("BUDDY_MATCH", "朧月, 胧月")
    for name in ["Mr丿朧月", "Mrゞ胧月", "Mr✕朧月"]:
        assert is_buddy(name)
    assert not is_buddy("李桑")
    assert not is_buddy("")


def test_is_buddy_false_without_config(no_identity_env):
    assert not is_buddy("Mr丿朧月")


def test_system_prompt_raises_when_placeholder_unresolved(no_identity_env, tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("你是「{{BOT_NAME}}」", encoding="utf-8")
    no_identity_env.setenv("PERSONA_PATH", str(persona))
    gen = ReplyGenerator(llm_client=None)
    with pytest.raises(RuntimeError, match="BOT_NAME"):
        gen._system_prompt()
    no_identity_env.setenv("BOT_NAME", "囡囡")
    assert gen._system_prompt() == "你是「囡囡」"


def test_mentions_bot_with_or_without_at(no_identity_env):
    from src.utils.identity import mentions_bot
    no_identity_env.setenv("BOT_AT_KEYWORD", "囡囡")
    assert mentions_bot("完蛋 囡囡不见了")
    assert mentions_bot("@『宵桜』囡囡 在吗")
    assert not mentions_bot("@李桑 来打boss")
    no_identity_env.delenv("BOT_AT_KEYWORD")
    assert not mentions_bot("囡囡呢")
