#!/usr/bin/env python3
"""定时推送：在配置的时间（每天或每周指定几天），向 SILENT_WHITELIST 里的群发送一条消息（从 messages 中随机选一条）。

配置文件 data/scheduled_messages.json（修改后重启生效）：
    {
      "mention_all": true,
      "prefixes": ["『宵桜』囡囡播报", "『宵桜』囡囡提醒"],
      "schedules": [
        {"time": "08:00", "messages": ["早上好呀～"]},
        {"time": "21:30", "messages": ["今天也辛苦啦", "早点休息"], "weekdays": [1, 3, 5]}
      ]
    }

- messages：到点从中随机选一条发送（每个群各自随机）。
- weekdays 可选：1=周一 … 7=周日；不配或为空表示每天发送。

- prefixes 可选：每条消息发送时随机选一个前缀，拼成「前缀：消息」。
- mention_all 可选：为 true 时在最前面加「@所有人 」，拼成「@所有人 前缀：消息」。
- 每个时间点、每个群每天最多发一次。开始发送前先写入 data/scheduled_state.json，
  重启或中途异常都不会重复发送（宁可漏发，不重复发）。
- 超过时间点 GRACE_MINUTES 分钟仍未发出（如 bot 未运行），当天跳过，不补发。
- 打开群失败（标题校验不通过）则跳过该群，绝不发到其他会话。

语音（可选）：文字发送成功后，按概率再补一条语音（每个群各自掷概率）。
    "voice_probability": 0.3,                     # 顶层默认概率，不配为 0（不发语音）
    {"time": "21:30", "messages": [...],
     "voice_probability": 1.0,                    # 可选，覆盖顶层概率
     "voice_messages": [                          # 到点随机选一句；不配则只发文字
       {"text": "周活动快结束啦", "mood": "开心", "rate": 1.1, "volume": 60, "style": "尾音上扬"},
       {"text": "快去兑奖哦", "style": ["带点笑意", "声音软一点"]},   # style 可写列表，每次随机选一个
       "只写文字也行（心情平静、语速 1.0）"
     ]}
- mood 见 src/action/voice_sender.py 的 MOODS；rate 0.5–2.0，volume 0–100，style 为补充语气描述。
- 随机性：每次合成随机换 seed（0–65535）；顶层 "voice_rate_jitter": 0.05 表示语速在配置值（未配为 1.0）
  基础上随机 ±0.05，不配为 0（不浮动）；style 为列表时每次随机选一个。
- 顶层 "voice_styles": {"撒娇": ["尾音拖长一点", ...], ...}：句子没写 style 时，从它心情对应的列表里随机选。
- 非法的语音条目启动时报错并跳过；语音失败只记日志，不重试，不影响已发出的文字。
- 发语音前重新打开并校验群标题，校验不通过则不发语音。
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from src.action.voice_sender import DEFAULT_MOOD, MOODS, build_tts_input
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


VoiceLine = dict  # {"text", "mood", 可选 "rate" / "volume" / "style"（配置里为候选列表）}
VoicePlan = tuple[float, list[VoiceLine]]  # (概率, 语音候选)


def _parse_voice_line(raw) -> VoiceLine:
    """字符串或对象 → 语音条目；不合法抛 ValueError。"""
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
        raise ValueError("缺少 text")
    line = {"text": str(raw["text"]).strip(), "mood": raw.get("mood") or DEFAULT_MOOD}
    for k in ("rate", "volume"):
        if raw.get(k) is not None:
            line[k] = raw[k]
    styles = raw.get("style")
    if styles is not None:
        styles = [styles] if isinstance(styles, str) else styles
        if not isinstance(styles, list) or not styles or not all(isinstance(x, str) and x.strip() for x in styles):
            raise ValueError("style 需为非空文字或文字列表")
        line["style"] = [x.strip() for x in styles]
    tune = {k: v for k, v in line.items() if k not in ("text", "mood", "style")}
    for style in line.get("style") or [None]:
        build_tts_input(line["text"], line["mood"], "", style=style, **tune)  # 校验心情和取值范围
    return line


def realize_voice_line(line: VoiceLine, rate_jitter: float = 0.0, rng=random) -> dict:
    """配置条目 → 本次实际合成参数：style 随机选一个、语速随机浮动、随机 seed。"""
    out = {k: v for k, v in line.items() if k != "style"}
    if line.get("style"):
        out["style"] = rng.choice(line["style"])
    if rate_jitter:
        base = line.get("rate", 1.0)
        out["rate"] = round(min(2.0, max(0.5, base + rng.uniform(-rate_jitter, rate_jitter))), 2)
    out["seed"] = rng.randint(0, 65535)
    return out


def load_rate_jitter(path: Path = CONFIG_PATH) -> float:
    j = _read_config(path).get("voice_rate_jitter", 0)
    if not isinstance(j, (int, float)) or not 0 <= j <= 0.5:
        _logger.error(f"[ScheduledPush] voice_rate_jitter 需为 0～0.5，已按 0 处理: {j}")
        print(f"❌ 定时推送 voice_rate_jitter 需为 0～0.5，已按 0 处理: {j}")
        return 0.0
    return float(j)


def _load_mood_styles(data: dict) -> dict[str, list[str]]:
    """顶层 voice_styles：心情 → style 候选。非法的心情或列表报错并跳过。"""
    styles = {}
    for mood, items in (data.get("voice_styles") or {}).items():
        try:
            if mood not in MOODS:
                raise ValueError(f"未知心情「{mood}」")
            styles[mood] = _parse_voice_line({"text": "校验", "mood": mood, "style": items})["style"]
        except (ValueError, TypeError) as e:
            _logger.error(f"[ScheduledPush] voice_styles 的「{mood}」不合法，已跳过: {e}")
            print(f"❌ 定时推送 voice_styles 的「{mood}」不合法，已跳过: {e}")
    return styles


def load_mood_styles(path: Path = CONFIG_PATH) -> dict[str, list[str]]:
    """读取顶层 voice_styles（心情 → style 候选），供回复语音复用。"""
    return _load_mood_styles(_read_config(path))


def load_voice_plans(path: Path = CONFIG_PATH) -> dict[tuple[str, frozenset[int]], VoicePlan]:
    """读取每个定时任务的语音配置，键为 (HH:MM, 星期几)。没有 voice_messages 或概率为 0 的不返回。
    句子没写 style 时，填入 voice_styles 里该心情的候选。"""
    data = _read_config(path)
    default_p = data.get("voice_probability", 0)
    mood_styles = _load_mood_styles(data)
    plans = {}
    for item in data.get("schedules", []):
        lines = []
        for raw in item.get("voice_messages") or []:
            try:
                line = _parse_voice_line(raw)
                if "style" not in line and line["mood"] in mood_styles:
                    line["style"] = mood_styles[line["mood"]]
                lines.append(line)
            except (ValueError, TypeError) as e:
                _logger.error(f"[ScheduledPush] 语音条目不合法，已跳过: {raw}: {e}")
                print(f"❌ 定时推送语音条目不合法，已跳过: {raw}: {e}")
        p = item.get("voice_probability", default_p)
        if not isinstance(p, (int, float)) or not 0 <= p <= 1:
            _logger.error(f"[ScheduledPush] voice_probability 需为 0～1，该条不发语音: {item.get('time')}")
            print(f"❌ 定时推送 {item.get('time')} 的 voice_probability 需为 0～1，该条不发语音")
            continue
        if lines and p > 0:
            plans[(str(item.get("time", "")).strip(), frozenset(item.get("weekdays") or []))] = (float(p), lines)
    return plans


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
        send_voice: Callable[[VoiceLine], ActionResult] | None = None,
    ):
        self.groups = groups
        self.open_chat = open_chat
        self.send = send
        self.state_path = state_path
        self.sleep = sleep
        self.send_voice = send_voice
        self.schedules = load_schedules(config_path)
        self.prefixes = load_prefixes(config_path) if self.schedules else []
        self.mention_all = bool(_read_config(config_path).get("mention_all")) if self.schedules else False
        self.voice_plans = load_voice_plans(config_path) if self.schedules and send_voice else {}
        self.rate_jitter = load_rate_jitter(config_path) if self.voice_plans else 0.0
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
        for at, messages, weekdays in due_schedules(self.schedules, now):
            for group in self.groups:
                key = f"{at}|{group}"
                if self.sent.get(key) == day:
                    continue
                self._mark_sent(key, day)
                self._push(at, group, messages, self.voice_plans.get((at, weekdays)))

    def _push(self, at: str, group: str, messages: list[str], voice: VoicePlan | None = None) -> None:
        text = random.choice(messages)
        _logger.info(f"[ScheduledPush] {at} 开始推送到 '{group}'（从 {len(messages)} 条中随机选 1 条）")
        if self.prefixes:
            text = f"{random.choice(self.prefixes)}：{text}"
        if self.mention_all:
            text = f"@所有人 {text}"
        if not self._send_with_retry(group, text):
            _logger.error(f"[ScheduledPush] {at} 推送到 '{group}' 失败")
            return
        _logger.info(f"[ScheduledPush] {at} 推送到 '{group}' 完成")
        if voice and random.random() < voice[0]:
            self._push_voice(at, group, random.choice(voice[1]))

    def _push_voice(self, at: str, group: str, line: VoiceLine) -> None:
        # 语音只能发到当前窗口：重新打开并校验群标题，不通过就不发
        if not self.open_chat(group):
            _logger.error(f"[ScheduledPush] {at} 语音：打开群失败 '{group}'，不发语音")
            return
        try:
            line = realize_voice_line(line, self.rate_jitter)
            result = self.send_voice(line)
        except Exception as e:  # 语音是附加内容，任何异常都不影响主流程
            result = ActionResult(success=False, error=str(e))
        if result.success:
            _logger.info(f"[ScheduledPush] {at} 语音推送到 '{group}' 完成: {line}")
        else:
            _logger.error(f"[ScheduledPush] {at} 语音推送到 '{group}' 失败: {result.error} | {line}")

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
