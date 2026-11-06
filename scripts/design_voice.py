#!/usr/bin/env python3
"""百炼「声音设计」：用一段文字描述直接生成一个新音色（不需要录音）。

用法（在 wechat-mac-rpa 目录下）：
    .venv/bin/python scripts/design_voice.py "年轻活泼的女性声音，20 岁左右，音色甜美清脆，语速偏快，适合游戏公会聊天"
    .venv/bin/python scripts/design_voice.py "温柔的女声…" --prefix sweet --preview "大家好呀，今晚的周活动别忘了兑换奖励哦"
    .venv/bin/python scripts/design_voice.py --list                 # 列出已设计的音色
    .venv/bin/python scripts/design_voice.py --delete <voice_id>    # 删除音色

生成后会把试听音频存到 data/voice_design/<voice_id>.wav 并用扬声器播放。
满意的话把打印出的 voice_id 填到 .env 的 TTS_VOICE_ID（TTS_MODEL 要与 --model 一致），
send_voice.py 就会用这个音色发语音。

说明（见 docs/sound-record.md）：
- 仅北京地域；创建音色免费，合成按目标模型计费；音色与声音复刻共用每账号 1000 个上限。
- 描述只支持中文或英文，≤500 字；描述性别、年龄、音高、语速、情绪、音色特点、适用场景，不要写「模仿某某」。
- 同一描述每次生成的声音会不同，可以多跑几次挑一个。
"""

import argparse
import base64
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
OUT_DIR = ROOT / "data" / "voice_design"

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

DEFAULT_PREVIEW = "大家好呀，我是囡囡，今晚的周活动快结束啦，记得去兑换奖励哦。"


def _call(input_: dict, parameters: dict | None = None) -> dict:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        sys.exit("❌ .env 缺少 DASHSCOPE_API_KEY")
    base = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    url = base.split("/compatible-mode")[0] + "/api/v1/services/audio/tts/customization"
    body = {"model": "voice-enrollment", "input": input_}
    if parameters:
        body["parameters"] = parameters
    with httpx.Client(transport=httpx.HTTPTransport(proxy=None), timeout=120) as http:
        r = http.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code != 200:
        sys.exit(f"❌ HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def main() -> int:
    p = argparse.ArgumentParser(description="用文字描述设计一个新音色")
    p.add_argument("prompt", nargs="?", help="音色描述（中文或英文，≤500 字）")
    p.add_argument("--preview", default=DEFAULT_PREVIEW, help="试听文本（15–200 字）")
    p.add_argument("--prefix", default="design", help="音色名前缀，仅字母数字，≤10 位")
    p.add_argument("--model", default=os.environ.get("TTS_MODEL") or "qwen-audio-3.1-tts-plus",
                   help="绑定的合成模型，默认取 .env 的 TTS_MODEL")
    p.add_argument("--lang", default="zh", choices=["zh", "en"], help="试听文本语言")
    p.add_argument("--no-play", action="store_true", help="只保存试听音频，不播放")
    p.add_argument("--list", action="store_true", help="列出已设计的音色")
    p.add_argument("--delete", metavar="VOICE_ID", help="删除指定音色")
    args = p.parse_args()

    if args.list:
        out = _call({"action": "list_voice", "page_size": 100, "page_index": 0})["output"]
        for v in out.get("voice_list", []):
            if "-vd-" in v.get("voice_id", ""):
                print(f"{v['voice_id']}  [{v.get('status')}]  {v.get('voice_prompt', '')}")
        return 0
    if args.delete:
        _call({"action": "delete_voice", "voice_id": args.delete})
        print(f"🗑  已删除 {args.delete}")
        return 0

    if not args.prompt:
        p.error("请提供音色描述")
    if len(args.prompt) > 500:
        p.error("音色描述不能超过 500 字")
    if not 15 <= len(args.preview) <= 200:
        p.error("试听文本需要 15–200 字")
    if not (args.prefix.isascii() and args.prefix.isalnum() and len(args.prefix) <= 10):
        p.error("--prefix 只能是字母数字，最多 10 位")

    print(f"设计中…（模型 {args.model}）")
    out = _call(
        {"action": "create_voice", "target_model": args.model, "voice_prompt": args.prompt,
         "preview_text": args.preview, "prefix": args.prefix, "language_hints": [args.lang]},
        {"sample_rate": 24000, "response_format": "wav"},
    )["output"]
    voice_id = out["voice_id"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wav = OUT_DIR / f"{voice_id}.wav"
    wav.write_bytes(base64.b64decode(out["preview_audio"]["data"]))
    print(f"✅ voice_id: {voice_id}\n   试听音频: {wav}")
    print(f"   要启用：.env 里设 TTS_MODEL={args.model} 和 TTS_VOICE_ID={voice_id}")
    if not args.no_play:
        subprocess.run(["afplay", str(wav)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
