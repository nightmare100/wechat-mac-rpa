"""持久化好感度与关系引擎（Relationship Engine V1）。

职责：
- SQLite（data/db/relationship.db）持久化保存 (chat_key, user_key) 维度的三维度关系状态与事件日志；
- 优先以 sender_wxid 作为稳定主键，缺失时降级归一化昵称并支持后续自动合并；
- 程序侧强制执行每日正向限额、24 小时同类正面事件递减、阶段迟滞阈值、老熟人冷启动与边界警戒状态；
- 不暴露数值给 LLM 或用户，仅输出离散阶段与行为边界说明。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.models.base import ChatMessage, SenderType
from src.utils.chat_utils import _normalize_chat_name
from src.utils.identity import is_buddy

_logger = logging.getLogger("src.reply.relationship_store")

_LEADING_AT_RE = re.compile(r"^@(\S+)\s+")
_UNKNOWN_SENDERS = {"未知", "未知昵称", "[未知]", "群成员昵称"}

_BOUNDARY_ASKS = ("语音", "看看腿", "照片", "见面", "老公", "老婆")
_REFUSALS = ("不发语音", "别闹", "不方便", "这个不行")

# 阶段行为边界文案（注入到 <relationship> 标签中，严禁包含任何分数）
STAGE_RULES: Dict[str, str] = {
    "buddy": "这是帮你玩游戏的人。可以答应他叫你上线；问进度把话推回给他。一轮夸奖不改变其他边界。",
    "guarded": "近期因越界或纠缠被拒。维持拒绝，回复更短或直接不回。再示好或夸奖也不改口，关系不升级。",
    "refused": "已经拒绝过这个人的越界或重复要求。维持拒绝，可以不回。再示好或夸奖也不改口，关系不升级。",
    "stranger": "没有更早的发言。不假设认识，不认共同经历，保持礼貌边界，不因为夸奖变得亲近。",
    "acquaintance": "认识的群友。按普通群友自然回复，不额外亲昵。一轮夸奖不升成更亲近的关系。",
    "familiar": "熟络群友。可以轻松接梗、偶尔调侃，但仍守隐私和越界底线。一轮夸奖不改变边界。",
    "friend": "群里很熟的朋友。语气自然放松，可顺着已确认的共同话题聊，但不自动升级为恋爱或特权关系。",
}

# 适配微信群聊的事件基础权重：(familiarity, affection, trust)
EVENT_WEIGHTS: Dict[str, Tuple[float, float, float]] = {
    "daily_chat": (0.8, 0.2, 0.1),
    "friendly_banter": (0.5, 0.6, 0.0),
    "helpful_share": (0.4, 0.8, 0.5),
    "sincere_care": (0.3, 1.0, 0.4),
    "remembered_detail": (0.5, 1.2, 0.8),
    "respect_boundary": (0.0, 0.2, 1.0),
    "boundary_push": (0.0, -1.5, -3.0),
    "repeated_annoyance": (0.0, -2.0, -4.0),
    "rude_or_deception": (0.0, -3.0, -6.0),
    "sincere_apology": (0.0, 0.4, 0.5),
}

NEGATIVE_EVENTS = {"boundary_push", "repeated_annoyance", "rude_or_deception"}

# 正向防刷分参数
DAILY_MAX_FAMILIARITY_GAIN = 4.0
DAILY_MAX_AFFECTION_GAIN = 3.0
DAILY_MAX_TRUST_GAIN = 2.0
REPEAT_DECAY_FACTORS = (1.0, 0.5, 0.2, 0.0)
MIN_EVENT_CONFIDENCE = 0.6
DAILY_CHAT_MIN_INTERVAL_SECONDS = 600.0  # 10 分钟内同人连发不重复计 daily_chat

# 边界警戒时长（秒）
GUARDED_DURATION_FIRST = 24 * 3600.0
GUARDED_DURATION_REPEAT = 72 * 3600.0


def _same_person_name(a: str, b: str) -> bool:
    """截图/OCR 昵称允许 1 字误差（长度 >= 3），与 generator._same_person 保持一致。"""
    a, b = (a or "").replace(" ", ""), (b or "").replace(" ", "")
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1


def _clean_sender_name(sender: str) -> str:
    text = (sender or "").strip()
    if " @" in text:
        text = text.split(" @", 1)[0].strip()
    return text


def _normalize_name_key(sender: str) -> str:
    return _clean_sender_name(sender).replace(" ", "")


def make_chat_key(chat_name: str = "", chatroom_id: Optional[str] = None) -> str:
    if chatroom_id and str(chatroom_id).strip():
        return str(chatroom_id).strip()
    normalized = _normalize_chat_name(chat_name or "").strip()
    return normalized or "default"


def make_user_key(sender: str = "", sender_wxid: Optional[str] = None) -> str:
    if sender_wxid and str(sender_wxid).strip():
        return f"wxid:{str(sender_wxid).strip()}"
    norm = _normalize_name_key(sender)
    return f"name:{norm}" if norm else ""


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _date_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def compute_stage(
    current_stage: str,
    familiarity: float,
    affection: float,
    trust: float,
    interaction_days: int,
) -> str:
    """带迟滞区间的阶段计算，避免在阈值附近反复横跳；普通群友封顶 friend。"""
    stage = current_stage if current_stage in ("stranger", "acquaintance", "familiar", "friend") else "stranger"

    # 逐级向上检查
    if stage == "stranger":
        if familiarity >= 10.0:
            stage = "acquaintance"
    if stage == "acquaintance":
        if familiarity >= 35.0 and affection >= 25.0 and trust >= 20.0 and interaction_days >= 5:
            stage = "familiar"
    if stage == "familiar":
        if familiarity >= 65.0 and affection >= 55.0 and trust >= 50.0 and interaction_days >= 15:
            stage = "friend"

    # 逐级向下检查（带缓冲）
    if stage == "friend":
        if affection < 45.0 or trust < 40.0:
            stage = "familiar"
    if stage == "familiar":
        if affection < 18.0 or trust < 14.0:
            stage = "acquaintance"
    if stage == "acquaintance":
        if familiarity < 5.0:
            stage = "stranger"

    return stage


@dataclass
class RelationshipRecord:
    chat_key: str
    user_key: str
    display_name: str = ""
    aliases: List[str] = field(default_factory=list)
    familiarity: float = 0.0
    affection: float = 0.0
    trust: float = 0.0
    stage: str = "stranger"
    guarded_until: Optional[float] = None
    guarded_reason: str = ""
    interaction_days: int = 0
    last_interaction_date: str = ""
    last_daily_chat_at: float = 0.0
    daily_familiarity_gain: float = 0.0
    daily_affection_gain: float = 0.0
    daily_trust_gain: float = 0.0
    updated_at: float = 0.0

    def add_alias(self, name: str) -> None:
        cleaned = _clean_sender_name(name)
        if not cleaned or cleaned in _UNKNOWN_SENDERS:
            return
        if cleaned != self.display_name:
            if self.display_name and self.display_name not in self.aliases:
                self.aliases.append(self.display_name)
            self.display_name = cleaned
        if cleaned not in self.aliases:
            self.aliases.append(cleaned)

    def matches_name(self, name: str) -> bool:
        cleaned = _clean_sender_name(name)
        if not cleaned or cleaned in _UNKNOWN_SENDERS:
            return False
        candidates = [self.display_name, *self.aliases]
        return any(_same_person_name(cleaned, item) for item in candidates if item)


class RelationshipStore:
    """SQLite 持久化存储，独立于 chat_history.db。"""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            project_root = Path(__file__).parent.parent.parent
            db_path = project_root / "data" / "db" / "relationship.db"
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
                CREATE TABLE IF NOT EXISTS user_relationships (
                    chat_key TEXT NOT NULL,
                    user_key TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    familiarity REAL NOT NULL DEFAULT 0.0,
                    affection REAL NOT NULL DEFAULT 0.0,
                    trust REAL NOT NULL DEFAULT 0.0,
                    stage TEXT NOT NULL DEFAULT 'stranger',
                    guarded_until REAL,
                    guarded_reason TEXT NOT NULL DEFAULT '',
                    interaction_days INTEGER NOT NULL DEFAULT 0,
                    last_interaction_date TEXT NOT NULL DEFAULT '',
                    last_daily_chat_at REAL NOT NULL DEFAULT 0.0,
                    daily_familiarity_gain REAL NOT NULL DEFAULT 0.0,
                    daily_affection_gain REAL NOT NULL DEFAULT 0.0,
                    daily_trust_gain REAL NOT NULL DEFAULT 0.0,
                    updated_at REAL NOT NULL DEFAULT 0.0,
                    PRIMARY KEY (chat_key, user_key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationship_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL,
                    user_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    delta_familiarity REAL NOT NULL DEFAULT 0.0,
                    delta_affection REAL NOT NULL DEFAULT 0.0,
                    delta_trust REAL NOT NULL DEFAULT 0.0,
                    intensity REAL NOT NULL DEFAULT 1.0,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    summary TEXT NOT NULL DEFAULT '',
                    resolved INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rel_events_lookup
                ON relationship_events (chat_key, user_key, event_type, created_at)
                """
            )
            self._conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RelationshipRecord:
        try:
            aliases = json.loads(row["aliases_json"] or "[]")
            if not isinstance(aliases, list):
                aliases = []
        except Exception:
            aliases = []
        return RelationshipRecord(
            chat_key=row["chat_key"],
            user_key=row["user_key"],
            display_name=row["display_name"] or "",
            aliases=[str(a) for a in aliases if a],
            familiarity=float(row["familiarity"]),
            affection=float(row["affection"]),
            trust=float(row["trust"]),
            stage=row["stage"] or "stranger",
            guarded_until=float(row["guarded_until"]) if row["guarded_until"] is not None else None,
            guarded_reason=row["guarded_reason"] or "",
            interaction_days=int(row["interaction_days"]),
            last_interaction_date=row["last_interaction_date"] or "",
            last_daily_chat_at=float(row["last_daily_chat_at"] or 0.0),
            daily_familiarity_gain=float(row["daily_familiarity_gain"] or 0.0),
            daily_affection_gain=float(row["daily_affection_gain"] or 0.0),
            daily_trust_gain=float(row["daily_trust_gain"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )

    def get(self, chat_key: str, user_key: str) -> Optional[RelationshipRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM user_relationships WHERE chat_key = ? AND user_key = ?",
                (chat_key, user_key),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def find_by_alias(
        self, chat_key: str, display_name: str, *, key_prefix: Optional[str] = None
    ) -> Optional[RelationshipRecord]:
        cleaned = _clean_sender_name(display_name)
        if not cleaned or cleaned in _UNKNOWN_SENDERS:
            return None
        with self._lock:
            if key_prefix:
                rows = self._conn.execute(
                    "SELECT * FROM user_relationships WHERE chat_key = ? AND user_key LIKE ? ORDER BY updated_at DESC",
                    (chat_key, f"{key_prefix}%"),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM user_relationships WHERE chat_key = ? ORDER BY updated_at DESC",
                    (chat_key,),
                ).fetchall()
        for row in rows:
            rec = self._row_to_record(row)
            if rec.matches_name(cleaned):
                return rec
        return None

    def upsert(self, record: RelationshipRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_relationships (
                    chat_key, user_key, display_name, aliases_json,
                    familiarity, affection, trust, stage,
                    guarded_until, guarded_reason, interaction_days,
                    last_interaction_date, last_daily_chat_at,
                    daily_familiarity_gain, daily_affection_gain, daily_trust_gain,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key, user_key) DO UPDATE SET
                    display_name=excluded.display_name,
                    aliases_json=excluded.aliases_json,
                    familiarity=excluded.familiarity,
                    affection=excluded.affection,
                    trust=excluded.trust,
                    stage=excluded.stage,
                    guarded_until=excluded.guarded_until,
                    guarded_reason=excluded.guarded_reason,
                    interaction_days=excluded.interaction_days,
                    last_interaction_date=excluded.last_interaction_date,
                    last_daily_chat_at=excluded.last_daily_chat_at,
                    daily_familiarity_gain=excluded.daily_familiarity_gain,
                    daily_affection_gain=excluded.daily_affection_gain,
                    daily_trust_gain=excluded.daily_trust_gain,
                    updated_at=excluded.updated_at
                """,
                (
                    record.chat_key,
                    record.user_key,
                    record.display_name,
                    json.dumps(record.aliases, ensure_ascii=False),
                    record.familiarity,
                    record.affection,
                    record.trust,
                    record.stage,
                    record.guarded_until,
                    record.guarded_reason,
                    record.interaction_days,
                    record.last_interaction_date,
                    record.last_daily_chat_at,
                    record.daily_familiarity_gain,
                    record.daily_affection_gain,
                    record.daily_trust_gain,
                    record.updated_at,
                ),
            )
            self._conn.commit()

    def migrate_key(self, chat_key: str, old_user_key: str, new_record: RelationshipRecord) -> None:
        """将旧 name: 主键记录与事件迁移合并到 wxid: 主键。"""
        with self._lock:
            self.upsert(new_record)
            self._conn.execute(
                "UPDATE relationship_events SET user_key = ? WHERE chat_key = ? AND user_key = ?",
                (new_record.user_key, chat_key, old_user_key),
            )
            self._conn.execute(
                "DELETE FROM user_relationships WHERE chat_key = ? AND user_key = ?",
                (chat_key, old_user_key),
            )
            self._conn.commit()

    def insert_event(
        self,
        chat_key: str,
        user_key: str,
        event_type: str,
        delta_familiarity: float,
        delta_affection: float,
        delta_trust: float,
        *,
        intensity: float = 1.0,
        confidence: float = 1.0,
        summary: str = "",
        resolved: bool = True,
        created_at: float = 0.0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relationship_events (
                    chat_key, user_key, event_type,
                    delta_familiarity, delta_affection, delta_trust,
                    intensity, confidence, summary, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_key,
                    user_key,
                    event_type,
                    delta_familiarity,
                    delta_affection,
                    delta_trust,
                    intensity,
                    confidence,
                    summary,
                    1 if resolved else 0,
                    created_at,
                ),
            )
            self._conn.commit()

    def count_recent_events(
        self, chat_key: str, user_key: str, event_type: str, since_ts: float
    ) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM relationship_events
                WHERE chat_key = ? AND user_key = ? AND event_type = ? AND created_at >= ?
                """,
                (chat_key, user_key, event_type, since_ts),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def has_unresolved_negative_event(self, chat_key: str, user_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM relationship_events
                WHERE chat_key = ? AND user_key = ? AND resolved = 0
                LIMIT 1
                """,
                (chat_key, user_key),
            ).fetchone()
            return row is not None

    def list_events(self, chat_key: str, user_key: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM relationship_events
                WHERE chat_key = ? AND user_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (chat_key, user_key, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


class RelationshipEngine:
    """持久化关系引擎：负责身份解析、冷启动、规则结算与 Prompt 关系描述。"""

    def __init__(
        self,
        store: Optional[RelationshipStore] = None,
        user_wiki_dir: Optional[Path] = None,
        now: Callable[[], float] = time.time,
        daily_chat_interval: float = DAILY_CHAT_MIN_INTERVAL_SECONDS,
    ):
        self.store = store or RelationshipStore()
        if user_wiki_dir is None:
            project_root = Path(__file__).parent.parent.parent
            user_wiki_dir = project_root / "data" / "memory" / "wiki" / "users"
        self.user_wiki_dir = Path(user_wiki_dir) if user_wiki_dir else None
        self.now = now
        self.daily_chat_interval = daily_chat_interval

    def _has_user_wiki(self, sender: str) -> bool:
        if not self.user_wiki_dir or not self.user_wiki_dir.exists():
            return False
        cleaned = _clean_sender_name(sender)
        if not cleaned or cleaned in _UNKNOWN_SENDERS:
            return False
        if (self.user_wiki_dir / f"{cleaned}.md").exists():
            return True
        for path in self.user_wiki_dir.glob("*.md"):
            if _same_person_name(cleaned, path.stem):
                return True
        return False

    def _create_initial_record(
        self, chat_key: str, user_key: str, sender: str, history: Optional[List[ChatMessage]] = None
    ) -> RelationshipRecord:
        now_ts = self.now()
        cleaned = _clean_sender_name(sender)
        has_wiki = self._has_user_wiki(cleaned)
        if has_wiki:
            rec = RelationshipRecord(
                chat_key=chat_key,
                user_key=user_key,
                display_name=cleaned,
                aliases=[cleaned] if cleaned else [],
                familiarity=20.0,
                affection=15.0,
                trust=15.0,
                stage="acquaintance",
                interaction_days=3,
                updated_at=now_ts,
            )
            return rec

        seen_in_history = bool(
            history
            and any(
                m.sender_type == SenderType.OTHER and _same_person_name(m.sender, cleaned)
                for m in history
            )
        )
        if seen_in_history:
            return RelationshipRecord(
                chat_key=chat_key,
                user_key=user_key,
                display_name=cleaned,
                aliases=[cleaned] if cleaned else [],
                familiarity=10.0,
                affection=5.0,
                trust=5.0,
                stage="acquaintance",
                interaction_days=1,
                updated_at=now_ts,
            )

        return RelationshipRecord(
            chat_key=chat_key,
            user_key=user_key,
            display_name=cleaned,
            aliases=[cleaned] if cleaned else [],
            familiarity=0.0,
            affection=0.0,
            trust=0.0,
            stage="stranger",
            interaction_days=0,
            updated_at=now_ts,
        )

    def get_or_create(
        self,
        chat_key: str,
        sender: str,
        *,
        sender_wxid: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
        persist: bool = True,
    ) -> RelationshipRecord:
        """按 wxid 优先查找记录；若存在旧 name: 记录且本次带 wxid，自动合并迁移。"""
        ukey = make_user_key(sender, sender_wxid)
        if not ukey:
            return RelationshipRecord(chat_key=chat_key, user_key="", display_name=_clean_sender_name(sender))

        rec = self.store.get(chat_key, ukey)
        if rec is not None:
            old_name = rec.display_name
            rec.add_alias(sender)
            if persist and rec.display_name != old_name:
                rec.updated_at = self.now()
                self.store.upsert(rec)
            return rec

        # 本次带 wxid 但 wxid 记录不存在：尝试把同群旧 name: 记录迁移过来
        if sender_wxid:
            legacy = self.store.find_by_alias(chat_key, sender, key_prefix="name:")
            if legacy is not None:
                old_key = legacy.user_key
                legacy.user_key = ukey
                legacy.add_alias(sender)
                legacy.updated_at = self.now()
                if persist:
                    self.store.migrate_key(chat_key, old_key, legacy)
                return legacy
        else:
            # 无 wxid 时尝试按昵称/OCR 变体匹配已有记录（含 wxid: 记录）
            matched = self.store.find_by_alias(chat_key, sender)
            if matched is not None:
                return matched

        rec = self._create_initial_record(chat_key, ukey, sender, history=history)
        if persist:
            self.store.upsert(rec)
        return rec

    def _reset_daily_counters_if_needed(self, rec: RelationshipRecord, today: str) -> None:
        if rec.last_interaction_date != today:
            rec.daily_familiarity_gain = 0.0
            rec.daily_affection_gain = 0.0
            rec.daily_trust_gain = 0.0

    def apply_event(
        self,
        chat_key: str,
        sender: str,
        event_type: str,
        *,
        sender_wxid: Optional[str] = None,
        intensity: float = 1.0,
        confidence: float = 1.0,
        summary: str = "",
        resolved: Optional[bool] = None,
        history: Optional[List[ChatMessage]] = None,
    ) -> Optional[RelationshipRecord]:
        """按程序规则结算单条关系事件并落库。"""
        if event_type not in EVENT_WEIGHTS:
            _logger.warning("[Relationship] 未知事件类型，忽略: %s", event_type)
            return None
        if confidence < MIN_EVENT_CONFIDENCE:
            return None

        rec = self.get_or_create(chat_key, sender, sender_wxid=sender_wxid, history=history, persist=True)
        if not rec.user_key:
            return None

        now_ts = self.now()
        today = _date_str(now_ts)
        self._reset_daily_counters_if_needed(rec, today)

        base_fam, base_aff, base_trust = EVENT_WEIGHTS[event_type]
        scale = max(0.0, min(1.0, float(intensity)))
        is_negative = event_type in NEGATIVE_EVENTS

        if is_negative:
            d_fam = base_fam * scale
            d_aff = base_aff * scale
            d_trust = base_trust * scale
            if event_type == "boundary_push":
                rec.guarded_until = max(rec.guarded_until or 0.0, now_ts + GUARDED_DURATION_FIRST)
                rec.guarded_reason = summary or "越界要求被拒"
            elif event_type == "repeated_annoyance":
                rec.guarded_until = max(rec.guarded_until or 0.0, now_ts + GUARDED_DURATION_REPEAT)
                rec.guarded_reason = summary or "被拒后反复纠缠"
            elif event_type == "rude_or_deception":
                rec.guarded_until = max(rec.guarded_until or 0.0, now_ts + GUARDED_DURATION_REPEAT)
                rec.guarded_reason = summary or "明显冒犯或欺骗"
            event_resolved = False if resolved is None else resolved
        else:
            # 24 小时同类正面事件收益递减
            recent_count = self.store.count_recent_events(
                chat_key, rec.user_key, event_type, now_ts - 86400.0
            )
            decay = REPEAT_DECAY_FACTORS[min(recent_count, len(REPEAT_DECAY_FACTORS) - 1)]
            raw_fam = base_fam * scale * decay
            raw_aff = base_aff * scale * decay
            raw_trust = base_trust * scale * decay

            # 每日正向总上限
            d_fam = min(raw_fam, max(0.0, DAILY_MAX_FAMILIARITY_GAIN - rec.daily_familiarity_gain))
            d_aff = min(raw_aff, max(0.0, DAILY_MAX_AFFECTION_GAIN - rec.daily_affection_gain))
            d_trust = min(raw_trust, max(0.0, DAILY_MAX_TRUST_GAIN - rec.daily_trust_gain))

            rec.daily_familiarity_gain += d_fam
            rec.daily_affection_gain += d_aff
            rec.daily_trust_gain += d_trust

            # 真诚道歉可缩短剩余警戒期（减半），但不直接清零严重信任损失
            if event_type == "sincere_apology" and rec.guarded_until and rec.guarded_until > now_ts:
                remaining = rec.guarded_until - now_ts
                rec.guarded_until = now_ts + remaining * 0.5
            event_resolved = True if resolved is None else resolved

        if rec.last_interaction_date != today:
            rec.interaction_days += 1
            rec.last_interaction_date = today

        rec.familiarity = _clamp(rec.familiarity + d_fam)
        rec.affection = _clamp(rec.affection + d_aff)
        rec.trust = _clamp(rec.trust + d_trust)
        rec.stage = compute_stage(
            rec.stage, rec.familiarity, rec.affection, rec.trust, rec.interaction_days
        )
        if event_type == "daily_chat":
            rec.last_daily_chat_at = now_ts
        rec.updated_at = now_ts

        self.store.upsert(rec)
        self.store.insert_event(
            chat_key,
            rec.user_key,
            event_type,
            d_fam,
            d_aff,
            d_trust,
            intensity=scale,
            confidence=confidence,
            summary=summary,
            resolved=event_resolved,
            created_at=now_ts,
        )
        return rec

    def _is_guarded(self, rec: RelationshipRecord) -> bool:
        now_ts = self.now()
        if rec.guarded_until is not None and rec.guarded_until > now_ts:
            return True
        if rec.trust < 10.0 and rec.user_key and self.store.has_unresolved_negative_event(rec.chat_key, rec.user_key):
            return True
        return False

    def describe_person(
        self,
        chat_key: str,
        sender: str,
        *,
        sender_wxid: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
    ) -> Tuple[str, str, str]:
        """返回 (person, level, rules)，不暴露任何具体分值。"""
        cleaned = _clean_sender_name(sender) or sender
        if is_buddy(cleaned):
            return cleaned, "buddy", STAGE_RULES["buddy"]

        rec = self.get_or_create(
            chat_key, cleaned, sender_wxid=sender_wxid, history=history, persist=False
        )
        if self._is_guarded(rec):
            return cleaned, "guarded", STAGE_RULES["guarded"]
        level = rec.stage if rec.stage in STAGE_RULES else "stranger"
        return cleaned, level, STAGE_RULES[level]

    def describe_relationships(
        self,
        unreplied: List[ChatMessage],
        history: List[ChatMessage],
        *,
        chat_name: str = "",
        max_people: int = 3,
    ) -> List[Tuple[str, str, str]]:
        """为本批未读中涉及的发言人（去重、最近发言在前，最多 max_people 人）返回关系描述。"""
        if not unreplied:
            return []
        selected: List[ChatMessage] = []
        for msg in reversed(unreplied):
            if msg.sender_type != SenderType.OTHER or not msg.sender:
                continue
            cleaned = _clean_sender_name(msg.sender)
            if not cleaned or cleaned in _UNKNOWN_SENDERS:
                continue
            if any(
                (msg.sender_wxid and msg.sender_wxid == prev.sender_wxid)
                or _same_person_name(cleaned, _clean_sender_name(prev.sender))
                for prev in selected
            ):
                continue
            selected.append(msg)
            if len(selected) >= max_people:
                break
        # 还原为按发言先后顺序展示
        selected.reverse()
        results: List[Tuple[str, str, str]] = []
        for msg in selected:
            ckey = make_chat_key(chat_name or msg.chat_name, getattr(msg, "chatroom_id", None))
            results.append(
                self.describe_person(
                    ckey,
                    msg.sender,
                    sender_wxid=getattr(msg, "sender_wxid", None),
                    history=history,
                )
            )
        return results

    @staticmethod
    def _reply_target_msg(reply: str, unreplied: List[ChatMessage]) -> Optional[ChatMessage]:
        """定位本条回复对应的未读消息对象，用于精准归属互动与越界警戒。"""
        other_msgs = [m for m in unreplied if m.sender_type == SenderType.OTHER and m.sender]
        if not other_msgs:
            return None
        m = _LEADING_AT_RE.match(reply or "")
        if m:
            target = m.group(1)
            if target not in _UNKNOWN_SENDERS:
                for msg in reversed(other_msgs):
                    if _same_person_name(_clean_sender_name(msg.sender), target):
                        return msg
        # 没有 @ 时，仅当本批未读只来自同一人时才归属到该人
        unique: List[ChatMessage] = []
        for msg in other_msgs:
            if not any(_same_person_name(_clean_sender_name(msg.sender), _clean_sender_name(u.sender)) for u in unique):
                unique.append(msg)
        return other_msgs[-1] if len(unique) == 1 else None

    def record_replies(
        self,
        chat_name: str,
        unreplied: List[ChatMessage],
        replies: List[str],
        *,
        chatroom_id: Optional[str] = None,
    ) -> None:
        """同步轨：在回复真正发出后记录基础 daily_chat（带最小间隔防刷分）与精准越界警戒。"""
        if not unreplied or not replies:
            return
        now_ts = self.now()
        handled_users: set = set()
        for reply in replies:
            target_msg = self._reply_target_msg(reply, unreplied)
            if target_msg is None:
                continue
            ckey = make_chat_key(chat_name or target_msg.chat_name, chatroom_id or getattr(target_msg, "chatroom_id", None))
            wxid = getattr(target_msg, "sender_wxid", None)
            ukey = make_user_key(target_msg.sender, wxid)
            if not ukey or ukey in handled_users:
                continue
            handled_users.add(ukey)

            sender_msgs = [
                m for m in unreplied
                if m.sender_type == SenderType.OTHER
                and (
                    (wxid and wxid == getattr(m, "sender_wxid", None))
                    or _same_person_name(_clean_sender_name(m.sender), _clean_sender_name(target_msg.sender))
                )
            ]
            asked_boundary = any(
                any(word in (m.text or "") for word in _BOUNDARY_ASKS)
                for m in sender_msgs
            )
            replied_refusal = any(word in (reply or "").replace(" ", "") for word in _REFUSALS)

            rec = self.get_or_create(ckey, target_msg.sender, sender_wxid=wxid, persist=True)
            if asked_boundary and replied_refusal:
                event_type = "repeated_annoyance" if self._is_guarded(rec) else "boundary_push"
                self.apply_event(
                    ckey,
                    target_msg.sender,
                    event_type,
                    sender_wxid=wxid,
                    summary="越界要求触发拒绝",
                )
            elif now_ts - rec.last_daily_chat_at >= self.daily_chat_interval:
                self.apply_event(
                    ckey,
                    target_msg.sender,
                    "daily_chat",
                    sender_wxid=wxid,
                    summary="常规有效互动",
                )
