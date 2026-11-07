"""回复消息时是否改发语音：纯逻辑，不碰微信。设计见 docs/superpowers/specs/2026-11-07-voice-reply-design.md。

LLM 生成回复时可额外输出 {"voice": {"scene", "mood", "accept"?}}；本模块据此和配置决定：
- 主动（被夸感谢 / 斗嘴调侃 / 安慰）：心情白名单、单条、≤20 字、可念、上一条非语音，再按场合概率；
- 要听语音：同一人窗口内第 1/2/3+ 次按递增概率答应，答应发 accept，否则照发 replies 里的拒绝。
语音 text 替代整批 replies。仅群聊调用。
"""

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.action.voice_sender import MOODS

_logger = logging.getLogger("src.voice_policy")

PROJECT_ROOT = Path(__file__).parent.parent.parent
VOICE_REPLY_CONFIG_PATH = PROJECT_ROOT / "data" / "voice_reply.json"
SCENES = ("被夸感谢", "斗嘴调侃", "安慰", "要听语音")
REQUEST_SCENE = "要听语音"

_MENTION = re.compile(r"^(?:@[^\s\u2005]+[\s\u2005]+)+")  # 只去开头的 @某某（与 _apply_at_policy 一致）
_UNSPEAKABLE = re.compile(
    r"https?://|www\.|[0-9０-９A-Za-zＡ-Ｚａ-ｚ]"
    r"|[\U0001F000-\U0001FAFF☀-➿️]"
    r"|\[[^\[\]]{1,4}\]"  # 微信文字表情 [捂脸]
    r"|@"  # 句中残留的 @某某（开头的已去掉）
)
_NON_WORD = re.compile(r"[\W_]")


@dataclass(frozen=True)
class VoiceReplyConfig:
    dry_run: bool
    hours: tuple[int, int]
    max_chars: int
    rate_by_length: tuple[float, float]
    moods: frozenset[str]
    scenes: dict[str, float]
    request_window_minutes: float
    request_probabilities: tuple[float, ...]


DEFAULTS = VoiceReplyConfig(
    dry_run=True, hours=(8, 22), max_chars=20, rate_by_length=(0.9, 1.2),
    moods=frozenset({"撒娇", "调皮", "开心", "安慰", "惊讶"}),
    scenes={"被夸感谢": 0.15, "斗嘴调侃": 0.15, "安慰": 0.2},
    request_window_minutes=10, request_probabilities=(0.3, 0.6, 0.9),
)


