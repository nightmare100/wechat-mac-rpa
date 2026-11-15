"""MuMu utility tests: simulated CLI/ADB only, never click a real app."""
import io
import json
import shlex
import subprocess

import pytest
from PIL import Image

from src.mumu import MuMuEmulatorUtil, MuMuError


def png():
    out = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(out, format="PNG")
    return out.getvalue()


class Runner:
    def __init__(self):
        self.calls = []
        self.devices = [dict(index=0, name="测试设备", state="running", adb_port=16384, pid=12)]
        self.cli_error = 0
        self.raw_png = png()
        self.fail_tap = False
        self.foreground = "com.android.settings"

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert not kwargs.get("shell")
        assert kwargs["timeout"] > 0
        output = b""
        if args[0] == "/fake/mumu-cli":
            payload = {"errcode": self.cli_error, "message": "error" if self.cli_error else ""}
            if args[1] == "info":
                payload["return"] = {"count": len(self.devices), "results": self.devices}
            output = json.dumps(payload).encode()
        elif "connect" in args:
            output = b"connected to 127.0.0.1:16384"
        elif "get-state" in args:
            output = b"device\n"
        elif "exec-out" in args:
            output = self.raw_png
        elif "shell" in args:
            cmd = shlex.split(args[-1])
            if cmd == ["getprop", "sys.boot_completed"]:
                output = b"1\n"
            elif cmd[:2] == ["dumpsys", "activity"]:
                output = f"mResumedActivity: ActivityRecord{{123 u0 {self.foreground}/.Main t1}}".encode()
            elif cmd[:2] == ["input", "tap"] and self.fail_tap:
                return subprocess.CompletedProcess(args, 1, b"", b"tap failed")
        return subprocess.CompletedProcess(args, 0, output, b"")


@pytest.fixture
def rig(tmp_path):
    runner = Runner()
    util = MuMuEmulatorUtil(cli_path="/fake/mumu-cli", adb_path="/fake/adb", runner=runner,
                            screenshot_dir=tmp_path / "shots")
    return util, runner


def shell_calls(runner):
    return [shlex.split(args[-1]) for args, _ in runner.calls if "shell" in args]


def test_lists_instance_and_resolves_changed_adb_port(rig):
    util, runner = rig
    device = util.list_devices()[0]
    assert (device.index, device.adb_port, device.state) == (0, 16384, "running")
    assert util.current_app() == "com.android.settings"
    runner.devices[0]["adb_port"] = 16416
    util.tap(1, 2)
    assert any(args[:3] == ["/fake/adb", "-s", "127.0.0.1:16416"] for args, _ in runner.calls)


def test_routes_only_selected_instance(rig):
    _, runner = rig
    runner.devices.append(dict(index=2, name="第二台", state="running", adb_port=16448))
    util = MuMuEmulatorUtil(2, cli_path="/fake/mumu-cli", adb_path="/fake/adb", runner=runner)
    util.tap(10, 20)
    selected = [args[2] for args, _ in runner.calls if args[:2] == ["/fake/adb", "-s"]]
    assert selected and set(selected) == {"127.0.0.1:16448"}


def test_cli_business_error_fails_even_with_zero_exit_code(rig):
    util, runner = rig
    runner.cli_error = 1001
    with pytest.raises(MuMuError):
        util.list_devices()


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("adb", 1)])
def test_command_failure_has_uniform_error(rig, failure):
    util, _ = rig
    util._runner = lambda *a, **k: (_ for _ in ()).throw(failure)
    with pytest.raises(MuMuError):
        util.list_devices()


def test_stopped_or_missing_device_never_receives_input(rig):
    util, runner = rig
    runner.devices[0]["state"] = "stopped"
    with pytest.raises(MuMuError):
        util.tap(1, 2)
    runner.devices.clear()
    with pytest.raises(MuMuError):
        util.capture_png()
    assert not shell_calls(runner)


def test_start_stop_and_package_app_commands(rig):
    util, runner = rig
    util.start()
    util.open_app("com.android.settings")
    util.close_app("com.android.settings")
    util.stop()
    calls = [args for args, _ in runner.calls]
    assert ["/fake/mumu-cli", "open", "0"] in calls
    assert ["/fake/mumu-cli", "close", "0"] in calls
    assert any("open_app" in args and "com.android.settings" in args for args in calls)
    assert any("close_app" in args and "com.android.settings" in args for args in calls)


