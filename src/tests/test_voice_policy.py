"""回复语音策略：时间窗口、场合与心情、字数与可念、不连发、傲娇计数、语速与 style、配置。"""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock

from src.bot.scheduled_push import load_mood_styles
from src.reply.voice_policy import (
    VoiceReplyConfig,
    VoiceReplyPolicy,
    is_speakable,
    load_voice_reply_config,
    parse_voice_meta,
    rate_for_length,
    speech_len,
    strip_mentions,
)

CFG = VoiceReplyConfig(
    dry_run=True, hours=(8, 22), max_chars=20, rate_by_length=(0.9, 1.2),
    moods=frozenset({"撒娇", "调皮", "开心", "安慰", "惊讶"}),
    scenes={"被夸感谢": 0.15, "斗嘴调侃": 0.15, "安慰": 0.2},
    request_window_minutes=10, request_probabilities=(0.3, 0.6, 0.9),
)
NOON = datetime(2026, 11, 7, 12, 0)


class Rng:
    """可控随机数：random() 依次返回给定值；uniform 返回下限+上限中点；choice 取第一个；randint 取下限。"""

    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if self.values else 0.0

    def uniform(self, a, b):
        return (a + b) / 2

    def choice(self, seq):
        return seq[0]

    def randint(self, a, b):
        return a


def _policy(*rolls, styles=None, jitter=0.0):
    return VoiceReplyPolicy(CFG, styles or {}, jitter, rng=Rng(*rolls))


PRAISE = {"scene": "被夸感谢", "mood": "调皮"}


def test_hours_window():
    p = _policy()
    assert not p.decide("群", "A", ["哼，算你有眼光"], PRAISE, NOON.replace(hour=7, minute=59)).send_voice
    assert not p.decide("群", "A", ["哼，算你有眼光"], PRAISE, NOON.replace(hour=22, minute=0)).send_voice
    assert p.decide("群", "A", ["哼，算你有眼光"], PRAISE, NOON.replace(hour=8, minute=0)).send_voice
    assert p.decide("群", "B", ["哼，算你有眼光"], PRAISE, NOON.replace(hour=21, minute=59)).send_voice


def test_proactive_scene_mood_and_probability():
    assert _policy(0.14).decide("群", "A", ["哼"], PRAISE, NOON).send_voice
    assert not _policy(0.16).decide("群", "A", ["哼"], PRAISE, NOON).send_voice
    assert _policy(0.19).decide("群", "A", ["抱抱"], {"scene": "安慰", "mood": "安慰"}, NOON).send_voice
    assert not _policy(0.0).decide("群", "A", ["哼"], {"scene": "被夸感谢", "mood": "平静"}, NOON).send_voice
    assert not _policy(0.0).decide("群", "A", ["哼"], None, NOON).send_voice


def test_proactive_requires_single_short_reply():
    assert not _policy(0.0).decide("群", "A", ["哼", "才不是"], PRAISE, NOON).send_voice
    assert not _policy(0.0).decide("群", "A", ["一" * 21], PRAISE, NOON).send_voice
    assert _policy(0.0).decide("群", "A", ["一" * 20], PRAISE, NOON).send_voice


def test_mentions_stripped_before_count_and_speech():
    d = _policy(0.0).decide("群", "A", ["@张三 哼，算你有眼光"], PRAISE, NOON)
    assert d.send_voice and d.text == "哼，算你有眼光"
    assert strip_mentions("@所有人 早") == "早"
    assert speech_len("@张三 哼，好！") == 2


def test_unspeakable_rejected():
    for text in ("看这个https://a.cn", "8点开打", "OK啦", "好呀😀", "好呀[捂脸]"):
        assert not is_speakable(text)
        assert not _policy(0.0).decide("群", "A", [text], PRAISE, NOON).send_voice
    assert is_speakable("哼，算你有眼光")


def test_long_final_reply_no_voice():
    d = _policy(0.0).decide("群", "A", ["这是一段被自检改写之后变得很长很长的回复内容哦"], PRAISE, NOON)
    assert not d.send_voice


def test_no_consecutive_voice():
    p = _policy(0.0, 0.0, 0.0, 0.0)
    p.record_sent("群", True)
    assert not p.decide("群", "A", ["哼"], PRAISE, NOON).send_voice
    assert p.decide("群", "B", ["好啦"], {"scene": "要听语音", "mood": "不情愿", "accept": "好啦就一句"}, NOON).send_voice
    p.record_sent("群", False)
    assert p.decide("群", "A", ["哼"], PRAISE, NOON).send_voice


