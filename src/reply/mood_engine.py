"""轻量持久化心情引擎（Mood Engine V1）。

职责：
- 独立 SQLite（data/db/mood.db）持久化保存群级氛围情绪与对单人的短期情绪残留；
- 负面情绪严格隔离到个人 (chat_key, user_key)，避免群聊中迁怒其他群友；
- 按时间半衰期自然回落至「平静」，结合睡前时段与近期聊天密度计算精力（高/平常/低）；
- 复用关系事件与语音 mood 驱动，0 额外 LLM 调用，绝不向模型或用户暴露分值或要求动作/旁白。
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Optional, Tuple

_logger = logging.getLogger("src.reply.mood_engine")

# 情绪表达倾向（注入 <current_state>，严格遵守 persona.md 禁止动作/旁白规则）
MOOD_RULES: Dict[str, str] = {
    "平静": "按平时的长度和语气。不改变事实、拒绝和 relationship。",
    "开心": "语气轻松自然，可以稍微多搭半句，不刻意卖萌。不改变事实、拒绝和 relationship。",
    "调皮": "可以轻快接梗或轻微打趣，别过度玩梗。不改变事实、拒绝和 relationship。",
    "小郁闷": "话可以稍短一点，语气平淡些，不卖惨也不迁怒别人。不改变事实、拒绝和 relationship。",
    "不耐烦": "回复简短直接，不接对方玩笑，维持拒绝边界。不改变事实、拒绝和 relationship。",
    "犯困": "可以更短，可以露出犯困。不改变事实、拒绝和 relationship。别人没说要睡时，不要催别人休息。",
}

# 各情绪衰减半衰期（分钟）
MOOD_HALF_LIFE_MINUTES: Dict[str, float] = {
    "开心": 45.0,
    "调皮": 35.0,
    "小郁闷": 60.0,
    "不耐烦": 120.0,
}

# 关系事件 → (情绪, 基础冲击强度, 是否同步影响群级氛围)
RELATIONSHIP_EVENT_TO_MOOD: Dict[str, Tuple[str, float, bool]] = {
    "friendly_banter": ("调皮", 35.0, True),
    "helpful_share": ("开心", 35.0, True),
    "sincere_care": ("开心", 45.0, True),
    "remembered_detail": ("开心", 50.0, True),
    "respect_boundary": ("平静", 0.0, False),  # 特殊处理：加速缓和个人负面情绪
    "sincere_apology": ("平静", 0.0, False),   # 特殊处理：减弱个人不耐烦
    "boundary_push": ("不耐烦", 55.0, False),
    "repeated_annoyance": ("不耐烦", 75.0, False),
    "rude_or_deception": ("不耐烦", 85.0, False),
}

# 语音单轮 mood → (会话情绪, 轻度冲击强度, 是否影响群级)
VOICE_MOOD_FEEDBACK: Dict[str, Tuple[str, float, bool]] = {
    "开心": ("开心", 25.0, True),
    "调皮": ("调皮", 25.0, True),
    "撒娇": ("调皮", 22.0, True),
    "难过": ("小郁闷", 25.0, False),
    "生气": ("不耐烦", 35.0, False),
    "不情愿": ("不耐烦", 25.0, False),
}

RELATIONSHIP_LEVEL_WEIGHT: Dict[str, float] = {
    "buddy": 1.2,
    "friend": 1.2,
    "familiar": 1.05,
    "acquaintance": 1.0,
    "known": 1.0,
    "stranger": 0.8,
    "guarded": 1.1,
    "refused": 1.1,
}

DECAY_FLOOR = 15.0  # 强度衰减低于此值回归「平静」
NEGATIVE_MOODS = {"不耐烦", "小郁闷"}
UPBEAT_BLOCKED_MOODS = {"犯困", "不耐烦", "小郁闷"}
FATIGUE_WINDOW_SECONDS = 900.0  # 15 分钟窗口
FATIGUE_REPLY_THRESHOLD = 8     # 15 分钟内连回 8 次进入低精力


@dataclass
class MoodRecord:
    chat_key: str
    scope_key: str  # "__group__" 或 user_key
    mood: str = "平静"
    intensity: float = 0.0
    reason: str = ""
    updated_at: float = 0.0


def _decay_intensity(mood: str, intensity: float, elapsed_seconds: float) -> float:
    if intensity <= 0.0 or mood not in MOOD_HALF_LIFE_MINUTES:
        return 0.0
    if elapsed_seconds <= 0.0:
        return intensity
    half_life_sec = MOOD_HALF_LIFE_MINUTES[mood] * 60.0
    return intensity * math.pow(0.5, elapsed_seconds / half_life_sec)


class MoodStore:
    """SQLite 持久化存储，独立于 relationship.db。"""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            project_root = Path(__file__).parent.parent.parent
            db_path = project_root / "data" / "db" / "mood.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mood_states (
                    chat_key TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    mood TEXT NOT NULL DEFAULT '平静',
                    intensity REAL NOT NULL DEFAULT 0.0,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0.0,
                    PRIMARY KEY (chat_key, scope_key)
                )
                """
            )
            self._conn.commit()

    def get(self, chat_key: str, scope_key: str) -> Optional[MoodRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mood_states WHERE chat_key = ? AND scope_key = ?",
                (chat_key, scope_key),
            ).fetchone()
            if not row:
                return None
            return MoodRecord(
                chat_key=row["chat_key"],
                scope_key=row["scope_key"],
                mood=row["mood"] or "平静",
                intensity=float(row["intensity"] or 0.0),
                reason=row["reason"] or "",
                updated_at=float(row["updated_at"] or 0.0),
            )

    def upsert(self, record: MoodRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO mood_states (chat_key, scope_key, mood, intensity, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key, scope_key) DO UPDATE SET
                    mood=excluded.mood,
                    intensity=excluded.intensity,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    record.chat_key,
                    record.scope_key,
                    record.mood,
                    record.intensity,
                    record.reason,
                    record.updated_at,
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


class MoodEngine:
    """轻量动态心情引擎：群级氛围 + 个人残留 + 时间衰减 + 精力节律 + 语音联动。"""

    GROUP_SCOPE = "__group__"

    def __init__(
        self,
        store: Optional[MoodStore] = None,
        now: Callable[[], float] = time.time,
    ):
        self.store = store or MoodStore()
        self.now = now
        self._op_lock = threading.RLock()
        self._recent_replies: Dict[str, Deque[float]] = {}

    def _decayed_record(self, chat_key: str, scope_key: str, now_ts: float) -> MoodRecord:
        rec = self.store.get(chat_key, scope_key)
        if rec is None:
            return MoodRecord(chat_key=chat_key, scope_key=scope_key, mood="平静", intensity=0.0, updated_at=now_ts)
        decayed = _decay_intensity(rec.mood, rec.intensity, now_ts - rec.updated_at)
        if decayed < DECAY_FLOOR:
            return MoodRecord(chat_key=chat_key, scope_key=scope_key, mood="平静", intensity=0.0, updated_at=now_ts)
        return MoodRecord(
            chat_key=chat_key,
            scope_key=scope_key,
            mood=rec.mood,
            intensity=decayed,
            reason=rec.reason,
            updated_at=now_ts,
        )

    def _apply_impulse(
        self,
        chat_key: str,
        scope_key: str,
        target_mood: str,
        delta: float,
        reason: str,
        now_ts: float,
    ) -> MoodRecord:
        cur = self._decayed_record(chat_key, scope_key, now_ts)
        if target_mood == cur.mood:
            # 同向叠加收益递减，避免线性爆表
            headroom = max(0.0, 100.0 - cur.intensity)
            new_intensity = min(100.0, cur.intensity + delta * (headroom / 100.0) * 0.85 + delta * 0.15)
            new_mood = target_mood
        elif (cur.mood in NEGATIVE_MOODS) != (target_mood in NEGATIVE_MOODS):
            # 异向情绪：强惯性抵消，只有净差值明显超过阈值才翻转，否则先缓和或回到平静
            if delta >= cur.intensity + DECAY_FLOOR:
                new_mood = target_mood
                new_intensity = min(100.0, delta - cur.intensity)
            elif cur.intensity - delta >= DECAY_FLOOR:
                new_mood = cur.mood
                new_intensity = cur.intensity - delta
            else:
                new_mood = "平静"
                new_intensity = 0.0
        else:
            # 同极性不同标签（如 开心 <-> 调皮），强的一方胜出
            if delta >= cur.intensity * 0.85:
                new_mood = target_mood
                new_intensity = min(100.0, max(delta, cur.intensity * 0.7 + delta * 0.4))
            else:
                new_mood = cur.mood
                new_intensity = min(100.0, cur.intensity + delta * 0.25)

        if new_intensity < DECAY_FLOOR:
            new_mood = "平静"
            new_intensity = 0.0

        updated = MoodRecord(
            chat_key=chat_key,
            scope_key=scope_key,
            mood=new_mood,
            intensity=new_intensity,
            reason=reason[:60] if new_mood != "平静" else "",
            updated_at=now_ts,
        )
        self.store.upsert(updated)
        return updated

    def apply_event(
        self,
        chat_key: str,
        event_type: str,
        *,
        user_key: Optional[str] = None,
        intensity: float = 1.0,
        relationship_level: str = "acquaintance",
        summary: str = "",
    ) -> None:
        """响应关系事件更新短期心情：负面仅作用于个人，绝不迁怒同群其他人。"""
        if not chat_key or event_type not in RELATIONSHIP_EVENT_TO_MOOD:
            return
        now_ts = self.now()
        scale = max(0.0, min(1.0, float(intensity)))
        rel_weight = RELATIONSHIP_LEVEL_WEIGHT.get(relationship_level, 1.0)

        with self._op_lock:
            # 守边界 / 真诚道歉：缓和个人负面残留；强烈不耐烦经真诚道歉后软化为轻微「小郁闷」（不直接跳开心）
            if event_type in ("respect_boundary", "sincere_apology"):
                if user_key:
                    cur = self._decayed_record(chat_key, user_key, now_ts)
                    if cur.mood in NEGATIVE_MOODS and cur.intensity > 0:
                        factor = 0.55 if event_type == "sincere_apology" else 0.7
                        new_int = cur.intensity * factor
                        if new_int < DECAY_FLOOR:
                            new_mood = "平静"
                        elif event_type == "sincere_apology" and cur.mood == "不耐烦":
                            new_mood = "小郁闷"
                        else:
                            new_mood = cur.mood
                        self.store.upsert(
                            MoodRecord(
                                chat_key=chat_key,
                                scope_key=user_key,
                                mood=new_mood,
                                intensity=new_int if new_mood != "平静" else 0.0,
                                reason=cur.reason if new_mood != "平静" else "",
                                updated_at=now_ts,
                            )
                        )
                return

            mood, base_delta, affects_group = RELATIONSHIP_EVENT_TO_MOOD[event_type]
            delta = base_delta * scale * rel_weight
            if delta <= 0.0:
                return
            had_personal_negative = False
            if user_key:
                prev_user = self._decayed_record(chat_key, user_key, now_ts)
                had_personal_negative = prev_user.mood in NEGATIVE_MOODS and prev_user.intensity >= DECAY_FLOOR
                self._apply_impulse(chat_key, user_key, mood, delta, summary or event_type, now_ts)
            if affects_group and not had_personal_negative:
                # 群级氛围受单次事件影响更温和（0.6x）；若该人原本处于个人负面，先抵消个人情绪不拉高群氛围
                self._apply_impulse(chat_key, self.GROUP_SCOPE, mood, delta * 0.6, summary or event_type, now_ts)

    def apply_voice_mood(
        self,
        chat_key: str,
        voice_mood: str,
        *,
        user_key: Optional[str] = None,
    ) -> None:
        """将刚发出的语音语气温和回写到短期心情，增强跨轮连续性。"""
        if not chat_key or voice_mood not in VOICE_MOOD_FEEDBACK:
            return
        mood, delta, affects_group = VOICE_MOOD_FEEDBACK[voice_mood]
        now_ts = self.now()
        with self._op_lock:
            if user_key:
                self._apply_impulse(chat_key, user_key, mood, delta, f"语音:{voice_mood}", now_ts)
            if affects_group:
                self._apply_impulse(chat_key, self.GROUP_SCOPE, mood, delta * 0.5, f"语音:{voice_mood}", now_ts)

    def record_bot_reply(self, chat_key: str) -> None:
        """记录本群刚发过一次回复，用于计算近期聊天疲劳度。"""
        if not chat_key:
            return
        now_ts = self.now()
        with self._op_lock:
            q = self._recent_replies.setdefault(chat_key, deque())
            q.append(now_ts)
            cutoff = now_ts - FATIGUE_WINDOW_SECONDS
            while q and q[0] < cutoff:
                q.popleft()

    def _is_fatigued(self, chat_key: str, now_ts: float) -> bool:
        q = self._recent_replies.get(chat_key)
        if not q:
            return False
        cutoff = now_ts - FATIGUE_WINDOW_SECONDS
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= FATIGUE_REPLY_THRESHOLD

    def get_state(
        self,
        chat_key: str,
        *,
        user_key: Optional[str] = None,
        in_bedtime: bool = False,
    ) -> Tuple[str, str, str]:
        """计算当前有效 (mood, energy, rules)。

        优先级：
        1. 对该人的未衰减负面残留（不迁怒别人）
        2. 睡前时段（犯困 + 低精力）
        3. 对该人的积极残留或群级氛围情绪
        4. 基线（平静 + 平常/低精力）
        """
        now_ts = self.now()
        with self._op_lock:
            user_rec = self._decayed_record(chat_key, user_key, now_ts) if (chat_key and user_key) else None
            group_rec = self._decayed_record(chat_key, self.GROUP_SCOPE, now_ts) if chat_key else None
            fatigued = self._is_fatigued(chat_key, now_ts) if chat_key else False

        # 1. 个人负面残留优先
        if user_rec and user_rec.mood in NEGATIVE_MOODS and user_rec.intensity >= DECAY_FLOOR:
            energy = "低" if (in_bedtime or fatigued) else "平常"
            return user_rec.mood, energy, MOOD_RULES[user_rec.mood]

        # 2. 睡前时段优先表现犯困
        if in_bedtime:
            return "犯困", "低", MOOD_RULES["犯困"]

        # 3. 个人积极情绪 vs 群级氛围，取较强者
        chosen = None
        if user_rec and user_rec.mood != "平静" and user_rec.intensity >= DECAY_FLOOR:
            chosen = user_rec
        if group_rec and group_rec.mood != "平静" and group_rec.intensity >= DECAY_FLOOR:
            if chosen is None or group_rec.intensity > chosen.intensity:
                chosen = group_rec

        if chosen is not None and chosen.mood in MOOD_RULES:
            if fatigued:
                energy = "低"
            elif chosen.mood in ("开心", "调皮") and chosen.intensity >= 60.0:
                energy = "高"
            else:
                energy = "平常"
            return chosen.mood, energy, MOOD_RULES[chosen.mood]

        # 4. 基线
        energy = "低" if fatigued else "平常"
        return "平静", energy, MOOD_RULES["平静"]

    def allows_upbeat_voice(
        self,
        chat_key: str,
        *,
        user_key: Optional[str] = None,
        in_bedtime: bool = False,
    ) -> bool:
        """处于犯困/不耐烦/小郁闷时，抑制发出违和的主动欢快语音。"""
        mood, _, _ = self.get_state(chat_key, user_key=user_key, in_bedtime=in_bedtime)
        return mood not in UPBEAT_BLOCKED_MOODS