def test_android_operations_and_shell_quoting(rig):
    util, runner = rig
    util.tap(10, 20)
    util.long_press(30, 40, duration_ms=900)
    util.swipe(1, 2, 3, 4, duration_ms=350)
    util.input_text("hello $HOME; 'world' & ls")
    util.keyevent("BACK")
    calls = shell_calls(runner)
    assert ["input", "tap", "10", "20"] in calls
    assert ["input", "swipe", "30", "40", "30", "40", "900"] in calls
    assert ["input", "swipe", "1", "2", "3", "4", "350"] in calls
    assert ["input", "text", "hello%s$HOME;%s'world'%s&%sls"] in calls
    assert ["input", "keyevent", "4"] in calls


@pytest.mark.parametrize("method,arguments", [
    ("tap", {"x": -1, "y": 0}), ("tap", {"x": True, "y": 0}),
    ("open_app", {"package": "com.app; rm -rf /"}),
    ("input_text", {"text": "中文"}), ("input_text", {"text": "literal%s"}),
    ("keyevent", {"key": "BACK; reboot"}),
    ("swipe", {"x1": 0, "y1": 0, "x2": 2, "y2": 2, "duration_ms": 0}),
])
def test_invalid_arguments_do_not_run_commands(rig, method, arguments):
    util, runner = rig
    with pytest.raises((ValueError, MuMuError)):
        getattr(util, method)(**arguments)
    assert not runner.calls


def test_screenshot_uses_android_exec_out_and_preserves_bytes(rig):
    util, runner = rig
    result = util.screenshot()
    assert result.path.read_bytes() == runner.raw_png
    assert (result.width, result.height, result.instance_id) == (12, 8, 0)
    assert any(args[-3:] == ["exec-out", "screencap", "-p"] for args, _ in runner.calls)
    assert result.path.parent == util.screenshot_dir


def test_bad_screenshot_never_overwrites_previous_file(rig, tmp_path):
    util, runner = rig
    path = tmp_path / "existing.png"
    path.write_bytes(png())
    runner.raw_png = b"error: offline"
    with pytest.raises(MuMuError):
        util.screenshot(path)
    assert path.read_bytes() == png()
    assert not list(tmp_path.glob("*.tmp"))


def test_steps_return_results_and_stop_after_failure(rig):
    util, runner = rig
    runner.fail_tap = True
    results = util.execute_steps([
        {"action": "current_app"}, {"action": "tap", "x": 1, "y": 2}, {"action": "keyevent", "key": "HOME"},
    ])
    assert [r.success for r in results] == [True, False]
    assert results[0].value == "com.android.settings"
    assert results[1].error
    assert ["input", "keyevent", "3"] not in shell_calls(runner)


def test_steps_validate_names_and_keys_before_any_execution(rig):
    util, runner = rig
    for bad in ({"action": "_run", "args": ["rm"]}, {"action": "tap", "x": 1}):
        with pytest.raises(ValueError):
            util.execute_steps([{"action": "tap", "x": 1, "y": 2}, bad])
    assert not runner.calls


def test_negative_or_all_instance_id_is_rejected():
    for invalid in (-1, "all", "0,2", True):
        with pytest.raises(ValueError):
            MuMuEmulatorUtil(invalid)


def test_stopped_zero_port_does_not_block_running_instance(rig):
    util, runner = rig
    runner.devices.append(dict(index=1, name="未启动", state="stopped", adb_port=0))
    assert util.list_devices()[1].adb_port is None
    util.tap(1, 2)


def test_successful_command_may_have_null_return(rig):
    util, runner = rig
    base = runner.__call__
    util._runner = lambda args, **kwargs: (
        subprocess.CompletedProcess(args, 0, b'{"errcode":0,"return":null}', b"")
        if args[1] == "open" else base(args, **kwargs)
    )
    util.start(wait=False)


def test_sequence_wait_is_explicit_and_bounded(rig, monkeypatch):
    util, _ = rig
    sleeps = []
    monkeypatch.setattr("src.mumu.client.time.sleep", sleeps.append)
    results = util.execute_steps([{"action": "wait", "seconds": 0.5}, {"action": "current_app"}])
    assert all(r.success for r in results) and sleeps == [0.5]
    with pytest.raises(ValueError):
        util.wait(61)


def test_corrupt_pixel_stream_is_rejected(rig):
    import struct
    import zlib
    util, runner = rig
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    runner.raw_png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                      + chunk(b"IDAT", b"not valid zlib") + chunk(b"IEND", b""))
    with pytest.raises(MuMuError):
        util.capture_png()


def test_wait_timeout_is_bounded_without_real_sleep(rig, monkeypatch):
    util, _ = rig
    ticks = [0.0]
    monkeypatch.setattr("src.mumu.client.time.monotonic", lambda: ticks[0])
    monkeypatch.setattr("src.mumu.client.time.sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    util._shell = lambda *args: "0"
    with pytest.raises(MuMuError, match="超时"):
        util.wait_until_ready(timeout=1)
    assert ticks[0] == 1 and util._deadline is None
