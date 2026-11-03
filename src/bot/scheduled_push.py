#!/usr/bin/env python3
"""定时推送：在配置的时间（每天或每周指定几天），向 SILENT_WHITELIST 里的群按顺序发送固定内容。

配置文件 data/scheduled_messages.json（修改后重启生效）：
    {
      "mention_all": true,
      "prefixes": ["『宵桜』囡囡播报", "『宵桜』囡囡提醒"],
      "schedules": [
        {"time": "08:00", "messages": ["早上好呀～"]},
        {"time": "21:30", "messages": ["今天也辛苦啦", "早点休息"], "weekdays": [1, 3, 5]}
      ]
    }

- weekdays 可选：1=周一 … 7=周日；不配或为空表示每天发送。

- prefixes 可选：每条消息发送时随机选一个前缀，拼成「前缀：消息」。
- mention_all 可选：为 true 时在最前面加「@所有人 」，拼成「@所有人 前缀：消息」。
- 每个时间点、每个群每天最多发一次。开始发送前先写入 data/scheduled_state.json，
  重启或中途异常都不会重复发送（宁可漏发，不重复发）。
- 超过时间点 GRACE_MINUTES 分钟仍未发出（如 bot 未运行），当天跳过，不补发。
- 打开群失败（标题校验不通过）则跳过该群，绝不发到其他会话。
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from src.models.base import ActionResult

_logger = logging.getLogger("src.scheduled_push")

PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "data" / "scheduled_messages.json"
STATE_PATH = PROJECT_ROOT / "data" / "scheduled_state.json"
GRACE_MINUTES = 10
SEND_RETRIES = 2


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _logger.error(f"[ScheduledPush] 配置读取失败 {path}: {e}")
        print(f"❌ 定时推送配置读取失败，本次运行不会定时推送: {path.name}: {e}")
        return {}


def load_prefixes(path: Path = CONFIG_PATH) -> list[str]:
    """读取 prefixes：每条消息随机拼接其中一个前缀，未配置则不拼接。"""
    return [str(p).strip() for p in _read_config(path).get("prefixes", []) if str(p).strip()]


Schedule = tuple[str, list[str], frozenset[int]]  # (HH:MM, 消息, 星期几；空集合=每天)
WEEKDAY_NAMES = "一二三四五六日"


def load_schedules(path: Path = CONFIG_PATH) -> list[Schedule]:
    """读取配置，返回 [(HH:MM, [消息...], {星期几})]。文件不存在返回空列表，非法条目跳过。"""
    data = _read_config(path)
    schedules = []
    for item in data.get("schedules", []):
        at = str(item.get("time", "")).strip()
        messages = [str(m) for m in item.get("messages", []) if str(m).strip()]
        try:
            datetime.strptime(at, "%H:%M")
        except ValueError:
            _logger.warning(f"[ScheduledPush] 时间格式错误，需为 HH:MM，已跳过: {item}")
            continue
        if not messages:
            _logger.warning(f"[ScheduledPush] 没有消息内容，已跳过: {item}")
            continue
        weekdays = item.get("weekdays") or []
        if not isinstance(weekdays, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in weekdays):
            _logger.warning(f"[ScheduledPush] weekdays 需为 1～7 的数字列表（1=周一），已跳过: {item}")
            continue
        schedules.append((at, messages, frozenset(weekdays)))
    return schedules


def due_schedules(schedules: list[Schedule], now: datetime) -> list[Schedule]:
    """返回今天需要发送、且当前处于发送窗口 [时间点, 时间点 + GRACE_MINUTES] 内的条目。"""
    due = []
    for at, messages, weekdays in schedules:
        if weekdays and now.isoweekday() not in weekdays:
            continue
        hour, minute = map(int, at.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= now <= start + timedelta(minutes=GRACE_MINUTES):
            due.append((at, messages, weekdays))
    return due


class ScheduledPusher:
    def __init__(
        self,
        groups: list[str],
        open_chat: Callable[[str], bool],
        send: Callable[[str, str], ActionResult],
        config_path: Path = CONFIG_PATH,
        state_path: Path = STATE_PATH,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.groups = groups
        self.open_chat = open_chat
        self.send = send
        self.state_path = state_path
        self.sleep = sleep
        self.schedules = load_schedules(config_path)
        self.prefixes = load_prefixes(config_path) if self.schedules else []
        self.mention_all = bool(_read_config(config_path).get("mention_all")) if self.schedules else False
        self.sent = self._load_state()
        if self.schedules:
            _logger.info(f"[ScheduledPush] 已加载 {len(self.schedules)} 个时间点，目标群: {groups}")
            plan = ", ".join(
                at + (f"(周{''.join(WEEKDAY_NAMES[d - 1] for d in sorted(days))})" if days else "(每天)")
                for at, _, days in self.schedules
            )
            print(f"  • 定时推送: {plan} → {', '.join(groups) or '（白名单为空，不会发送）'}")

    def _load_state(self) -> dict[str, str]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _mark_sent(self, key: str, day: str) -> None:
        self.sent[key] = day
        try:
            self.state_path.write_text(json.dumps(self.sent, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            _logger.error(f"[ScheduledPush] 发送记录写入失败: {e}")

    def run_due(self, now: datetime | None = None) -> None:
        """在主循环每轮调用：发送所有到点且今天未发过的内容。"""
        if not self.schedules or not self.groups:
            return
        now = now or datetime.now()
        day = now.strftime("%Y-%m-%d")
        for at, messages, _ in due_schedules(self.schedules, now):
            for group in self.groups:
                key = f"{at}|{group}"
                if self.sent.get(key) == day:
                    continue
                self._mark_sent(key, day)
                self._push(at, group, messages)

    def _push(self, at: str, group: str, messages: list[str]) -> None:
        _logger.info(f"[ScheduledPush] {at} 开始推送到 '{group}'，共 {len(messages)} 条")
        for i, text in enumerate(messages):
            if self.prefixes:
                text = f"{random.choice(self.prefixes)}：{text}"
            if self.mention_all:
                text = f"@所有人 {text}"
            if not self._send_with_retry(group, text):
                _logger.error(f"[ScheduledPush] {at} 推送到 '{group}' 失败，放弃剩余 {len(messages) - i} 条")
                return
            if i < len(messages) - 1:
                self.sleep(random.uniform(2, 5))
        _logger.info(f"[ScheduledPush] {at} 推送到 '{group}' 完成")

    def _send_with_retry(self, group: str, text: str) -> bool:
        for attempt in range(SEND_RETRIES + 1):
            if attempt:
                self.sleep(random.uniform(5, 10))
            if not self.open_chat(group):
                _logger.warning(f"[ScheduledPush] 打开群失败 '{group}' (第 {attempt + 1} 次)")
                continue
            result = self.send(group, text)
            if result.success:
                return True
            _logger.warning(f"[ScheduledPush] 发送失败 '{group}' (第 {attempt + 1} 次): {result.error}")
        return False
