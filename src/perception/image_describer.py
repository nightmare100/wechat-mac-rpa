"""按需单图视觉理解（仅针对目标发送者所发图片）。

核心约束：
- 直接触发（@我 / 提到我 / 引用我 / followup 接话）在生成前按需调用；
- 主动插话（chime-in）仅在首轮决定要插话且未在冷却中后，对插话目标按需调用并二次投喂；
- 复用 `data/game_knowledge.md` 第三节（视觉与数值对照）作为参考词典；
- 按消息唯一标识做有界内存缓存，失败或拉图 404 时静默降级，不阻塞主聊天链路。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple, Union

from src.models.base import ChatMessage, SenderType
from src.utils.json_extractor import extract_json

_logger = logging.getLogger("src.perception.image_describer")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_KB_REL_PATH = Path("data") / "game_knowledge.md"
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
_DEFAULT_MAX_SIDE = 1024
_DEFAULT_CACHE_SIZE = 500
_DEFAULT_TIMEOUT_SECONDS = 12.0

_BASE_VISION_PROMPT = """你是一个微信群聊图片内容转述助手。请简洁、客观地描述这张图片的内容，并提取图片里最关键的文字或数值。

要求：
1. 只输出严格 JSON，不要 markdown 代码块或额外解释：
   {"description": "一句话客观描述画面主体、场景或情绪（20~60字）", "text": "图上关键文字/数值，没有则留空字符串"}
