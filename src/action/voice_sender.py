"""发送微信语音条：百炼复刻声音合成 → 播放到 BlackHole → 按住右 Option 让微信录音 → 松开发送。

链路验证与原理见 docs/sound-record.md。只发到微信**当前打开的聊天窗口**，不切换会话。

配置（.env，复用项目现有百炼配置）：
    DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL   百炼鉴权与业务空间地址
    TTS_MODEL / TTS_VOICE_ID                 复刻声音绑定的模型与 voice_id
    VOICE_INPUT_DEVICE                       虚拟声卡名，默认 "BlackHole 2ch"
"""

import io
import logging
import os
import re
import subprocess  # nosec B404
import tempfile
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import httpx

from src.models.base import ActionResult

_logger = logging.getLogger("src.voice_sender")

# 心情 → (控制类标签, instruction)。标签写在文本开头，instruction 放在请求 input 里。
MOODS: dict[str, tuple[Optional[str], Optional[str]]] = {
    "平静": (None, None),
    "开心": ("[excited]", "语气开心、轻快"),
    "撒娇": (None, "撒娇的语气，声音软软的"),
    "调皮": ("[mischievously]", "调皮、俏皮的语气"),
    "好奇": ("[curious]", "好奇地问"),
    "惊讶": ("[amazed]", "惊讶的语气"),
    "难过": ("[sad]", "有点难过、低落"),
    "生气": ("[angry]", "有点生气，但不凶"),
    "疲惫": ("[tired]", "有气无力、很累"),
    "安慰": ("[empathetic]", "温柔地安慰对方"),
    "不情愿": ("[reluctantly]", "不太情愿的语气"),
    "悄悄话": ("[whispers]", "小声说悄悄话"),
}
DEFAULT_MOOD = "平静"
MAX_SECONDS = 60.0          # 微信单条语音上限
PRE_ROLL = 1.5              # 按下 Option 后等多久再播放，避免吞掉开头
POST_ROLL = 0.5             # 播完后等多久再松开
RIGHT_OPTION_KEYCODE = 61
_NX_DEVICERALTKEYMASK = 0x40  # 右 Alt 的设备位；微信只认右 Option


# 可调参数 → 取值范围（百炼 HTTP 接口文档）
RANGES = {"rate": (0.5, 2.0), "volume": (0, 100)}


def build_tts_input(
    text: str, mood: str, voice_id: str,
    rate: Optional[float] = None, volume: Optional[int] = None, style: Optional[str] = None,
) -> dict:
    """组装合成请求的 input：控制类标签加在文本开头，instruction = 心情语气 + style。"""
    if mood not in MOODS:
        raise ValueError(f"未知心情「{mood}」，可选：{'、'.join(MOODS)}")
    tag, instruction = MOODS[mood]
    body = {"text": f"{tag}{text}" if tag else text, "voice": voice_id, "format": "wav", "sample_rate": 24000}
    instruction = "；".join(x for x in (instruction, style) if x)
    if instruction:
        body["instruction"] = instruction
    for name, value in (("rate", rate), ("volume", volume)):
        if value is None:
            continue
        lo, hi = RANGES[name]
        if not lo <= value <= hi:
            raise ValueError(f"{name} 取值范围 {lo}–{hi}，当前 {value}")
        body[name] = value
    return body


def wav_seconds(data: bytes) -> float:
    """按实际字节数算时长：百炼返回的 wav 头里长度字段是流式占位值（约 2GB），不能用 getnframes()。"""
    with wave.open(io.BytesIO(data)) as w:
        bytes_per_sec = w.getframerate() * w.getnchannels() * w.getsampwidth()
    start = data.find(b"data") + 8
    return max(0, len(data) - start) / float(bytes_per_sec)


def find_audiotoolbox_index(device_name: str, run: Callable = subprocess.run) -> Optional[int]:
    """按设备名查 ffmpeg audiotoolbox 输出设备序号（新增或拔出设备后序号会变，不能写死）。"""
    out = run(
        ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "anullsrc", "-t", "0.05",
         "-f", "audiotoolbox", "-list_devices", "true", "-"],
        capture_output=True, text=True, timeout=15,
    )
    for line in (out.stderr or "").splitlines():
        m = re.search(r"\[(\d+)\]\s+(.+?),", line)
        if m and m.group(2).strip() == device_name:
            return int(m.group(1))
    return None


