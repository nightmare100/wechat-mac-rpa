import io
import wave
from types import SimpleNamespace

import pytest

from src.action.voice_sender import MOODS, VoiceSender, build_tts_input, find_audiotoolbox_index, wav_seconds


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * int(24000 * seconds))
    return buf.getvalue()


DEVICES = (
    "[AudioToolbox @ 0x1] CoreAudio devices:\n"
    "[AudioToolbox @ 0x1] [1]                  BlackHole 2ch, BlackHole2ch_UID\n"
    "[AudioToolbox @ 0x1] [3]           MacBook Pro Speakers, BuiltInSpeakerDevice\n"
)


class FakeRun:
    def __init__(self, fail=None):
        self.calls, self.input, self.fail = [], "MacBook Pro Microphone", fail or set()

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        name = cmd[0]
        if name == "SwitchAudioSource" and "-c" in cmd:
            return SimpleNamespace(returncode=0, stdout=self.input + "\n", stderr="")
        if name == "SwitchAudioSource":
            if "switch" in self.fail:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            self.input = cmd[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if name == "ffmpeg" and "-list_devices" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr=DEVICES)
        if name == "ffmpeg":
            return SimpleNamespace(returncode=1 if "play" in self.fail else 0, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _sender(run, keys):
    return VoiceSender(run=run, key=keys.append, sleep=lambda s: None, http=object())


def test_wav_seconds_ignores_placeholder_header_length():
    import struct
    data = bytearray(_wav(3.36))
    data[4:8] = struct.pack("<I", 2147483583)   # 百炼返回的流式占位长度
    i = data.find(b"data")
    data[i + 4:i + 8] = struct.pack("<I", 2147483547)
    assert abs(wav_seconds(bytes(data)) - 3.36) < 0.01


def test_build_input_tag_and_instruction():
    body = build_tts_input("周活动快结束啦", "开心", "vid")
    assert body["text"] == "[excited]周活动快结束啦"
    assert body["instruction"] == "语气开心、轻快"
    assert body["voice"] == "vid"


def test_build_input_mood_without_tag_or_instruction():
    assert build_tts_input("好", "撒娇", "v")["text"] == "好"
    assert "instruction" not in build_tts_input("好", "平静", "v")


def test_unknown_mood_rejected():
    with pytest.raises(ValueError):
        build_tts_input("好", "暴躁", "v")


def test_at_least_five_moods():
    assert len(MOODS) >= 5


def test_find_device_index_by_name():
    assert find_audiotoolbox_index("BlackHole 2ch", FakeRun()) == 1
    assert find_audiotoolbox_index("不存在", FakeRun()) is None


def test_send_sequence_and_restore():
    run, keys = FakeRun(), []
    result = _sender(run, keys).send_wav(_wav(2))
    assert result.success
    assert keys == [True, False]
    play = [c for c in run.calls if c[0] == "ffmpeg" and "-list_devices" not in c][0]
    assert play[play.index("-audio_device_index") + 1] == "1"
    assert run.input == "MacBook Pro Microphone"  # 发完切回原输入


def test_play_failure_still_releases_key_and_restores_input():
    run, keys = FakeRun(fail={"play"}), []
    result = _sender(run, keys).send_wav(_wav(2))
    assert not result.success
    assert keys == [True, False]
    assert run.input == "MacBook Pro Microphone"


def test_switch_failure_never_presses_key():
    run, keys = FakeRun(fail={"switch"}), []
    result = _sender(run, keys).send_wav(_wav(2))
    assert not result.success and keys == []


def test_too_long_rejected_before_touching_anything():
    run, keys = FakeRun(), []
    result = _sender(run, keys).send_wav(_wav(59))
    assert not result.success and keys == [] and run.calls == []


def test_tune_params_and_style_appended():
    body = build_tts_input("好", "撒娇", "v", rate=0.9, volume=60, style="尾音拖长一点")
    assert body["rate"] == 0.9 and body["volume"] == 60
    assert body["instruction"] == "撒娇的语气，声音软软的；尾音拖长一点"
    assert build_tts_input("好", "平静", "v", style="慢一点")["instruction"] == "慢一点"
    assert "rate" not in build_tts_input("好", "平静", "v")


@pytest.mark.parametrize("kw", [{"rate": 2.5}, {"rate": 0.4}, {"volume": 101}])
def test_tune_out_of_range_rejected(kw):
    with pytest.raises(ValueError):
        build_tts_input("好", "平静", "v", **kw)
