#!/usr/bin/env python3
"""在微信当前聊天窗口发送一条语音（百炼复刻声音）。

用法（在 wechat-mac-rpa 目录下）：
    .venv/bin/python scripts/send_voice.py "周活动快结束啦" --mood 开心
    .venv/bin/python scripts/send_voice.py "测试一下" --mood 撒娇 --dry-run   # 只合成并用扬声器播放，不发微信
    .venv/bin/python scripts/send_voice.py --list                            # 列出可选心情

发送前请先在微信里打开目标聊天窗口；发送期间不要碰键盘鼠标。
"""

import argparse
import os
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from src.action.voice_sender import DEFAULT_MOOD, MOODS, VoiceSender, wav_seconds  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="在微信当前聊天窗口发送一条语音")
    p.add_argument("text", nargs="?", help="要说的话")
    p.add_argument("--mood", default=DEFAULT_MOOD, help=f"心情，默认{DEFAULT_MOOD}")
    p.add_argument("--dry-run", action="store_true", help="只合成并用扬声器播放，不发微信")
    p.add_argument("--list", action="store_true", help="列出可选心情")
    args = p.parse_args()

    if args.list:
        for name, (tag, ins) in MOODS.items():
            print(f"{name:<4} 标签={tag or '—':<16} 语气={ins or '—'}")
        return 0
    if not args.text:
        p.error("请提供要说的话")
    if args.mood not in MOODS:
        p.error(f"未知心情「{args.mood}」，可选：{'、'.join(MOODS)}")

    sender = VoiceSender()
    if args.dry_run:
        data = sender.synthesize(args.text, args.mood)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
        print(f"合成完成 {wav_seconds(data):.1f}s，扬声器播放中…")
        subprocess.run(["afplay", f.name], check=False)
        Path(f.name).unlink(missing_ok=True)
        return 0

    result = sender.send(args.text, args.mood)
    print("✅ 已发送" if result.success else f"❌ 发送失败：{result.error}")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
