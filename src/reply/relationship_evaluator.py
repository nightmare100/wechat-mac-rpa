"""关系引擎 Phase 2：后台低频批量 LLM 事件评估器。

设计要点：
- 零主链路延迟：主回复只做内存入队，后台守护线程按 (chat_key, user_key) 防抖批量结算；
- 极省 token：仅对与 Bot 有真实来回回复的群友生效，满 batch_turns 轮或空闲满 idle_seconds 才打包调 1 次 LLM；
- 职责分离：LLM 只识别结构化事件（event_type / intensity / confidence / summary），最终增减分、24h 递减、每日封顶、阶段迟滞全部由 RelationshipEngine 强制执行；
- 安全降级：任何 LLM 异常、超时或脏 JSON 仅记录 warning，绝不影响主循环。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.models.base import ChatMessage, SenderType
from src.reply.relationship_store import (
    RelationshipEngine,
    _clean_sender_name,
    _same_person_name,
    make_chat_key,
    make_user_key,
)
from src.utils.json_extractor import extract_json

_logger = logging.getLogger("src.reply.relationship_evaluator")

# 允许由异步 LLM 识别的事件白名单（排除 daily_chat，避免与同步轨重复计分）
EVALUATOR_EVENT_TYPES = (
    "friendly_banter",
    "helpful_share",
    "sincere_care",
    "remembered_detail",
    "respect_boundary",
    "boundary_push",
    "repeated_annoyance",
    "rude_or_deception",
    "sincere_apology",
)
_EVALUATOR_EVENT_SET = set(EVALUATOR_EVENT_TYPES)

_SYSTEM_PROMPT = """你是一个微信群聊关系事件识别器。你的唯一任务是从给定的一段群友与 Bot 的对话片段中，判断是否发生了显著影响长期关系的事件。

允许输出的 event_type（仅限以下 9 种，禁止输出其他类型）：
- friendly_banter：轻松接梗、互相调侃、聊得很投机（须结合语境确认是友善玩笑而非冒犯）
- helpful_share：对方主动帮群友解围、分享攻略/经验或热心助人
- sincere_care：对方表达具体、真诚的关心或体贴提醒（机械夸奖/客套不算）
- remembered_detail：对方记得并准确提起此前已发生的真实细节（若对方在编造无来源共同经历，不得算此项）
- respect_boundary：Bot 表达边界或拒绝后，对方爽快收敛、理解并尊重边界
- sincere_apology：冲突或冒犯后，对方真诚道歉
- boundary_push：对方越界索要（如索要语音、照片、低俗暗示、强行要求）
- repeated_annoyance：被拒绝或要求停止后，仍反复纠缠同一要求
- rude_or_deception：明显恶意辱骂、人身攻击或蓄意欺骗

