"""关闭藏宝图时，Bot 不加载可选依赖、不消费藏宝图消息。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("setting", [None, "0", "false", "off", "1", "true"])
def test_treasure_switch_controls_dependency_loading_and_message_handling(setting):
    # 独立进程避免已缓存的模块掩盖启动时的 eager import。
    script = '''
import builtins
import os

original_import = builtins.__import__

class TreasureDependencyRequested(Exception):
    pass

def guarded_import(name, *args, **kwargs):
    if name == "src.bot.treasure_flow" or name.startswith("src.treasure_map"):
        raise TreasureDependencyRequested(name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from src.bot.wechat_bot import WeChatBot
from src.models.base import ChatMessage, SenderType

bot = WeChatBot.__new__(WeChatBot)
if os.environ.get("ENABLE_TREASURE_MAP") in ("1", "true"):
    try:
        bot._build_treasure_flow()
    except TreasureDependencyRequested:
        pass
    else:
        raise AssertionError("开启时应加载藏宝图处理模块")
else:
    bot.treasure_flow = bot._build_treasure_flow()
    assert bot.treasure_flow is None
    messages = [ChatMessage(
        text="藏宝图鉴别", sender="测试用户", sender_type=SenderType.OTHER,
        chat_name="测试群",
    )]
    assert bot._handle_treasure_flow("测试群", messages, 1) is False
    assert messages[0].replied is False
'''
    env = os.environ.copy()
    env.pop("ENABLE_TREASURE_MAP", None)
    if setting is not None:
        env["ENABLE_TREASURE_MAP"] = setting
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
