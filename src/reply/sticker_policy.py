"""群聊回复表情包策略：纯逻辑，不碰微信。设计见 docs/superpowers/specs/2026-11-22-sticker-reply-design.md。

LLM 生成回复时可额外输出 {"sticker": {"tag": "谢谢", "mode": "append|replace"}}；
本模块从 data/stickers/<tag>/ 动态扫描图片，并按以下硬规则决定是否发送：
- 本轮已发语音（voiced=True）时互斥不发；
- replace 模式仅限单条且去 @ 后正文 ≤ replace_max_chars 字，否则自动降级为 append；
- 按群独立冷却（回复条数 >= cooldown_replies 且时间间隔 >= cooldown_minutes）；
- 按 append/replace 各自概率抽签；
- 同群最近 recent_no_repeat 张内尽量不重复发送同一张图。
"""

from __future__ import annotations

import json
import logging
import random
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

_logger = logging.getLogger("src.sticker_policy")

PROJECT_ROOT = Path(__file__).parent.parent.parent
STICKER_REPLY_CONFIG_PATH = PROJECT_ROOT / "data" / "sticker_reply.json"
IMAGE_EXTENSIONS = frozenset({".gif", ".png", ".jpg", ".jpeg", ".webp"})
VALID_MODES = frozenset({"append", "replace"})
_MENTION_RE = re.compile(r"^(?:@[^\s\u2005]+[\s\u2005]+)+")


@dataclass(frozen=True)
class StickerReplyConfig:
    dry_run: bool
    stickers_dir: Path
    append_probability: float
    replace_probability: float
    cooldown_replies: int
    cooldown_minutes: float
    recent_no_repeat: int
    replace_max_chars: int


DEFAULTS = StickerReplyConfig(
    dry_run=True,
    stickers_dir=PROJECT_ROOT / "data" / "stickers",
    append_probability=0.25,
    replace_probability=0.15,
    cooldown_replies=3,
    cooldown_minutes=3.0,
    recent_no_repeat=5,
    replace_max_chars=8,
)


@dataclass(frozen=True)
class StickerDecision:
    send_sticker: bool
    mode: str = ""
    image_path: Optional[Path] = None
    tag: str = ""
    reason: str = ""