def post_right_option(down: bool) -> None:
    import Quartz as Q
    ev = Q.CGEventCreateKeyboardEvent(None, RIGHT_OPTION_KEYCODE, down)
    Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
    Q.CGEventSetFlags(ev, (Q.kCGEventFlagMaskAlternate | _NX_DEVICERALTKEYMASK) if down else 0)
    Q.CGEventPost(Q.kCGHIDEventTap, ev)


class VoiceSender:
    def __init__(
        self,
        run: Callable = subprocess.run,
        key: Callable[[bool], None] = post_right_option,
        sleep: Callable[[float], None] = time.sleep,
        http: Optional[httpx.Client] = None,
    ):
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        base = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.tts_url = base.split("/compatible-mode")[0] + "/api/v1/services/audio/tts/SpeechSynthesizer"
        self.model = os.environ.get("TTS_MODEL", "")
        self.voice_id = os.environ.get("TTS_VOICE_ID", "")
        self.device = os.environ.get("VOICE_INPUT_DEVICE", "BlackHole 2ch")
        self.run, self.key, self.sleep = run, key, sleep
        # 与项目现有百炼客户端一致：不走本地代理
        self.http = http or httpx.Client(transport=httpx.HTTPTransport(proxy=None), timeout=60)

    # ---- 合成 ----
    def synthesize(self, text: str, mood: str = DEFAULT_MOOD, **tune) -> bytes:
        """tune：可选 rate / volume / style，见 build_tts_input。"""
        if not (self.api_key and self.model and self.voice_id):
            raise RuntimeError("缺少配置：DASHSCOPE_API_KEY / TTS_MODEL / TTS_VOICE_ID")
        r = self.http.post(
            self.tts_url,
            json={"model": self.model, "input": build_tts_input(text, mood, self.voice_id, **tune)},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:300]}")
        url = r.json()["output"]["audio"]["url"]
        data = self.http.get(url).content  # OSS 签名链接：不能带 Authorization 头
        if data[:4] != b"RIFF":
            raise RuntimeError(f"下载的音频不是 wav: {data[:120]!r}")
        return data

    # ---- 发送 ----
    def _current_input(self) -> str:
        return self.run(["SwitchAudioSource", "-c", "-t", "input"],
                        capture_output=True, text=True, timeout=5).stdout.strip()

    def _set_input(self, name: str) -> bool:
        return self.run(["SwitchAudioSource", "-t", "input", "-s", name],
                        capture_output=True, text=True, timeout=5).returncode == 0

    def send_wav(self, data: bytes) -> ActionResult:
        """把 wav 作为语音条发到微信当前聊天窗口。"""
        seconds = wav_seconds(data)
        if seconds > MAX_SECONDS - PRE_ROLL - POST_ROLL:
            return ActionResult(success=False, error=f"语音 {seconds:.1f}s 太长，微信单条上限 60s")
        index = find_audiotoolbox_index(self.device, self.run)
        if index is None:
            return ActionResult(success=False, error=f"找不到音频设备「{self.device}」，请确认已安装 BlackHole")
        original = self._current_input()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
            wav_path = f.name
        pressed = False
        try:
            if self.run(["osascript", "-e", 'tell application "WeChat" to activate'],
                        capture_output=True, text=True, timeout=5).returncode != 0:
                return ActionResult(success=False, error="激活微信失败")
            self.sleep(1.0)
            if not self._set_input(self.device):
                return ActionResult(success=False, error=f"切换输入设备到「{self.device}」失败")
            self.key(True)
            pressed = True
            self.sleep(PRE_ROLL)
            play = self.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", wav_path,
                 "-f", "audiotoolbox", "-audio_device_index", str(index), "-"],
                capture_output=True, text=True, timeout=seconds + 15,
            )
            self.sleep(POST_ROLL)
            if play.returncode != 0:
                # 已经在录音：松开会把空白/半截语音发出去，这里仍需松开，由调用方知晓失败
                return ActionResult(success=False, error=f"播放失败: {play.stderr.strip()[:200]}")
            return ActionResult(success=True)
        finally:
            if pressed:
                self.key(False)
            if original:
                self._set_input(original)
            Path(wav_path).unlink(missing_ok=True)

    def send(self, text: str, mood: str = DEFAULT_MOOD, **tune) -> ActionResult:
        try:
            data = self.synthesize(text, mood, **tune)
        except Exception as e:
            _logger.error("[VoiceSender] 合成失败: %s", e)
            return ActionResult(success=False, error=f"合成失败: {e}")
        result = self.send_wav(data)
        if result.success:
            result.sent_text = text
        _logger.info("[VoiceSender] mood=%s tune=%s text=%r success=%s error=%s",
                     mood, tune, text, result.success, result.error)
        return result
