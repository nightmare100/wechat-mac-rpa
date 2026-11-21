"""TraceMemo local HTTP receiver. UI is used only to locate/verify sends."""
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from src.models.base import ChatMessage, PerceptionResult, SenderType
from src.utils.chat_utils import _normalize_chat_name
from src.utils.identity import is_at_bot


class TraceMemoError(RuntimeError):
    """Safe diagnostic; never includes a response body or credentials."""


class TraceMemoClient:
    def __init__(self, base_url=None, token=None, timeout=10, opener=None):
        self.base_url = (base_url or os.getenv("TRACEMEMO_BASE_URL", "http://127.0.0.1:6131/api/v1")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TraceMemoError("TRACEMEMO_BASE_URL 必须是本机 HTTP API 地址")
        self._token = token or os.getenv("TRACEMEMO_API_TOKEN") or os.getenv("WECHATEXPLORER_API_TOKEN")
        if not self._token:
            raise TraceMemoError("请配置本机环境变量 TRACEMEMO_API_TOKEN")
        self.timeout = timeout
        self._opener = opener or urlopen

    def request(self, path, params=None):
        headers = {"Accept": "application/json"}
        if path != "/health":
            headers["Authorization"] = f"Bearer {self._token}"
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        try:
            with self._opener(Request(url, headers=headers), timeout=self.timeout) as response:
                data = json.loads(response.read())
            if not isinstance(data, dict):
                raise ValueError("invalid response")
            return data
        except HTTPError as exc:
            raise TraceMemoError(f"TraceMemo {path} HTTP {exc.code}（检查服务及 API Token）") from None
        except Exception:
            raise TraceMemoError(f"TraceMemo {path} 请求失败（检查本机服务及响应格式）") from None

    def read_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes:
        target = self._loopback_url(url)
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            with self._opener(Request(target, headers=headers), timeout=self.timeout) as response:
                data = response.read(max_bytes + 1) if max_bytes is not None else response.read()
            if max_bytes is not None and len(data) > max_bytes:
                raise TraceMemoError("TraceMemo 图片大小超限")
            if not isinstance(data, bytes) or not data:
                raise ValueError("empty media")
            return data
        except TraceMemoError:
            raise
        except Exception:
            raise TraceMemoError("TraceMemo 图片读取失败（检查本机服务及图片地址）") from None

    def _loopback_url(self, url: str) -> str:
        parsed = urlsplit(url)
        base = urlsplit(self.base_url)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise TraceMemoError("TraceMemo 图片地址必须是本机")
            return url
        if not url.startswith("/"):
            raise TraceMemoError("TraceMemo 图片地址无效")
        origin = f"{base.scheme}://{base.netloc}"
        if url.startswith("/api/"):
            return origin + url
        return self.base_url + url


class TraceMemoPipeline:
    source_name = "tracememo"
    merge_mode = "tracememo"
    OVERLAP_SECONDS = 120
    CONTEXT_SECONDS = 1800

    def __init__(self, profile=None, *, client=None, targets=None, clock=None, ui=None):
        self._clock = clock or time.time
        self.started_at = self._clock()
        self.client = client or TraceMemoClient()
        configured = targets if targets is not None else (os.getenv("TRACEMEMO_CHATS") or os.getenv("SILENT_WHITELIST", "")).split(",")
        configured = list(dict.fromkeys(t.strip() for t in configured if t.strip()))
        if not configured:
            raise TraceMemoError("请配置 TRACEMEMO_CHATS 或 SILENT_WHITELIST 指定监听群")
        health = self.client.request("/health")
        if not health.get("ok") or not health.get("ready"):
            raise TraceMemoError("TraceMemo 服务或数据库未就绪")
        version = health.get("skill", {}).get("version")
        if version and version != "1.4.0":
            raise TraceMemoError("TraceMemo API 契约版本已变化，请更新 Reader Skill 并核对接收适配器")
        groups = self.client.request("/chatroom").get("chatrooms")
        if not isinstance(groups, list):
            raise TraceMemoError("TraceMemo 群列表格式异常")
        self._targets = []
        for target in configured:
            matches = [g for g in groups if target in (g.get("wxid"), g.get("m_nsUsrName"), g.get("m_nsNickName"))]
            if len(matches) != 1:
                raise TraceMemoError(f"监听群 {target!r} 不存在或存在同名歧义，请核对群配置")
            group = matches[0]
            name = group.get("m_nsNickName", "")
            talker = group.get("wxid") or group.get("m_nsUsrName")
            # UI sends are located by display name, so even explicit IDs cannot
            # make two equal/normalized names safe to send to.
            if not name or not talker or sum(_normalize_chat_name(g.get("m_nsNickName", "")) == _normalize_chat_name(name) for g in groups) != 1:
                raise TraceMemoError("监听群存在同名歧义或缺少名称，无法安全定位发送窗口")
            if not any(t[0] == talker for t in self._targets):
                self._targets.append((talker, name))
        self._next = 0
        self._since = {talker: max(0, int(self.started_at) - self.CONTEXT_SECONDS) for talker, _ in self._targets}
        self._ui = ui
        self._profile = profile

    def _ui_component(self, name):
        if self._ui is None:
            from src.perception.vision_pipeline import VisionPipeline
            self._ui = VisionPipeline(self._profile)
        return getattr(self._ui, name)

    @property
    def capture(self):
        return self._ui_component("capture")

    @property
    def ocr(self):
        return self._ui_component("ocr")

    @property
    def layout(self):
        return self._ui_component("layout")

    def perceive(self):
        talker, name = self._targets[self._next]
        # A failing group must not starve other configured groups.
        self._next = (self._next + 1) % len(self._targets)
        requested_at = self._clock()
        messages = self._read_group_messages(talker, name, self._since[talker])
        self._since[talker] = max(self._since[talker], int(requested_at) - self.OVERLAP_SECONDS)
        return PerceptionResult(chat_name=name, messages=messages, chat_list_items=[], screenshot_path="", is_group=True)

    def _read_group_messages(self, talker: str, name: str, start_time: int) -> list[ChatMessage]:
        data = self.client.request("/chatlog", {"talker": talker, "startTime": start_time})
        rows = data.get("messages")
        if not isinstance(rows, list) or data.get("count", len(rows)) != len(rows):
            raise TraceMemoError("TraceMemo 消息响应不完整，不推进读取范围")
        try:
            messages = [self._message(row, talker, name) for row in rows]
        except (KeyError, ValueError, TypeError, AttributeError):
            raise TraceMemoError("TraceMemo 消息字段异常，不推进读取范围") from None
        messages.sort(key=lambda m: (m.create_time, m.local_id or 0))
        return messages

    def recent_group_messages(self, talker: str, start_time: int) -> list[ChatMessage]:
        """Read history independently of the normal polling cursor."""
        name = next((name for group_id, name in self._targets if group_id == talker), None)
        if name is None:
            raise TraceMemoError("群不在 TraceMemo 监听列表中")
        return self._read_group_messages(talker, name, start_time)

    def _message(self, row, talker, name):
        if row.get("sessionId") and row["sessionId"] != talker:
            raise ValueError("wrong conversation")
        created = int(row["createTime"])
        local_id = int(row["localId"]) if row.get("localId") is not None else None
        server_id = str(row.get("serverId") or "")
        if local_id is None and server_id in ("", "0"):
            raise ValueError("missing identity")
        kind = row.get("type", "")
        system = row.get("from") == "system" or kind == "系统消息"
        if not system and not isinstance(row.get("isSender"), bool):
            raise ValueError("missing sender direction")
        sender_type = SenderType.SYSTEM if system else SenderType.SELF if row.get("isSender") is True else SenderType.OTHER
        text = row.get("content") or ""
        structured = row.get("contentData") or {}
        quoted_text = ""
        if structured.get("type") == "quote":
            text = structured.get("title") or structured.get("content") or text
            quoted_content = structured.get("quotedContent") or "[引用消息]"
            # An explicit unknown author prevents a content-only match from
            # incorrectly granting the self-quote cooldown exemption.
            quoted_text = f"{structured.get('quotedSender') or '[未知]'}：{quoted_content}"
        message_type = {"图片": "image", "表情": "sticker", "动画表情": "sticker", "视频": "video", "语音": "voice", "文件": "file", "链接": "link_card"}.get(kind, "text")
        media = row.get("media") if isinstance(row.get("media"), dict) else {}
        media_url = media.get("url") if message_type == "image" and isinstance(media.get("url"), str) else ""
        if message_type in ("image", "sticker", "video"):
            text = f"[{kind}，内容未解析]"
        elif message_type == "voice":
            text = row.get("voiceTranscript") if row.get("voiceTranscriptState") == "transcribed" else "[语音，内容未转写]"
        return ChatMessage(text=text or "", sender="自己" if sender_type == SenderType.SELF else row.get("name") or "[未知]",
                           sender_type=sender_type, chat_name=name, chatroom_id=talker,
                           is_at_me=is_at_bot(text), timestamp=row.get("datetime"),
                           quoted_text=quoted_text,
                           replied=created < self.started_at or sender_type in (SenderType.SELF, SenderType.SYSTEM),
                           message_type=message_type, local_id=local_id, server_id=server_id,
                           source="tracememo", media_url=media_url,
                           create_time=created, sender_wxid=row.get("senderId"), raw_type=10000 if system else None)
