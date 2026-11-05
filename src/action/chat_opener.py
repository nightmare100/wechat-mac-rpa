#!/usr/bin/env python3
"""L4 Action - 通过微信搜索打开指定聊天，并用本地 OCR 校验标题。"""

import logging
import time
from typing import Optional

from src.action.system_automation import MacOSSystemAutomation, SystemAutomation
from src.utils.chat_utils import _normalize_chat_name

_logger = logging.getLogger("src.chat_opener")

# 本地 OCR 偶尔漏掉标题首字或尾字（如「弓箭2战神公会」读成「箭2战神公会」），
# 群名至少这么长时，允许少开头或结尾 1 个字；中间的字必须完全一致，防止发到名字相近的群。
FUZZY_MIN_LEN = 5


def title_matches(ocr_title: str, chat_name: str) -> bool:
    title = _normalize_chat_name(ocr_title)
    target = _normalize_chat_name(chat_name)
    if not title or not target:
        return False
    if title == target:
        return True
    return len(target) >= FUZZY_MIN_LEN and title in (target[1:], target[:-1])


class ChatOpener:
    """⌘F 搜索 → 粘贴聊天名 → 回车 → OCR 读标题确认。标题对不上一律视为失败，宁可不发也不发错。"""

    def __init__(self, perception, automation: Optional[SystemAutomation] = None):
        # perception 需提供 capture / ocr / layout（SmartPerceptionPipeline、VisionPipeline 均满足）
        self.perception = perception
        self.automation = automation or MacOSSystemAutomation()

    def _titles(self) -> list[str]:
        """本地 OCR 识别窗口标题区文字（不调用百炼 API）。"""
        capture_result = self.perception.capture.capture()
        elements = self.perception.ocr.recognize(capture_result.image_path)
        layout = self.perception.layout.parse(elements, capture_result.image_path)
        return [t for t in [layout.chat_name or ""] + [e.text for e in layout.title_elements] if t]

    def is_current(self, chat_name: str) -> bool:
        titles = self._titles()
        matched = any(title_matches(t, chat_name) for t in titles)
        _logger.info(f"[ChatOpener] 标题校验 target='{chat_name}' titles={titles} matched={matched}")
        return matched

    def open(self, chat_name: str) -> bool:
        """打开聊天并校验，成功返回 True。"""
        if self.is_current(chat_name):
            return True

        ok, original_clipboard = self.automation.get_clipboard_text()
        try:
            self.automation.activate_app("WeChat")
            time.sleep(0.5)
            self.automation.send_keys('keystroke "f" using command down')
            time.sleep(0.5)
            self.automation.set_clipboard_text(chat_name)
            self.automation.send_keys('keystroke "v" using command down')
            time.sleep(1.5)  # 等待搜索结果
            self.automation.send_keys("keystroke return")
            time.sleep(1.5)  # 等待会话打开
        finally:
            if ok:
                self.automation.set_clipboard_text(original_clipboard)

        if self.is_current(chat_name):
            _logger.info(f"[ChatOpener] 已打开聊天: {chat_name}")
            return True
        _logger.warning(f"[ChatOpener] 打开聊天失败，标题不匹配: {chat_name}")
        self.automation.send_keys("key code 53")  # Esc，关闭可能残留的搜索框
        return False
