"""藏宝图指令：先于关键词流程，按人和群各自接收接下来的 3 张图片。"""
import re
from dataclasses import dataclass
from pathlib import Path

from src.models.base import ActionResult, ChatMessage, SenderType
from src.treasure_map.models import CoverTemplateError, CoveredTreasureMapError, TreasureMapError
from src.utils.chat_utils import _normalize_chat_name

COMMANDS = {"藏宝图鉴别": "match", "藏宝图添加": "add"}
IMAGE_SLOTS = 3
MIN_CLOSEST_MATCH_PERCENT = 70
_ACK = {
    "match": "收到，请发送接下来的 3 张藏宝图",
    "add": "收到，请发送接下来的 3 张要收录的藏宝图",
}


@dataclass
class _Session:
    mode: str
    remaining: int
    after: tuple[int, int]


def _delivered(result: ActionResult) -> bool:
    return bool(result.success or result.skipped or (result.error or "").startswith("静默模式"))


def _stamp(message: ChatMessage) -> tuple[int, int]:
    return (message.create_time or 0, message.local_id or 0)


def _person(chat_name: str, message: ChatMessage) -> tuple[str, str]:
    chat = message.chatroom_id or _normalize_chat_name(chat_name)
    sender = message.sender_wxid or message.sender
    return (chat, sender)


def _command(message: ChatMessage) -> str | None:
    if message.sender_type != SenderType.OTHER or message.message_type not in ("text", ""):
        return None
    body = re.sub(r"@[^\s\u2005]+", "", message.text or "")
    body = re.sub(r"\s+", "", body)
    return COMMANDS.get(body)


class TreasureFlow:
    def __init__(self, library, fetch_image, send_text, send_image, mark_replied):
        self.library = library
        self.fetch_image = fetch_image
        self.send_text = send_text
        self.send_image = send_image
        self.mark_replied = mark_replied
        self.sessions: dict[tuple[str, str], _Session] = {}

    def handle(self, chat_name: str, messages: list[ChatMessage]) -> bool:
        handled = False
        for message in sorted(messages, key=_stamp):
            command = _command(message)
            key = _person(chat_name, message)
            if command:
                self.sessions[key] = _Session(command, IMAGE_SLOTS, _stamp(message))
                if not self._deliver_text(chat_name, _ACK[command]):
                    return True
                self.mark_replied(chat_name, message, _ACK[command])
                handled = True
                continue
            session = self.sessions.get(key)
            if session is None or message.message_type != "image" or _stamp(message) <= session.after:
                continue
            if not self._consume_image(chat_name, message, session):
                return True
            handled = True
            if session.remaining <= 0:
                self.sessions.pop(key, None)
        return handled

    def _consume_image(self, chat_name: str, message: ChatMessage, session: _Session) -> bool:
        fetched = self.fetch_image(message)
        path = Path(fetched) if fetched else None
        try:
            if path is None:
                reply = "这张图片读不到"
                sent = self._deliver_text(chat_name, reply)
            elif session.mode == "match":
                reply, sent = self._match(chat_name, path)
            else:
                reply, sent = self._add(chat_name, path)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        if not sent:
            return False
        self.mark_replied(chat_name, message, reply)
        session.remaining -= 1
        return True

    def _match(self, chat_name: str, path) -> tuple[str, bool]:
        try:
            result = self.library.match(path)
        except CoverTemplateError as exc:
            reply = str(exc)
            return reply, self._deliver_text(chat_name, reply)
        except TreasureMapError:
            reply = "这张藏宝图处理失败"
            return reply, self._deliver_text(chat_name, reply)
        if result.status == "found" and result.path is not None:
            return "匹配成功", self._deliver_image(chat_name, result.path)
        # 逐格比较中，inliers 是吻合格数，good_matches 是已翻开格数。
        similarity = result.similarity
        if result.path is None or similarity is None or similarity.good_matches <= 0:
            return "未匹配成功", self._deliver_text(chat_name, "未匹配成功")
        matched = similarity.inliers
        opened = similarity.good_matches
        if matched * 100 <= opened * MIN_CLOSEST_MATCH_PERCENT:
            return "未匹配成功", self._deliver_text(chat_name, "未匹配成功")
        percent = f"{matched / opened * 100:.1f}".removesuffix(".0")
        note = f"最相似，未匹配成功（匹配 {matched}/{opened} 格，{percent}%）"
        if not self._deliver_text(chat_name, note):
            return note, False
        return note, self._deliver_image(chat_name, result.path)

    def _add(self, chat_name: str, path) -> tuple[str, bool]:
        try:
            result = self.library.add(path)
        except (CoveredTreasureMapError, CoverTemplateError) as exc:
            reply = str(exc)
            return reply, self._deliver_text(chat_name, reply)
        except TreasureMapError:
            reply = "这张藏宝图处理失败"
            return reply, self._deliver_text(chat_name, reply)
        reply = "已重复" if result.status == "duplicate" else "已加入"
        return reply, self._deliver_text(chat_name, reply)

    def _deliver_text(self, chat_name: str, text: str) -> bool:
        return _delivered(self.send_text(chat_name, text))

    def _deliver_image(self, chat_name: str, image_path) -> bool:
        return _delivered(self.send_image(chat_name, image_path))
