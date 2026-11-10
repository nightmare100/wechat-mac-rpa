"""bot 和朧月的昵称，从 .env 读取（见 .env.example），persona 和代码共用。"""

import os
import re

_PLACEHOLDER_RE = re.compile(r"\{\{(BOT_NAME|BUDDY_NAME)\}\}")


def render_persona(text: str) -> str:
    """替换 persona 里的 {{BOT_NAME}} / {{BUDDY_NAME}}；未配置的保留原样。不用 str.format，persona 里有 JSON 花括号。"""
    return _PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), "").strip() or m.group(0), text)


def unresolved_persona_vars(text: str) -> list:
    """替换后仍残留的占位符对应的环境变量名（去重、按出现顺序）。"""
    return list(dict.fromkeys(_PLACEHOLDER_RE.findall(text)))


def is_at_bot(text: str) -> bool:
    """消息文字里有「@…BOT_AT_KEYWORD」。@ 和关键词之间允许最多 6 个字符，昵称前缀常被识别错。"""
    keyword = os.environ.get("BOT_AT_KEYWORD", "").strip()
    return bool(keyword) and re.search(r"@\S{0,6}" + re.escape(keyword), text or "") is not None


def is_buddy(sender: str) -> bool:
    """发送者昵称含 BUDDY_MATCH 里任一写法（逗号分隔）。"""
    variants = [v.strip() for v in os.environ.get("BUDDY_MATCH", "").split(",") if v.strip()]
    return any(v in (sender or "") for v in variants)
