# MuMu Python 模块

用户确认：先做 Python 工具类，与项目放在一起新建模块；最终接微信 Bot；截图从 Android 内部获取。

采用 `src/mumu/` 独立模块，公开 `MuMuEmulatorUtil`，本期不注册到 Bot。复用 MuMu 自带 `mumu-cli` 管理实例及启动 App，按实例信息中的 ADB 端口访问 Android；不用 Mac 屏幕截图或鼠标坐标。

提供实例列表/启停、等待 Android 就绪、启动/关闭指定包名 App、当前 App、点击/长按/滑动、ASCII 文本输入、按键、PNG 截图与操作序列。文字中文输入需要额外输入法，先明确报错，不自动安装。坐标对应 Android 截图像素。

实例默认 0，可显式选择；不对 all 执行操作。Python subprocess 参数数组执行，远端 shell 参数正确引用；不开放任意 shell 执行接口。识别非零返回码、CLI JSON 业务错误、ADB 断连及超时；失败抛出统一错误，序列遇错停止并返回结构化结果。

截图使用 `adb -s <serial> exec-out screencap -p` 的原始 PNG 字节，验证 PNG 后原子保存；默认保存 `data/mumu/screenshots`（不入 Git）。入口支持自定义工具路径与超时，并有文档和离线测试。

方案比较：纯 Mac GUI 自动化依赖窗口位置；MuMu CLI + ADB 可定位明确实例、获取 Android 原生截图，符合已确认方向。UI 节点选择、图像识别、中文输入法和具体 App 业务流程后续另做。

验收：离线验证实例解析、正确设备路由、错误处理、输入引用、序列停止与 PNG 保存；本机只读验证列表、当前 App 和截图。不修改现有 Bot，不操作未知 App 的业务按钮。