def _prob(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1


def load_voice_reply_config(path: Path = VOICE_REPLY_CONFIG_PATH) -> Optional[VoiceReplyConfig]:
    """不存在或 enabled 为假返回 None；单项非法报 ❌ 并用默认值。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _logger.error(f"[VoiceReply] 配置读取失败，不启用: {e}")
        print(f"❌ 回复语音配置读取失败，不启用: {path.name}: {e}")
        return None
    if not data.get("enabled"):
        return None

    def bad(name):
        _logger.error(f"[VoiceReply] 配置项 {name} 不合法，使用默认值")
        print(f"❌ 回复语音配置项 {name} 不合法，使用默认值")

    d = DEFAULTS
    hours = data.get("hours", list(d.hours))
    if not (isinstance(hours, list) and len(hours) == 2 and all(isinstance(h, int) for h in hours)
            and 0 <= hours[0] < hours[1] <= 24):
        bad("hours"); hours = list(d.hours)
    max_chars = data.get("max_chars", d.max_chars)
    if not (isinstance(max_chars, int) and max_chars > 0):
        bad("max_chars"); max_chars = d.max_chars
    rbl = data.get("rate_by_length", list(d.rate_by_length))
    if not (isinstance(rbl, list) and len(rbl) == 2 and all(isinstance(x, (int, float)) for x in rbl)
            and 0.5 <= rbl[0] <= rbl[1] <= 2.0):
        bad("rate_by_length"); rbl = list(d.rate_by_length)
    moods = data.get("moods", sorted(d.moods))
    if not (isinstance(moods, list) and moods and all(m in MOODS for m in moods)):
        bad("moods"); moods = sorted(d.moods)
    scenes = dict(d.scenes)
    for name, p in (data.get("scenes") or {}).items():
        if name in scenes and _prob(p):
            scenes[name] = float(p)
        else:
            bad(f"scenes.{name}")
    req = data.get("request") or {}
    window = req.get("window_minutes", d.request_window_minutes)
    if not (isinstance(window, (int, float)) and window > 0):
        bad("request.window_minutes"); window = d.request_window_minutes
    probs = req.get("probabilities", list(d.request_probabilities))
    if not (isinstance(probs, list) and probs and all(_prob(p) for p in probs)):
        bad("request.probabilities"); probs = list(d.request_probabilities)
    return VoiceReplyConfig(
        dry_run=bool(data.get("dry_run", True)), hours=(hours[0], hours[1]), max_chars=max_chars,
        rate_by_length=(float(rbl[0]), float(rbl[1])), moods=frozenset(moods), scenes=scenes,
        request_window_minutes=window, request_probabilities=tuple(float(p) for p in probs),
    )


def parse_voice_meta(raw) -> Optional[dict]:
    """只接受 scene ∈ SCENES 且 mood ∈ MOODS 的 dict；accept 仅「要听语音」保留。其他一律 None。"""
    if not isinstance(raw, dict):
        return None
    scene, mood = raw.get("scene"), raw.get("mood")
    if scene not in SCENES or mood not in MOODS:
        return None
    meta = {"scene": scene, "mood": mood}
    if scene == REQUEST_SCENE and isinstance(raw.get("accept"), str):
        meta["accept"] = raw["accept"]
    return meta


def strip_mentions(text: str) -> str:
    return _MENTION.sub("", text).strip()


def speech_len(text: str) -> int:
    return len(_NON_WORD.sub("", strip_mentions(text)))


def is_speakable(text: str) -> bool:
    return not _UNSPEAKABLE.search(text)


def rate_for_length(n: int, lo: float = 0.9, hi: float = 1.2, max_chars: int = 20) -> float:
    n = max(1, min(n, max_chars))
    return round(lo + (n - 1) / (max_chars - 1) * (hi - lo), 2) if max_chars > 1 else lo


@dataclass
class VoiceDecision:
    send_voice: bool
    reason: str
    scene: str = ""
    text: str = ""
    mood: str = ""
    style: Optional[str] = None
    rate: Optional[float] = None


class VoiceReplyPolicy:
    def __init__(self, config: VoiceReplyConfig, mood_styles: dict[str, list[str]], rate_jitter: float,
                 rng: random.Random = random.Random()):
        self.config = config
        self.mood_styles = mood_styles
        self.rate_jitter = rate_jitter
        self.rng = rng
        self._last_voice: dict[str, bool] = {}
        self._requests: dict[tuple[str, str], tuple[int, datetime]] = {}

    def record_sent(self, chat: str, was_voice: bool) -> None:
        self._last_voice[chat] = was_voice

    def _speakable_text(self, text: str) -> tuple[Optional[str], str]:
        text = strip_mentions(text)
        if not text:
            return None, "内容为空"
        if speech_len(text) > self.config.max_chars:
            return None, f"超过{self.config.max_chars}字"
        if not is_speakable(text):
            return None, "含链接/数字/英文/表情"
        return text, ""

    def _voice(self, scene: str, text: str, mood: str, reason: str) -> VoiceDecision:
        lo, hi = self.config.rate_by_length
        rate = rate_for_length(speech_len(text), lo, hi, self.config.max_chars)
        if self.rate_jitter:
            rate += self.rng.uniform(-self.rate_jitter, self.rate_jitter)
        rate = round(min(hi, max(lo, rate)), 2)
        pool = self.mood_styles.get(mood)
        style = self.rng.choice(pool) if pool else None
        return VoiceDecision(True, reason, scene, text, mood, style, rate)

    def decide(self, chat: str, sender: str, replies: list[str], meta: Optional[dict],
               now: datetime) -> VoiceDecision:
        meta = parse_voice_meta(meta)
        if meta is None:
            return VoiceDecision(False, "无语音场合")
        scene, mood = meta["scene"], meta["mood"]
        start, end = self.config.hours
        if not start <= now.hour < end:
            return VoiceDecision(False, f"不在{start}-{end}点", scene)
        if scene == REQUEST_SCENE:
            return self._decide_request(chat, sender, meta, now)

        if mood not in self.config.moods:
            return VoiceDecision(False, f"心情「{mood}」不在白名单", scene)
        if len(replies) != 1:
            return VoiceDecision(False, f"回复{len(replies)}条", scene)
        text, why = self._speakable_text(replies[0])
        if text is None:
            return VoiceDecision(False, why, scene)
        if self._last_voice.get(chat):
            return VoiceDecision(False, "上一条已是语音", scene)
        p = self.config.scenes.get(scene, 0.0)
        if self.rng.random() >= p:
            return VoiceDecision(False, f"概率未中({p})", scene)
        return self._voice(scene, text, mood, f"概率命中({p})")

    def _decide_request(self, chat: str, sender: str, meta: dict, now: datetime) -> VoiceDecision:
        key = (chat, sender)
        count, last = self._requests.get(key, (0, now))
        if count and now - last > timedelta(minutes=self.config.request_window_minutes):
            count = 0
        count += 1
        self._requests[key] = (count, now)
        probs = self.config.request_probabilities
        p = probs[min(count, len(probs)) - 1]
        if self.rng.random() >= p:
            return VoiceDecision(False, f"傲娇拒绝(第{count}次,{p})", REQUEST_SCENE)
        text, why = self._speakable_text(meta.get("accept", ""))
        if text is None:
            return VoiceDecision(False, f"答应句不可用：{why}", REQUEST_SCENE)
        del self._requests[key]
        return self._voice(REQUEST_SCENE, text, meta["mood"], f"答应(第{count}次,{p})")