严格规则：
1. 普通问候、日常闲聊、单句客套、重复刷“你真可爱”等机械夸奖，一律返回 {"events": []}。
2. 必须有对话内容作为明确依据，不要脑补；最多输出 2 条最显著的事件，同一行为不要拆成多条重复计分。
3. 只输出一个合法 JSON 对象，不要解释、不要 Markdown 代码块。
格式：{"events":[{"event_type":"...","intensity":0.5,"confidence":0.8,"summary":"20字内中文依据"}]}"""


@dataclass
class _PendingBucket:
    chat_key: str
    chat_name: str
    user_key: str
    sender: str
    sender_wxid: Optional[str] = None
    turns: List[Tuple[str, str]] = field(default_factory=list)  # [(user_text, bot_reply), ...]
    last_updated_at: float = 0.0


def _extract_llm_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    return ""


class RelationshipEvaluator:
    """后台低频批量关系事件评估器。"""

    def __init__(
        self,
        engine: RelationshipEngine,
        llm_client: Any = None,
        *,
        batch_turns: int = 3,
        idle_seconds: float = 300.0,
        poll_interval: float = 30.0,
        max_turns_per_prompt: int = 6,
        llm_timeout: float = 15.0,
        now: Callable[[], float] = time.time,
    ):
        self.engine = engine
        self.llm_client = llm_client
        self.batch_turns = max(1, int(batch_turns))
        self.idle_seconds = max(1.0, float(idle_seconds))
        self.poll_interval = max(0.05, float(poll_interval))
        self.max_turns_per_prompt = max(1, int(max_turns_per_prompt))
        self.llm_timeout = float(llm_timeout)
        self.now = now

        self._lock = threading.Lock()
        self._buckets: Dict[Tuple[str, str], _PendingBucket] = {}
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.llm_client is None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="RelationshipEvaluator",
                daemon=True,
            )
            self._thread.start()

    def stop(self, flush: bool = False, timeout: float = 2.0) -> None:
        if flush:
            self.flush_due(force=True)
        self._stop_event.set()
        self._wake_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    def enqueue_replies(
        self,
        chat_name: str,
        unreplied: List[ChatMessage],
        replies: List[str],
        *,
        chatroom_id: Optional[str] = None,
    ) -> int:
        """将本次成功发出的回复按目标分桶入队，纯内存操作不阻塞主线程。返回入队轮数。"""
        if self.llm_client is None or not unreplied or not replies:
            return 0
        now_ts = self.now()
        enqueued = 0
        should_wake = False

        # 先按目标归组本批回复
        grouped: Dict[Tuple[str, str], Tuple[ChatMessage, List[str], List[str]]] = {}
        for reply in replies:
            target_msg = self.engine._reply_target_msg(reply, unreplied)
            if target_msg is None:
                continue
            ckey = make_chat_key(
                chat_name or target_msg.chat_name,
                chatroom_id or getattr(target_msg, "chatroom_id", None),
            )
            wxid = getattr(target_msg, "sender_wxid", None)
            ukey = make_user_key(target_msg.sender, wxid)
            if not ukey:
                continue
            key = (ckey, ukey)
            if key not in grouped:
                user_texts = [
                    (m.text or "").strip()[:200]
                    for m in unreplied
                    if m.sender_type == SenderType.OTHER
                    and (
                        (wxid and wxid == getattr(m, "sender_wxid", None))
                        or _same_person_name(_clean_sender_name(m.sender), _clean_sender_name(target_msg.sender))
                    )
                    and (m.text or "").strip()
                ]
                grouped[key] = (target_msg, user_texts, [])
            grouped[key][2].append((reply or "").strip()[:200])

        with self._lock:
            for (ckey, ukey), (target_msg, user_texts, bot_replies) in grouped.items():
                if not user_texts and not bot_replies:
                    continue
                bucket = self._buckets.get((ckey, ukey))
                if bucket is None:
                    bucket = _PendingBucket(
                        chat_key=ckey,
                        chat_name=chat_name or target_msg.chat_name,
                        user_key=ukey,
                        sender=_clean_sender_name(target_msg.sender) or target_msg.sender,
                        sender_wxid=getattr(target_msg, "sender_wxid", None),
                    )
                    self._buckets[(ckey, ukey)] = bucket
                else:
                    bucket.sender = _clean_sender_name(target_msg.sender) or bucket.sender
                    if getattr(target_msg, "sender_wxid", None):
                        bucket.sender_wxid = target_msg.sender_wxid

                user_summary = " / ".join(user_texts[-3:])
                bot_summary = " / ".join(bot_replies[-2:])
                bucket.turns.append((user_summary, bot_summary))
                if len(bucket.turns) > self.max_turns_per_prompt:
                    bucket.turns = bucket.turns[-self.max_turns_per_prompt:]
                bucket.last_updated_at = now_ts
                enqueued += 1
                if len(bucket.turns) >= self.batch_turns:
                    should_wake = True

        if should_wake:
            self._wake_event.set()
        return enqueued

    def pending_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _pop_due_buckets(self, *, force: bool = False) -> List[_PendingBucket]:
        now_ts = self.now()
        due: List[_PendingBucket] = []
        with self._lock:
            for key in list(self._buckets.keys()):
                bucket = self._buckets[key]
                if (
                    force
                    or len(bucket.turns) >= self.batch_turns
                    or (now_ts - bucket.last_updated_at >= self.idle_seconds)
                ):
                    due.append(self._buckets.pop(key))
        return due

    def flush_due(self, *, force: bool = False) -> int:
        """同步结算所有到期（达到轮数或空闲超时）的桶。返回成功入账的事件总数。"""
        if self.llm_client is None:
            return 0
        due_buckets = self._pop_due_buckets(force=force)
        applied_total = 0
        for bucket in due_buckets:
            applied_total += self._evaluate_bucket(bucket)
        return applied_total

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.flush_due(force=False)
            except Exception as e:
                _logger.warning("[RelationshipEvaluator] 后台结算异常: %s", e)
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()

    def _build_user_prompt(self, bucket: _PendingBucket, current_level: str) -> str:
        lines = [
            f"群聊：{bucket.chat_name}",
            f"群友：{bucket.sender}",
            f"当前关系阶段：{current_level}",
            "近期互动片段：",
        ]
        for idx, (u_text, b_text) in enumerate(bucket.turns[-self.max_turns_per_prompt:], 1):
            lines.append(f"{idx}. {bucket.sender}: {u_text}")
            lines.append(f"   Bot: {b_text}")
        return "\n".join(lines)

    @staticmethod
    def _parse_events(raw_text: str) -> List[Dict[str, Any]]:
        data = extract_json(raw_text or "")
        if not isinstance(data, dict):
            return []
        raw_events = data.get("events")
        if not isinstance(raw_events, list):
            return []

        deduped: Dict[str, Dict[str, Any]] = {}
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            ev_type = str(item.get("event_type") or "").strip()
            if ev_type not in _EVALUATOR_EVENT_SET:
                continue
            try:
                intensity = max(0.0, min(1.0, float(item.get("intensity", 0.5))))
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
            except (TypeError, ValueError):
                continue
            summary = str(item.get("summary") or "").strip()[:60]
            candidate = {
                "event_type": ev_type,
                "intensity": intensity,
                "confidence": confidence,
                "summary": summary,
            }
            prev = deduped.get(ev_type)
            if prev is None or (confidence * intensity) > (prev["confidence"] * prev["intensity"]):
                deduped[ev_type] = candidate

        # 每次批量结算最多采纳 2 条事件，防止过度放大
        ranked = sorted(
            deduped.values(),
            key=lambda x: x["confidence"] * x["intensity"],
            reverse=True,
        )
        return ranked[:2]

    def _evaluate_bucket(self, bucket: _PendingBucket) -> int:
        if not bucket.turns:
            return 0
        _, current_level, _ = self.engine.describe_person(
            bucket.chat_key, bucket.sender, sender_wxid=bucket.sender_wxid
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(bucket, current_level)},
        ]
        try:
            response = self.llm_client.chat(
                messages=messages,
                temperature=0.1,
                max_tokens=300,
                timeout=self.llm_timeout,
            )
            raw_text = _extract_llm_text(response)
        except Exception as e:
            _logger.warning("[RelationshipEvaluator] LLM 评估失败 (%s): %s", bucket.sender, e)
            return 0

        events = self._parse_events(raw_text)
        applied = 0
        for ev in events:
            rec = self.engine.apply_event(
                bucket.chat_key,
                bucket.sender,
                ev["event_type"],
                sender_wxid=bucket.sender_wxid,
                intensity=ev["intensity"],
                confidence=ev["confidence"],
                summary=ev["summary"],
            )
            if rec is not None:
                applied += 1
                _logger.info(
                    "[RelationshipEvaluator] %s (%s) -> %s (intensity=%.2f, conf=%.2f, stage=%s)",
                    bucket.sender,
                    bucket.chat_name,
                    ev["event_type"],
                    ev["intensity"],
                    ev["confidence"],
                    rec.stage,
                )
        return applied