2. 隐私保护：如果图片包含裸露、暴力等不适宜内容，返回 {"description": "[图片]", "text": ""}。
3. 严禁脑补未显示的信息；认不出具体装备/角色名时只描述颜色、图标特征和可见文字。
4. 不要给出攻略评价、强度打分或抽卡建议。
"""


def _is_enabled_env() -> bool:
    return os.environ.get("ENABLE_IMAGE_UNDERSTANDING", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _resolve_kb_path(path: Optional[Union[str, Path]] = None) -> Path:
    if path is None:
        raw = os.environ.get("GAME_KNOWLEDGE_PATH", "").strip()
        p = Path(raw) if raw else _DEFAULT_KB_REL_PATH
    else:
        p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def extract_visual_knowledge_section(markdown_text: str) -> str:
    """提取 `game_knowledge.md` 中“视觉与数值对照”大节（供 VLM 参考）。"""
    if not markdown_text or not markdown_text.strip():
        return ""
    lines: List[str] = []
    in_section = False
    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            if in_section:
                break
            if "视觉" in heading or "数值对照" in heading or "VLM" in heading:
                in_section = True
            continue
        if in_section:
            lines.append(raw_line)
    return "\n".join(lines).strip()


def compress_image_to_jpeg(
    raw_bytes: bytes,
    *,
    max_side: int = _DEFAULT_MAX_SIDE,
    quality: int = 85,
) -> Tuple[bytes, str]:
    """将图片压缩至长边不超过 max_side 的 JPEG；Pillow 不可用或解码失败时原样回退。"""
    if not raw_bytes:
        return b"", "image/jpeg"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[3])
                work = bg
            elif img.mode != "RGB":
                work = img.convert("RGB")
            else:
                work = img.copy()

            w, h = work.size
            longest = max(w, h)
            if max_side > 0 and longest > max_side:
                scale = max_side / float(longest)
                work = work.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

            buf = io.BytesIO()
            work.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        # 兜底：根据魔数判断 MIME，避免非标准图片直接崩溃
        if raw_bytes.startswith(b"\x89PNG"):
            return raw_bytes, "image/png"
        if raw_bytes.startswith(b"GIF8"):
            return raw_bytes, "image/gif"
        if raw_bytes.startswith(b"RIFF") and b"WEBP" in raw_bytes[:16]:
            return raw_bytes, "image/webp"
        return raw_bytes, "image/jpeg"


def _norm_sender(name: str) -> str:
    return (name or "").split(" @")[0].strip().lower()


def _sender_matches(msg: ChatMessage, target_wxids: set[str], target_names: set[str]) -> bool:
    wxid = (getattr(msg, "sender_wxid", None) or "").strip()
    if wxid and wxid in target_wxids:
        return True
    name = _norm_sender(getattr(msg, "sender", "") or "")
    return bool(name and name in target_names)


def _msg_cache_key(msg: ChatMessage) -> str:
    server_id = str(getattr(msg, "server_id", "") or "").strip()
    if server_id and server_id != "0":
        return f"srv:{server_id}"
    chatroom_id = str(getattr(msg, "chatroom_id", "") or "").strip()
    local_id = getattr(msg, "local_id", None)
    if chatroom_id and local_id is not None:
        return f"loc:{chatroom_id}:{local_id}"
    media_url = str(getattr(msg, "media_url", "") or "").strip()
    if media_url:
        return f"url:{media_url}"
    return f"obj:{id(msg)}"


def _msg_stamp(msg: ChatMessage) -> Optional[float]:
    ct = getattr(msg, "create_time", None)
    if ct is not None:
        try:
            return float(ct)
        except (TypeError, ValueError):
            return None
    return None


def select_target_images(
    to_reply: Sequence[ChatMessage],
    all_messages: Sequence[ChatMessage],
    target_messages: Sequence[ChatMessage],
    *,
    max_images: int = 2,
    lookback_seconds: int = 120,
) -> List[ChatMessage]:
    """筛选属于 target 发送者的未解析图片（本批未读 + 近 2 分钟内该 target 的前序图片）。"""
    if max_images <= 0 or not target_messages:
        return []

    target_wxids: set[str] = set()
    target_names: set[str] = set()
    latest_target_ts: Optional[float] = None
    for tm in target_messages:
        wxid = (getattr(tm, "sender_wxid", None) or "").strip()
        if wxid:
            target_wxids.add(wxid)
        name = _norm_sender(getattr(tm, "sender", "") or "")
        if name:
            target_names.add(name)
        ts = _msg_stamp(tm)
        if ts is not None and (latest_target_ts is None or ts > latest_target_ts):
            latest_target_ts = ts

    if not target_wxids and not target_names:
        return []

    def _is_eligible_image(m: ChatMessage) -> bool:
        return (
            getattr(m, "sender_type", None) == SenderType.OTHER
            and getattr(m, "message_type", "") == "image"
            and bool(getattr(m, "media_url", ""))
            and not bool((getattr(m, "image_description", "") or "").strip())
            and _sender_matches(m, target_wxids, target_names)
        )

    candidates: List[ChatMessage] = []
    seen_keys: set[str] = set()

    def _add(m: ChatMessage) -> None:
        key = _msg_cache_key(m)
        if key not in seen_keys:
            seen_keys.add(key)
            candidates.append(m)

    # 1) 当前未读批次里 target 发的图片
    for m in to_reply:
        if _is_eligible_image(m):
            _add(m)

    # 2) 回看 all_messages 里同一 target 在近 lookback_seconds 内的前序图片（先发图再 @ 问）
    if lookback_seconds > 0 and all_messages:
        to_reply_ids = {id(m) for m in to_reply}
        for m in reversed(all_messages):
            if id(m) in to_reply_ids:
                continue
            if not _is_eligible_image(m):
                continue
            if latest_target_ts is not None:
                m_ts = _msg_stamp(m)
                if m_ts is not None:
                    delta = latest_target_ts - m_ts
                    if delta < 0 or delta > lookback_seconds:
                        continue
            _add(m)

    if len(candidates) <= max_images:
        candidates.sort(key=lambda m: (_msg_stamp(m) or 0.0, getattr(m, "local_id", None) or 0))
        return candidates

    # 超过上限时保留最新的 max_images 张，再按时间正序返回
    candidates.sort(key=lambda m: (_msg_stamp(m) or 0.0, getattr(m, "local_id", None) or 0))
    return candidates[-max_images:]


class ImageDescriber:
    """轻量单图视觉描述器（DashScope 兼容接口 + 有界缓存 + 热加载视觉词典）。"""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        kb_path: Optional[Union[str, Path]] = None,
        enabled: Optional[bool] = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_side: int = _DEFAULT_MAX_SIDE,
        cache_size: int = _DEFAULT_CACHE_SIZE,
        client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._kb_path = kb_path
        self._enabled_override = enabled
        self._timeout = timeout
        self._max_side = max_side
        self._cache_size = max(1, cache_size)
        self._client = client

        self._cache: OrderedDict[str, Tuple[str, str]] = OrderedDict()
        self._kb_cached_path: Optional[Path] = None
        self._kb_cached_stat: Optional[Tuple[int, int]] = None
        self._kb_cached_section: str = ""

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return _is_enabled_env()

    @property
    def model(self) -> str:
        return self._model or os.environ.get("VISION_MODEL", "qwen3.6-flash").strip() or "qwen3.6-flash"

    def _get_visual_knowledge(self) -> str:
        target = _resolve_kb_path(self._kb_path)
        try:
            st = target.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._kb_cached_path = target
            self._kb_cached_stat = None
            self._kb_cached_section = ""
            return ""
        if target == self._kb_cached_path and sig == self._kb_cached_stat:
            return self._kb_cached_section
        try:
            content = target.read_text(encoding="utf-8")
            self._kb_cached_section = extract_visual_knowledge_section(content)
            self._kb_cached_path = target
            self._kb_cached_stat = sig
        except OSError:
            self._kb_cached_section = ""
            self._kb_cached_stat = None
        return self._kb_cached_section

    def _build_prompt(self) -> str:
        kb_section = self._get_visual_knowledge()
        if not kb_section:
            return _BASE_VISION_PROMPT
        return f"{_BASE_VISION_PROMPT}\n参考背景（仅在图片确实属于《弓箭传说2》游戏截图时参考）：\n{kb_section}\n"

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        api_key = self._api_key or os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY not set")
        import httpx
        from openai import OpenAI

        http_client = httpx.Client(
            transport=httpx.HTTPTransport(proxy=None),
            timeout=self._timeout,
        )
        self._client = OpenAI(
            api_key=api_key,
            base_url=self._base_url
            or os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            http_client=http_client,
        )
        return self._client

    def describe_bytes(self, raw_bytes: bytes) -> Tuple[str, str]:
        """调用 VLM 识别单张图片字节流，返回 (description, text)。"""
        if not raw_bytes:
            return "", ""
        compressed, mime = compress_image_to_jpeg(raw_bytes, max_side=self._max_side)
        b64 = base64.b64encode(compressed).decode("utf-8")
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._build_prompt()},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
            temperature=0.0,
            max_tokens=300,
            extra_body={"enable_thinking": False},
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = extract_json(raw) or {}
        desc = str(parsed.get("description") or "").strip()
        text = str(parsed.get("text") or "").strip()
        if not desc and raw and not raw.startswith("{"):
            desc = raw[:120]
        return desc, text

    def get_cached(self, msg: ChatMessage) -> Optional[Tuple[str, str]]:
        key = _msg_cache_key(msg)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put_cache(self, msg: ChatMessage, desc: str, text: str) -> None:
        key = _msg_cache_key(msg)
        self._cache[key] = (desc, text)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def enrich_messages(
        self,
        chat_name: str,
        to_reply: Sequence[ChatMessage],
        all_messages: Sequence[ChatMessage],
        target_messages: Sequence[ChatMessage],
        *,
        reader: Callable[[str], bytes],
        max_images: int = 2,
        lookback_seconds: int = 120,
    ) -> int:
        """为目标发送者的未解析图片补全 image_description 与 image_text，返回成功解析数。"""
        selected = select_target_images(
            to_reply,
            all_messages,
            target_messages,
            max_images=max_images,
            lookback_seconds=lookback_seconds,
        )
        if not selected:
            return 0

        t0 = time.time()
        resolved = 0
        senders: List[str] = []
        for msg in selected:
            cached = self.get_cached(msg)
            if cached is not None:
                desc, text = cached
                if desc:
                    msg.image_description = desc
                    msg.image_text = text
                    resolved += 1
                continue
            try:
                raw_bytes = reader(msg.media_url)
                if not raw_bytes or len(raw_bytes) > _MAX_IMAGE_BYTES:
                    continue
                desc, text = self.describe_bytes(raw_bytes)
                if desc:
                    msg.image_description = desc
                    msg.image_text = text
                    self.put_cache(msg, desc, text)
                    resolved += 1
                    if msg.sender and msg.sender not in senders:
                        senders.append(msg.sender)
            except Exception as exc:
                _logger.warning(
                    "[ImageVision] 图片识别失败已降级 (chat=%s, sender=%s): %s",
                    chat_name or "-",
                    msg.sender or "-",
                    exc,
                )
        if resolved > 0:
            elapsed_ms = (time.time() - t0) * 1000
            _logger.info(
                "[ImageVision] chat=%s senders=%s imgs=%d elapsed=%.0fms",
                chat_name or "-",
                senders or [m.sender for m in selected],
                resolved,
                elapsed_ms,
            )
        return resolved


__all__ = [
    "ImageDescriber",
    "compress_image_to_jpeg",
    "extract_visual_knowledge_section",
    "select_target_images",
]
