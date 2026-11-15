"""MuMu CLI for instances/apps, ADB for Android input and native PNG capture."""
import inspect
import io
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from PIL import Image

from .models import DeviceInfo, MuMuError, Screenshot, StepResult

APP = Path("/Applications/MuMuPlayer.app")
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
COMPONENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)/[A-Za-z0-9_.$]+")
KEYS = {"HOME": 3, "BACK": 4, "ENTER": 66, "DEL": 67, "MENU": 82, "APP_SWITCH": 187,
        "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22, "DPAD_CENTER": 23}


def _integer(value, name, low=0, high=32767):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low} 到 {high} 的整数")
    return value


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是大于 0 的有限秒数")
    return float(value)


def _package(value):
    if not isinstance(value, str) or not PACKAGE.fullmatch(value):
        raise ValueError("package 必须是 Android 包名，例如 com.android.settings")
    return value


class MuMuEmulatorUtil:
    """One explicit MuMu instance. Methods on the same object are serialized.

    Coordinates are Android screenshot pixels. No Mac window activation, mouse
    events, APK installation, or arbitrary public shell execution is performed.
    """

    def __init__(self, instance_id=0, *, cli_path=None, adb_path=None, timeout=15,
                 screenshot_dir=None, runner=None):
        self.instance_id = _integer(instance_id, "instance_id")
        self.cli_path = str(Path(cli_path or os.getenv("MUMU_CLI_PATH") or APP / "Contents/MacOS/mumu-cli").expanduser())
        self.adb_path = str(Path(adb_path or os.getenv("MUMU_ADB_PATH") or APP / "Contents/MacOS/MuMuEmulator.app/Contents/MacOS/tools/adb").expanduser())
        self.timeout = _seconds(timeout, "timeout")
        self.screenshot_dir = Path(screenshot_dir or Path(__file__).resolve().parents[2] / "data/mumu/screenshots").expanduser()
        self._runner = runner or subprocess.run
        self._lock = threading.RLock()
        self._deadline = None

    def _run(self, args, operation):
        timeout = self.timeout
        if self._deadline is not None:
            timeout = min(timeout, self._deadline - time.monotonic())
            if timeout <= 0:
                raise MuMuError(f"{operation} 超时")
        try:
            result = self._runner(args, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError:
            raise MuMuError(f"{operation} 找不到命令工具，请检查 MUMU_CLI_PATH / MUMU_ADB_PATH") from None
        except subprocess.TimeoutExpired:
            raise MuMuError(f"{operation} 超时") from None
        except OSError:
            raise MuMuError(f"{operation} 无法执行，请检查工具权限") from None
        if result.returncode:
            # Never echo the command or input text (which may contain credentials).
            raise MuMuError(f"{operation} 失败，退出码 {result.returncode}")
        return result.stdout

    def _cli(self, *args):
        raw = self._run([self.cli_path, *args], "MuMu CLI")
        try:
            data = json.loads(raw)
            code = data.get("errcode", data.get("code"))
            if type(code) is not int or code != 0:
                raise MuMuError(f"MuMu CLI 业务错误（code={code}）")
            items = (data.get("return") or {}).get("results", [])
            for item in items:
                item_code = item.get("errcode", item.get("code", 0))
                if item_code != 0:
                    raise MuMuError(f"MuMu 实例操作失败（code={item_code}）")
            return data
        except (ValueError, TypeError, AttributeError):
            raise MuMuError("MuMu CLI 返回无效 JSON；请检查 MuMu 是否运行及版本兼容性") from None

    def list_devices(self):
        """Return MuMu instance metadata, without starting any instance."""
        with self._lock:
            data = self._cli("info", "all")
            try:
                rows = data["return"]["results"]
                devices = []
                for row in rows:
                    index = _integer(row["index"], "index")
                    port = row.get("adb_port")
                    if port == 0 and type(port) is int:
                        port = None
                    if port is not None:
                        port = _integer(port, "adb_port", 1, 65535)
                    devices.append(DeviceInfo(index, str(row["name"]), str(row["state"]), port, row.get("pid")))
                if len({d.index for d in devices}) != len(devices):
                    raise ValueError("duplicate instances")
                return devices
            except (KeyError, ValueError, TypeError):
                raise MuMuError("MuMu 实例列表格式异常") from None

    def _connect(self):
        device = next((d for d in self.list_devices() if d.index == self.instance_id), None)
        if device is None:
            raise MuMuError(f"MuMu 实例 {self.instance_id} 不存在")
        if device.state != "running" or not device.adb_port:
            raise MuMuError(f"MuMu 实例 {self.instance_id} 尚未运行或 ADB 未就绪")
        serial = f"127.0.0.1:{device.adb_port}"
        connected = self._run([self.adb_path, "connect", serial], "ADB 连接").decode(errors="replace").lower()
        if "connected to" not in connected:
            raise MuMuError(f"MuMu 实例 {self.instance_id} ADB 连接失败")
        state = self._run([self.adb_path, "-s", serial, "get-state"], "ADB 设备状态")
        if state.strip() != b"device":
            raise MuMuError(f"MuMu 实例 {self.instance_id} ADB 未授权或离线")
        return serial

    def _shell(self, *args):
        serial = self._connect()
        # ADB joins shell arguments on Android: a local argv list alone is not
        # sufficient to protect metacharacters, so quote the remote command too.
        command = shlex.join(str(arg) for arg in args)
        output = self._run([self.adb_path, "-s", serial, "shell", command], "Android 操作")
        return output.decode(errors="replace").strip()

    def _wait_until(self, predicate, timeout, description):
        previous = self._deadline
        self._deadline = time.monotonic() + _seconds(timeout, "timeout")
        last_error = ""
        try:
            while time.monotonic() < self._deadline:
                try:
                    if predicate():
                        return
                except MuMuError as exc:
                    last_error = str(exc)
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.5, remaining))
            raise MuMuError(f"{description}超时" + (f"（{last_error}）" if last_error else ""))
        finally:
            self._deadline = previous

    def start(self, *, wait=True, timeout=120):
        _seconds(timeout, "timeout")
        with self._lock:
            self._cli("open", str(self.instance_id))
            if wait:
                self.wait_until_ready(timeout=timeout)

    def stop(self):
        with self._lock:
            self._cli("close", str(self.instance_id))

    def wait_until_ready(self, *, timeout=120):
        """Wait for both ADB and Android sys.boot_completed=1."""
        with self._lock:
            self._wait_until(lambda: self._shell("getprop", "sys.boot_completed") == "1", timeout, "等待 Android 启动")

    def open_app(self, package, *, wait=True, timeout=30):
        _package(package)
        _seconds(timeout, "timeout")
        with self._lock:
            self._connect()
            self._cli("control", str(self.instance_id), "--action", "open_app", "--package", package)
            if wait:
                self._wait_until(lambda: self.current_app() == package, timeout, "等待目标 App 进入前台")

    def close_app(self, package):
        _package(package)
        with self._lock:
            self._connect()
            self._cli("control", str(self.instance_id), "--action", "close_app", "--package", package)

    def current_app(self):
        """Foreground package, or None if no resumed/focused activity exists."""
        with self._lock:
            text = self._shell("dumpsys", "activity", "activities")
            for line in text.splitlines():
                if "ResumedActivity" in line or "mResumedActivity" in line:
                    match = COMPONENT.search(line)
                    if match:
                        return match.group(1)
            text = self._shell("dumpsys", "window", "windows")
            for line in text.splitlines():
                if "mCurrentFocus" in line:
                    match = COMPONENT.search(line)
                    if match:
                        return match.group(1)
            return None

    def tap(self, x, y):
        _integer(x, "x")
        _integer(y, "y")
        with self._lock:
            self._shell("input", "tap", x, y)

    def long_press(self, x, y, *, duration_ms=800):
        self.swipe(x, y, x, y, duration_ms=duration_ms)

    def swipe(self, x1, y1, x2, y2, *, duration_ms=300):
        for name, value in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
            _integer(value, name)
        _integer(duration_ms, "duration_ms", 1, 60000)
        with self._lock:
            self._shell("input", "swipe", x1, y1, x2, y2, duration_ms)

    def input_text(self, text):
        """Printable ASCII via Android input text; Chinese needs an IME adapter."""
        if not isinstance(text, str) or not text or any(not 32 <= ord(c) <= 126 for c in text):
            raise ValueError("input_text 仅支持非空可打印 ASCII；中文需要另接 Android 输入法")
        if "%s" in text:
            raise ValueError("Android input text 无法保留字面量 %s")
        with self._lock:
            self._shell("input", "text", text.replace(" ", "%s"))

    def keyevent(self, key):
        if isinstance(key, str):
            key = KEYS.get(key.removeprefix("KEYCODE_").upper())
        _integer(key, "keyevent", 0, 999)
        with self._lock:
            self._shell("input", "keyevent", key)

    def wait(self, seconds):
        """Explicit pause between UI steps, at most 60 seconds."""
        seconds = _seconds(seconds, "seconds")
        if seconds > 60:
            raise ValueError("单步 wait 不得超过 60 秒")
        with self._lock:
            time.sleep(seconds)

    @staticmethod
    def _png_size(data):
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise ValueError("not PNG")
                size = image.size
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return size
        except (OSError, ValueError, SyntaxError):
            raise MuMuError("Android 截图不是有效 PNG，未保存") from None

    def capture_png(self):
        """Return validated Android framebuffer PNG bytes; no host screenshot."""
        with self._lock:
            serial = self._connect()
            data = self._run([self.adb_path, "-s", serial, "exec-out", "screencap", "-p"], "Android 截图")
            self._png_size(data)
            return data

    def screenshot(self, path=None):
        with self._lock:
            path = Path(path).expanduser() if path is not None else self.screenshot_dir / f"mumu_{self.instance_id}_{uuid.uuid4().hex}.png"
            if path.suffix.lower() != ".png":
                raise ValueError("截图路径必须以 .png 结尾")
            data = self.capture_png()
            width, height = self._png_size(data)
            temporary = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as f:
                    temporary = Path(f.name)
                    f.write(data)
                os.replace(temporary, path)
            except OSError:
                raise MuMuError("截图文件保存失败，请检查目录及权限") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return Screenshot(path.resolve(), width, height, self.instance_id, time.time())

    def execute_steps(self, steps):
        """Execute structured primitives, return results, stop on first failure.

        Validate action names and call signatures before any action is executed.
        Runtime errors are captured in StepResult; standalone methods raise.
        """
        methods = {name: getattr(self, name) for name in (
            "open_app", "close_app", "current_app", "tap", "long_press", "swipe",
            "input_text", "keyevent", "screenshot", "wait_until_ready", "wait",
        )}
        prepared = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("action"), str) or step["action"] not in methods:
                raise ValueError("未知操作步骤；只接受公开的 Android 操作")
            action = step["action"]
            arguments = {k: v for k, v in step.items() if k != "action"}
            try:
                inspect.signature(methods[action]).bind(**arguments)
            except TypeError:
                raise ValueError(f"步骤 {action} 参数不匹配") from None
            prepared.append((action, arguments))
        results = []
        with self._lock:
            for index, (action, arguments) in enumerate(prepared):
                try:
                    value = methods[action](**arguments)
                    results.append(StepResult(index, action, True, value))
                except (MuMuError, ValueError) as exc:
                    results.append(StepResult(index, action, False, error=str(exc)))
                    break
        return results