def _prob(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _non_neg_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _non_neg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_sticker_reply_config(path: Path = STICKER_REPLY_CONFIG_PATH) -> Optional[StickerReplyConfig]:
    """不存在或 enabled 为假返回 None；单项非法记警告并回退默认值。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("顶层必须是 JSON 对象")
    except (OSError, ValueError) as exc:
        _logger.error("[StickerReply] 配置读取失败，不启用: %s", exc)
        return None
    if not data.get("enabled"):
        return None

    def bad(name: str) -> None:
        _logger.warning("[StickerReply] 配置项 %s 不合法，使用默认值", name)

    d = DEFAULTS
    dry_run = data.get("dry_run", d.dry_run)
    if not isinstance(dry_run, bool):
        bad("dry_run")
        dry_run = d.dry_run

    raw_dir = data.get("stickers_dir", "data/stickers")
    if isinstance(raw_dir, str) and raw_dir.strip():
        stickers_dir = Path(raw_dir.strip())
        if not stickers_dir.is_absolute():
            stickers_dir = PROJECT_ROOT / stickers_dir
    else:
        bad("stickers_dir")
        stickers_dir = d.stickers_dir

    append_p = data.get("append_probability", d.append_probability)
    if not _prob(append_p):
        bad("append_probability")
        append_p = d.append_probability

    replace_p = data.get("replace_probability", d.replace_probability)
    if not _prob(replace_p):
        bad("replace_probability")
        replace_p = d.replace_probability

    cooldown_replies = data.get("cooldown_replies", d.cooldown_replies)
    if not _non_neg_int(cooldown_replies):
        bad("cooldown_replies")
        cooldown_replies = d.cooldown_replies

    cooldown_minutes = data.get("cooldown_minutes", d.cooldown_minutes)
    if not _non_neg_num(cooldown_minutes):
        bad("cooldown_minutes")
        cooldown_minutes = d.cooldown_minutes

    recent_no_repeat = data.get("recent_no_repeat", d.recent_no_repeat)
    if not _non_neg_int(recent_no_repeat):
        bad("recent_no_repeat")
        recent_no_repeat = d.recent_no_repeat

    replace_max_chars = data.get("replace_max_chars", d.replace_max_chars)
    if not (isinstance(replace_max_chars, int) and not isinstance(replace_max_chars, bool) and replace_max_chars > 0):
        bad("replace_max_chars")
        replace_max_chars = d.replace_max_chars

    return StickerReplyConfig(
        dry_run=dry_run,
        stickers_dir=stickers_dir,
        append_probability=float(append_p),
        replace_probability=float(replace_p),
        cooldown_replies=cooldown_replies,
        cooldown_minutes=float(cooldown_minutes),
        recent_no_repeat=recent_no_repeat,
        replace_max_chars=replace_max_chars,
    )


def parse_sticker_meta(raw) -> Optional[dict]:
    """解析 LLM 输出的 sticker 字段：{"tag": "...", "mode": "append|replace"}。"""
    if not isinstance(raw, dict):
        return None
    tag = raw.get("tag")
    if not isinstance(tag, str) or not tag.strip():
        return None
    mode = raw.get("mode", "append")
    if not isinstance(mode, str) or mode.strip() not in VALID_MODES:
        mode = "append"
    return {"tag": tag.strip(), "mode": mode.strip()}


def render_sticker_prompt(template: str, tags: List[str]) -> str:
    """将当前可用表情标签填入提示词模板；无可用标签时返回空串。"""
    cleaned = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
    if not template or not cleaned:
        return ""
    return template.replace("{{STICKER_TAGS}}", "、".join(cleaned))


def _strip_leading_mention(text: str) -> str:
    return _MENTION_RE.sub("", (text or "").strip()).strip()


class StickerPolicy:
    """按群管理表情包目录扫描、冷却、概率与最近去重。"""

    def __init__(self, config: StickerReplyConfig, rng: Callable[[], float] = random.random):
        self.config = config
        self._rng = rng
        self._replies_since_last: Dict[str, int] = {}
        self._last_sent_at: Dict[str, datetime] = {}
        self._recent_sent: Dict[str, Deque[str]] = {}

    def list_images(self, tag: str) -> List[Path]:
        if not tag or "/" in tag or "\\" in tag or tag.startswith("."):
            return []
        tag_dir = self.config.stickers_dir / tag
        if not tag_dir.is_dir():
            return []
        return sorted(
            p for p in tag_dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTENSIONS
        )

    def available_tags(self) -> List[str]:
        root = self.config.stickers_dir
        if not root.is_dir():
            return []
        tags = []
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.name.startswith(".") and self.list_images(entry.name):
                tags.append(entry.name)
        return tags

    def _pick_image(self, chat_name: str, images: List[Path]) -> Path:
        recent = list(self._recent_sent.get(chat_name, ()))
        recent_set = set(recent)
        fresh = [img for img in images if str(img) not in recent_set]
        if fresh:
            pool = fresh
        elif len(images) > 1 and recent:
            last_one = recent[-1]
            pool = [img for img in images if str(img) != last_one] or images
        else:
            pool = images
        idx = min(len(pool) - 1, int(self._rng() * len(pool)))
        return pool[idx]

    def decide(
        self,
        chat_name: str,
        replies: List[str],
        sticker_meta: Optional[dict],
        voiced: bool = False,
        now: Optional[datetime] = None,
    ) -> StickerDecision:
        if voiced:
            return StickerDecision(False, reason="本轮已发语音")
        if not sticker_meta or not isinstance(sticker_meta, dict):
            return StickerDecision(False, reason="未标注表情")
        tag = str(sticker_meta.get("tag") or "").strip()
        mode = str(sticker_meta.get("mode") or "append").strip()
        if mode not in VALID_MODES:
            mode = "append"
        images = self.list_images(tag)
        if not images:
            return StickerDecision(False, tag=tag, reason="标签无可用素材")

        now = now or datetime.now()
        last_at = self._last_sent_at.get(chat_name)
        if last_at is not None:
            replies_gap = self._replies_since_last.get(chat_name, 0)
            if replies_gap < self.config.cooldown_replies:
                return StickerDecision(False, tag=tag, reason=f"回复条数冷却({replies_gap}/{self.config.cooldown_replies})")
            if now - last_at < timedelta(minutes=self.config.cooldown_minutes):
                return StickerDecision(False, tag=tag, reason="时间冷却中")

        # replace 仅允许单条且去 @ 后正文很短的回复，防止误替换有信息量的回答
        if mode == "replace":
            body = _strip_leading_mention(replies[0]) if len(replies) == 1 else ""
            if len(replies) != 1 or not body or len(body) > self.config.replace_max_chars:
                mode = "append"

        prob = self.config.replace_probability if mode == "replace" else self.config.append_probability
        if prob <= 0 or self._rng() >= prob:
            return StickerDecision(False, mode=mode, tag=tag, reason="概率未命中")

        chosen = self._pick_image(chat_name, images)
        return StickerDecision(True, mode=mode, image_path=chosen, tag=tag, reason="命中发送")

    def record_sent(
        self,
        chat_name: str,
        image_path: Optional[Path] = None,
        sent: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        if sent and image_path is not None:
            self._replies_since_last[chat_name] = 0
            self._last_sent_at[chat_name] = now or datetime.now()
            if self.config.recent_no_repeat > 0:
                buf = self._recent_sent.setdefault(chat_name, deque(maxlen=self.config.recent_no_repeat))
                buf.append(str(image_path))
        else:
            if chat_name in self._last_sent_at:
                self._replies_since_last[chat_name] = self._replies_since_last.get(chat_name, 0) + 1
