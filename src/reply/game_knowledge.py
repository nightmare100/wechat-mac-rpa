"""《弓箭传说2》关键词按需游戏知识注入。

从共享知识库（默认 `data/game_knowledge.md`）解析基础玩法、高频黑话与品阶对照，
仅在当前未读或最近对话命中游戏术语时注入精简 `<game_knowledge>` 块；
普通日常闲聊返回空字符串（零额外 token）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from html import escape as xml_escape
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

_logger = logging.getLogger("src.reply.game_knowledge")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_REL_PATH = Path("data") / "game_knowledge.md"

_BOUNDARY_NOTE = (
    "仅供参考，用来听懂术语和基础概念；不要主动科普，"
    "涉及配装攻略/强度排行仍按人设推给对方或说不太清楚。"
)

_INCLUDED_H2_MARKERS = ("基础玩法", "公会日常", "高频黑话", "核心概念")
_RARITY_H3_MARKERS = ("品阶", "边框颜色")

_ENTRY_LINE_RE = re.compile(
    r"^\s*(?:\d+\.|[-*•])\s*\*\*(.+?)\*\*\s*[：:]\s*(.+?)\s*$"
)
_BULLET_LINE_RE = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
_TITLE_SPLIT_RE = re.compile(r"[/（）()、，,]")
_ASCII_CJK_SPACE_RE = re.compile(
    r"(?<=[a-z0-9])\s+(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])\s+(?=[a-z0-9])"
)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# 过滤过宽的元标题词或日常高频词，避免非游戏闲聊误触发
_TITLE_STOP_TOKENS = {
    "常见流派词",
    "每日",
    "三套",
    "普通",
    "精良",
    "稀有",
    "困难",
    "祝福",
}

# 针对已知条目的安全别名、常见笔误及复合触发词（严禁单字通用触发词）
_EXTRA_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("魔植", ("魔值",)),
    ("每日公会日常", ("公会日常", "魔物得分", "打魔物")),
    ("钓鱼", ("鱼饵",)),
    ("公会飞船", ("飞船", "发船", "上船", "捐票", "几票", "船长", "舱位")),
    ("通天塔", ("爬塔",)),
    ("主线关卡", ("主线",)),
    ("困难关卡", ("困难关",)),
    ("封印之战", ("封印", "英杰", "火龙")),
    ("竞技场", ("pvp",)),
    ("公会远征", ("远征", "远征币")),
    ("扫荡", ("扫荡票",)),
    ("三套", ("s装", "三套s装", "s级装备", "s套", "神域")),
    ("龙骑", ("s装", "三套s装", "s级装备", "s套", "神域")),
    ("铭刻", ("符文",)),
    ("圣物", ("红圣", "红圣物", "金圣物")),
    ("神眷者", ("神眷", "镰刀")),
    ("出金", ("小保底", "抽卡", "开箱")),
    ("换积分", ("兑积分",)),
    (
        "拉满",
        (
            "爆肝",
            "太肝",
            "好肝",
            "很肝",
            "肝不动",
            "肝帝",
            "太卷",
            "好卷",
            "很卷",
            "内卷",
            "卷王",
            "卷不动",
        ),
    ),
    ("协助", ("接协助",)),
)

_RARITY_KEYWORDS: Tuple[str, ...] = (
    "混沌",
    "神话",
    "传说",
    "史诗",
    "红装",
    "金装",
    "橙装",
    "紫装",
    "s装",
    "s级装备",
    "品阶",
    "边框颜色",
)


@dataclass(frozen=True)
class KnowledgeEntry:
    title: str
    description: str
    keywords: Tuple[str, ...]


def _normalize_text(text: str) -> str:
    return _ASCII_CJK_SPACE_RE.sub("", (text or "").strip().lower())


def _is_enabled_env() -> bool:
    return os.environ.get("ENABLE_GAME_KNOWLEDGE", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _resolve_knowledge_path(path: Optional[Union[str, Path]] = None) -> Path:
    if path is None:
        raw = os.environ.get("GAME_KNOWLEDGE_PATH", "").strip()
        p = Path(raw) if raw else _DEFAULT_REL_PATH
    else:
        p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _extract_keywords(title: str) -> Tuple[str, ...]:
    """从条目标题提取干净关键词，过滤单字和泛化词，并补充安全别名/笔误。"""
    seen: set[str] = set()
    keywords: List[str] = []

    def _add(token: str) -> None:
        norm = _normalize_text(token)
        # 严禁单字通用触发词（如 红/金/肝/卷/票）
        if len(norm) < 2 or norm in _TITLE_STOP_TOKENS or norm in seen:
            return
        seen.add(norm)
        keywords.append(norm)

    for part in _TITLE_SPLIT_RE.split(title):
        cleaned = part.strip()
        if not cleaned:
            continue
        _add(cleaned)

    norm_title = _normalize_text(title)
    for marker, aliases in _EXTRA_ALIASES:
        if _normalize_text(marker) in norm_title:
            for alias in aliases:
                _add(alias)

    return tuple(keywords)


def parse_game_knowledge(markdown_text: str) -> List[KnowledgeEntry]:
    """解析共享游戏知识库 markdown，提取一/二节条目与三.1品阶对照，并按标题去重。"""
    if not markdown_text or not markdown_text.strip():
        return []

    entries_by_title: dict[str, KnowledgeEntry] = {}
    title_order: List[str] = []

    in_included_h2 = False
    in_rarity_h3 = False
    rarity_lines: List[str] = []

    def _upsert(title: str, description: str, keywords: Tuple[str, ...]) -> None:
        clean_title = title.strip()
        clean_desc = description.strip()
        if not clean_title or not clean_desc:
            return
        key = _normalize_text(clean_title)
        if key in entries_by_title:
            existing = entries_by_title[key]
            merged_kw = tuple(dict.fromkeys((*existing.keywords, *keywords)))
            entries_by_title[key] = KnowledgeEntry(
                title=existing.title,
                description=existing.description,
                keywords=merged_kw,
            )
            return
        entries_by_title[key] = KnowledgeEntry(
            title=clean_title,
            description=clean_desc,
            keywords=keywords,
        )
        title_order.append(key)

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("## "):
            heading = line[3:].strip()
            in_included_h2 = any(marker in heading for marker in _INCLUDED_H2_MARKERS)
            in_rarity_h3 = False
            continue

        if line.startswith("### "):
            sub_heading = line[4:].strip()
            in_rarity_h3 = any(marker in sub_heading for marker in _RARITY_H3_MARKERS)
            continue

        if in_included_h2:
            m = _ENTRY_LINE_RE.match(line)
            if m:
                title, desc = m.group(1).strip(), m.group(2).strip()
                _upsert(title, desc, _extract_keywords(title))
            continue

        if in_rarity_h3:
            bm = _BULLET_LINE_RE.match(line)
            if bm:
                cleaned = _BOLD_RE.sub(r"\1", bm.group(1)).strip()
                if cleaned:
                    rarity_lines.append(cleaned)

    if rarity_lines:
        _upsert(
            "品阶与边框颜色",
            "；".join(rarity_lines),
            _RARITY_KEYWORDS,
        )

    return [entries_by_title[k] for k in title_order]


def _keyword_matches(keyword: str, normalized_text: str) -> bool:
    if not keyword or not normalized_text:
        return False
    if keyword.isascii():
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])",
                normalized_text,
            )
        )
    if keyword[0].isascii() and keyword[0].isalnum():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(keyword)}", normalized_text))
    return keyword in normalized_text


def _is_system_message(msg: Any) -> bool:
    sender_type = getattr(msg, "sender_type", None)
    if sender_type is None:
        return False
    val = getattr(sender_type, "value", sender_type)
    return str(val).lower() == "system"


def _truncate_xml_escaped(raw_text: str, max_escaped_len: int) -> str:
    """按原文安全截断后再做 XML 转义，避免把 `&amp;`/`&lt;` 实体截断成半截。"""
    if max_escaped_len <= 0 or not raw_text:
        return ""
    escaped = xml_escape(raw_text, quote=False)
    if len(escaped) <= max_escaped_len:
        return escaped
    lo, hi = 0, len(raw_text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = xml_escape(raw_text[:mid], quote=False)
        if len(candidate) <= max_escaped_len:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _extract_message_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    parts: List[str] = []
    for attr in ("text", "image_description", "image_text", "quoted_text"):
        val = getattr(msg, attr, None)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return " ".join(parts)


class GameKnowledgeBase:
    """按关键词从本地游戏知识库筛选并渲染 `<game_knowledge>` 块。"""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        *,
        enabled: Optional[bool] = None,
    ) -> None:
        self._path_override = path
        self._enabled_override = enabled
        self._cached_path: Optional[Path] = None
        self._cached_stat: Optional[Tuple[int, int]] = None
        self._entries: List[KnowledgeEntry] = []

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return _is_enabled_env()

    @property
    def path(self) -> Path:
        return _resolve_knowledge_path(self._path_override)

    def _reload_if_needed(self) -> List[KnowledgeEntry]:
        target = self.path
        try:
            st = target.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._cached_path = target
            self._cached_stat = None
            self._entries = []
            return self._entries

        if target == self._cached_path and sig == self._cached_stat:
            return self._entries

        try:
            content = target.read_text(encoding="utf-8")
            self._entries = parse_game_knowledge(content)
            self._cached_path = target
            self._cached_stat = sig
        except OSError as e:
            _logger.warning("[GameKnowledge] 读取失败，降级为空: %s", e)
            self._cached_path = target
            self._cached_stat = None
            self._entries = []
        return self._entries

    def select_with_matches(
        self,
        unread_messages: Sequence[Any],
        recent_messages: Optional[Sequence[Any]] = None,
        *,
        max_items: int = 5,
        max_chars: int = 700,
        ignore_enabled: bool = False,
    ) -> Tuple[str, List[str]]:
        """匹配未读与最近 3 条非系统历史消息中的游戏术语，返回 (`<game_knowledge>` 块, 命中标题列表)。"""
        if (not ignore_enabled and not self.enabled) or max_items <= 0 or max_chars <= 0:
            return "", []

        entries = self._reload_if_needed()
        if not entries:
            return "", []

        unread_text = _normalize_text(
            "\n".join(_extract_message_text(m) for m in (unread_messages or ()))
        )
        non_system_recent = [m for m in (recent_messages or ()) if not _is_system_message(m)]
        recent_slice = non_system_recent[-3:]
        recent_text = _normalize_text(
            "\n".join(_extract_message_text(m) for m in recent_slice)
        )
        if not unread_text and not recent_text:
            return "", []

        scored: List[Tuple[int, int, int, int, KnowledgeEntry]] = []
        for idx, entry in enumerate(entries):
            if not entry.keywords:
                continue
            unread_hits = (
                sum(1 for kw in entry.keywords if _keyword_matches(kw, unread_text))
                if unread_text
                else 0
            )
            recent_hits = (
                sum(1 for kw in entry.keywords if _keyword_matches(kw, recent_text))
                if recent_text
                else 0
            )
            if unread_hits == 0 and recent_hits == 0:
                continue
            scored.append((1 if unread_hits > 0 else 0, unread_hits, recent_hits, idx, entry))

        if not scored:
            return "", []

        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        selected = [item[4] for item in scored[:max_items]]

        header = f"<game_knowledge>\n<note>{xml_escape(_BOUNDARY_NOTE, quote=False)}</note>\n"
        footer = "\n</game_knowledge>"
        remaining = max_chars - len(header) - len(footer)
        if remaining <= 0:
            return "", []

        lines: List[str] = []
        matched_titles: List[str] = []
        used = 0
        for entry in selected:
            safe_title = xml_escape(entry.title, quote=False)
            safe_desc = xml_escape(entry.description, quote=False)
            line = f"- {safe_title}：{safe_desc}"
            sep = 1 if lines else 0
            if used + sep + len(line) <= remaining:
                lines.append(line)
                matched_titles.append(entry.title)
                used += sep + len(line)
            elif not lines:
                prefix = f"- {safe_title}："
                if len(prefix) + 2 <= remaining:
                    avail = remaining - len(prefix) - 1
                    truncated = _truncate_xml_escaped(entry.description, avail)
                    if truncated:
                        lines.append(prefix + truncated + "…")
                        matched_titles.append(entry.title)
                break
            else:
                break

        if not lines:
            return "", []
        return header + "\n".join(lines) + footer, matched_titles

    def select(
        self,
        unread_messages: Sequence[Any],
        recent_messages: Optional[Sequence[Any]] = None,
        *,
        max_items: int = 5,
        max_chars: int = 700,
    ) -> str:
        """匹配未读与最近 3 条非系统历史消息中的游戏术语，返回 `<game_knowledge>` XML 块或空串。"""
        block, _ = self.select_with_matches(
            unread_messages,
            recent_messages,
            max_items=max_items,
            max_chars=max_chars,
        )
        return block


__all__ = ["GameKnowledgeBase", "KnowledgeEntry", "parse_game_knowledge"]