def _ask(p, sender, now, accept="好啦好啦，就一句哦"):
    return p.decide("群", sender, ["哼，才不给你听"], {"scene": "要听语音", "mood": "不情愿", "accept": accept}, now)


def test_request_escalation():
    p = _policy(0.35, 0.55)
    assert not _ask(p, "A", NOON).send_voice
    assert _ask(p, "A", NOON + timedelta(minutes=5)).send_voice

    p = _policy(0.85, 0.85, 0.85)
    assert not _ask(p, "B", NOON).send_voice
    assert not _ask(p, "B", NOON + timedelta(minutes=1)).send_voice
    assert _ask(p, "B", NOON + timedelta(minutes=2)).send_voice

    p = _policy(0.35, 0.35)
    assert not _ask(p, "C", NOON).send_voice
    assert not _ask(p, "C", NOON + timedelta(minutes=11)).send_voice  # 超窗，仍按第 1 次 0.3

    p = _policy(0.0, 0.35)
    assert _ask(p, "D", NOON).send_voice
    assert not _ask(p, "D", NOON + timedelta(minutes=1)).send_voice  # 答应后从第 1 次算


def test_request_accept_text_rules():
    p = _policy(0.0, 0.55)
    assert not _ask(p, "A", NOON, accept="一" * 21).send_voice  # 掷中但太长 → 拒绝，计数不清零
    d = _ask(p, "A", NOON + timedelta(minutes=1))  # 第 2 次 0.6
    assert d.send_voice and d.text == "好啦好啦，就一句哦"

    p = _policy(0.35)
    assert not _ask(p, "B", NOON.replace(hour=23)).send_voice  # 窗口外不计次
    assert not _ask(p, "B", NOON).send_voice  # 仍是第 1 次（0.35 ≥ 0.3）


def test_rate_for_length():
    assert rate_for_length(1) == 0.9
    assert rate_for_length(20) == 1.2
    assert rate_for_length(30) == 1.2
    import random
    p = VoiceReplyPolicy(CFG, {}, 0.1, rng=random.Random(3))
    for _ in range(100):
        d = p.decide("群", "A", ["哼"], {"scene": "要听语音", "mood": "不情愿", "accept": "好吧"}, NOON)
        if d.send_voice:
            assert 0.9 <= d.rate <= 1.2


def test_style_from_mood_pool():
    d = _policy(0.0, styles={"调皮": ["a", "b"]}).decide("群", "A", ["哼"], PRAISE, NOON)
    assert d.style in ("a", "b") and d.mood == "调皮"
    assert _policy(0.0).decide("群", "A", ["哼"], PRAISE, NOON).style is None


def test_garbage_meta_is_none():
    for raw in ("x", [], {}, {"scene": "乱写", "mood": "开心"}, {"scene": "安慰", "mood": "暴躁"}, Mock(), None):
        assert parse_voice_meta(raw) is None
    assert parse_voice_meta({"scene": "安慰", "mood": "安慰", "accept": "x"}) == {"scene": "安慰", "mood": "安慰"}
    assert parse_voice_meta({"scene": "要听语音", "mood": "不情愿", "accept": "好吧"})["accept"] == "好吧"


def test_load_config(tmp_path):
    assert load_voice_reply_config(tmp_path / "none.json") is None
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert load_voice_reply_config(path) is None
    full = {"enabled": True, "dry_run": True, "hours": [8, 22], "max_chars": 20, "rate_by_length": [0.9, 1.2],
            "moods": ["撒娇", "调皮", "开心", "安慰", "惊讶"],
            "scenes": {"被夸感谢": 0.15, "斗嘴调侃": 0.15, "安慰": 0.2},
            "request": {"window_minutes": 10, "probabilities": [0.3, 0.6, 0.9]}}
    path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    assert load_voice_reply_config(path) == CFG
    full["scenes"]["安慰"] = 2
    path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    assert load_voice_reply_config(path).scenes["安慰"] == 0.2  # 非法 → 默认值


def test_load_mood_styles_public(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"voice_styles": {"开心": ["元气"]}}, ensure_ascii=False), encoding="utf-8")
    assert load_mood_styles(path) == {"开心": ["元气"]}
