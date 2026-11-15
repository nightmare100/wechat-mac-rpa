# MuMu 模拟器 Python 模块

独立模块，后续可由微信 Bot 的工具层调用。当前不注册到 Bot，不改变现有微信行为。

使用本机 MuMu 自带 `mumu-cli` 和 ADB；已在 Mac MuMu 1.9.15 验证实例查询、当前 App 查询和 Android 原生 PNG 截图。启动/关闭实例及 App、点击等写操作通过离线测试，尚未在真实 App 内执行。

## 快速使用

在 `wechat-mac-rpa` 目录使用项目虚拟环境运行 Python：

```python
from src.mumu import MuMuEmulatorUtil, MuMuError

mumu = MuMuEmulatorUtil(instance_id=0)
print(mumu.list_devices())

try:
    mumu.start()  # 启动指定实例，并等待 Android 启动完成
    mumu.open_app("com.android.settings")  # 包名，不是显示名称
    print(mumu.current_app())

    shot = mumu.screenshot()
    print(shot.path, shot.width, shot.height)

    # 坐标以 Android 截图像素为准，与 Mac 窗口位置和缩放无关。
    mumu.tap(100, 200)
    mumu.long_press(100, 200, duration_ms=800)
    mumu.swipe(100, 500, 100, 200, duration_ms=300)
    mumu.keyevent("BACK")
except MuMuError as exc:
    print("模拟器操作失败:", exc)
```

无需截图文件时使用 `png_bytes = mumu.capture_png()`；指定位置使用 `mumu.screenshot("/tmp/mumu.png")`。默认保存到项目 `data/mumu/screenshots/`，不会加入 Git；文件名唯一，当前不自动清理历史截图。指定已有文件时，只有完整 PNG 读取和校验成功才会原子替换。

## 顺序操作

```python
results = mumu.execute_steps([
    {"action": "open_app", "package": "com.android.settings"},
    {"action": "wait", "seconds": 0.5},
    {"action": "screenshot"},
    {"action": "keyevent", "key": "BACK"},
])
for result in results:
    print(result.index, result.action, result.success, result.value, result.error)
```

执行前校验所有步骤的动作名和参数名；参数值在各动作执行时校验。同一对象的序列和单次操作用锁串行执行。遇到运行时失败停止，返回已经执行的结果及失败步骤，不执行剩余步骤，也不自动重试点击。单次方法通过异常报告错误：参数不合法为 `ValueError`，工具/设备/执行失败为 `MuMuError`。

## 接口

| 方法 | 行为 |
|---|---|
| `list_devices()` | 实例列表：编号、名称、状态、ADB 端口、进程号 |
| `start(wait=True, timeout=120)` / `stop()` | 启停指定实例 |
| `wait_until_ready(timeout=120)` | 等待 ADB 可访问且 `sys.boot_completed=1` |
| `open_app(package, wait=True, timeout=30)` | 启动 App，默认等待进入前台 |
| `close_app(package)` | 关闭指定 App |
| `current_app()` | 前台包名，未找到前台 Activity 时返回 `None` |
| `tap(x, y)` / `long_press(x, y, duration_ms=800)` | 点击 / 长按 |
| `swipe(x1, y1, x2, y2, duration_ms=300)` | 滑动 |
| `input_text(text)` | 可打印 ASCII 输入；输入框须已获得焦点 |
| `keyevent(key)` | Android 键码，或 `BACK/HOME/ENTER/DEL/MENU/APP_SWITCH/DPAD_*` |
| `wait(seconds)` | 序列中的显式等待，0 < seconds <= 60 |
| `capture_png()` / `screenshot(path=None)` | PNG 字节 / `Screenshot` 返回值 |
| `execute_steps(steps)` | 返回 `list[StepResult]`，首个失败后停止 |

## 配置及边界

- 需要打开 MuMu 管理器、启用对应实例的 ADB 功能。实例编号来自 `list_devices()`，不会隐式操作所有实例；每次 Android 操作重新查询实际 ADB 端口，并使用 `adb -s` 定位。
- 默认工具位于 `/Applications/MuMuPlayer.app`。可用构造参数 `cli_path`、`adb_path` 或环境变量 `MUMU_CLI_PATH`、`MUMU_ADB_PATH` 覆盖；显式参数优先。模块自身不加载 `.env`，由调用方负责加载环境。
- 构造参数 `timeout=15` 是单条外部命令的超时秒数；启动/前台等待另有总超时，等待中的命令不会超出剩余时间。
- `open_app(wait=False)` 只确认 CLI 接受操作；默认 `wait=True` 还会核对实际前台包名。App 首启权限弹窗或登录页需后续流程处理。
- 坐标是非负整数，不自动裁剪到屏幕范围；旋转或分辨率变化后应重新截图取坐标。
- `input_text` 暂不支持中文、换行和字面量 `%s`，会明确报错；不会自动安装输入法、请求 root 或修改系统配置。
- 无界面识别/控件定位、App 安装卸载、任意 shell 或自然语言任务规划；这些可通过后续模块或 Bot 适配扩展。
- 锁只覆盖同一个 Python 工具对象；后续 Bot 应复用单实例，不并发创建多个对象操作同一台模拟器。

测试（不连接真实模拟器）：`.venv/bin/python -m pytest src/tests/test_mumu.py -q`。
