"""按群覆写功能开关：未在配置文件中声明的字段继承 .env 全局默认值。

默认读取 `data/group_config.json`（可通过环境变量 `GROUP_CONFIG_PATH` 指定）。
文件不存在时全部回退到调用方传入的全局默认值；文件更新后按 mtime 自动热加载。
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

_logger = logging.getLogger("src.bot.group_config")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_UNREAD_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")


def normalize_group_key(chat_name: str) -> str:
    """剥掉群名末尾未读数后缀（如 ' (45)'）并做 NFKC 与空白归一化。"""
    if not chat_name:
        return ""
    cleaned = _UNREAD_SUFFIX_RE.sub("", chat_name).strip()
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", cleaned)).strip()


class GroupConfig:
    """按群读取功能开关与可选覆盖项（支持热加载）。"""

    def __init__(self, path: Optional[Path | str] = None):
        if path is None:
            configured = os.environ.get("GROUP_CONFIG_PATH", "data/group_config.json").strip()
            resolved = Path(configured)
            if not resolved.is_absolute():
                resolved = _PROJECT_ROOT / resolved
            self.path = resolved
        else:
            self.path = Path(path)
        self._mtime_ns: int = -1
        self._defaults: Dict[str, Any] = {}
        self._groups: Dict[str, Dict[str, Any]] = {}

    def _reload_if_needed(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            if self._mtime_ns != -1:
                self._mtime_ns = -1
                self._defaults = {}
                self._groups = {}
            return
        if mtime_ns == self._mtime_ns:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层必须是 JSON 对象")
            defaults = raw.get("defaults") or {}
            groups_raw = raw.get("groups") or {}
            if not isinstance(defaults, dict) or not isinstance(groups_raw, dict):
                raise ValueError("defaults 和 groups 必须是 JSON 对象")
            normalized_groups: Dict[str, Dict[str, Any]] = {}
            for name, cfg in groups_raw.items():
                if not isinstance(name, str) or not isinstance(cfg, dict):
                    continue
                key = normalize_group_key(name)
                if key:
                    normalized_groups[key] = dict(cfg)
            self._defaults = dict(defaults)
            self._groups = normalized_groups
            self._mtime_ns = mtime_ns
        except Exception as exc:
            _logger.warning("[GroupConfig] 读取 %s 失败，沿用默认配置: %s", self.path, exc)
            self._defaults = {}
            self._groups = {}
            self._mtime_ns = mtime_ns

    def get(self, chat_name: str, key: str, default: Any = None) -> Any:
        """按 groups[chat_name][key] → defaults[key] → default 顺序取值。"""
        self._reload_if_needed()
        norm = normalize_group_key(chat_name)
        if norm and norm in self._groups and key in self._groups[norm]:
            return self._groups[norm][key]
        if key in self._defaults:
            return self._defaults[key]
        return default

    def is_enabled(self, chat_name: str, key: str, default: bool = False) -> bool:
        """读取按群布尔开关；非布尔值回退 default。"""
        val = self.get(chat_name, key, default)
        return val if isinstance(val, bool) else default

    def any_group_enabled(self, key: str, default: bool = False) -> bool:
        """全局默认开启、defaults 开启、或任一配置群开启时返回 True（用于按需初始化子模块）。"""
        self._reload_if_needed()
        if isinstance(self._defaults.get(key), bool):
            effective_default = self._defaults[key]
        else:
            effective_default = default
        if effective_default:
            return True
        return any(cfg.get(key) is True for cfg in self._groups.values())
