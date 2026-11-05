# 发送语音消息调研（存档）

> 调研日期：2026-11-04
> 状态：仅调研，未实现、未实测。项目代码中目前没有任何语音相关逻辑。

## 背景

想让 bot 偶尔发送真正的语音条（语音气泡），而不只是文字。

## 前提：微信 Mac 版已支持发语音

- 本机微信版本：**4.1.15**
- 微信 Mac 版从 **4.1.9** 起支持发送语音消息：
  - 输入框右下角有麦克风按钮；
  - **按住 Option（Alt）键录音，松开即发送**；
  - 单条最长 60 秒。
- 注意区分：4.1.8 加入的是"语音输入文字"（Mac 上默认按住 fn），会把语音转成文字发出，不是语音条。

## 核心限制

微信**没有"选择音频文件作为语音条发送"的入口**，只能通过录音发出。
因此 bot 发语音必须让微信"录"进去一段音频。

## 方案对比

| 方案 | 群里效果 | 可行性 |
|---|---|---|
| A. 虚拟声卡回环 + 模拟录音 | 真正的语音条 | 可行，需要装虚拟声卡并实测 |
| B. 把音频当文件发送 | .mp3 文件卡片，需下载才能听 | 简单，可复用发文件流程，但不像"本人说话" |
| C. 喇叭外放 + 真麦克风收音 | 真正的语音条 | 有回声和环境噪音、半夜会出声，只适合临时验证 |
| D. 直接发 SILK 文件（hook/注入/iPad 协议） | 真正的语音条 | **不采用**：违反微信协议、封号风险高、需关闭 SIP、升级易失效，与本项目的 RPA 路线不符 |

在不碰微信内部的前提下，**方案 A 是唯一能发真正语音条的可行路线**。

## 方案 A：虚拟声卡回环

### 原理

全程数字信号，不经过喇叭和麦克风：

```
TTS 音频 → 播放到虚拟声卡输出 → 虚拟声卡输入 → 微信当作麦克风录音
```

- 房间里听不到声音，不会录进环境杂音；
- 音质基本无损；
- 录音时长 = 语音时长，这期间 bot 不能做别的事。

### 流程

1. **TTS 生成语音**：
   - 百炼语音合成（CosyVoice / qwen-tts，可选可爱女声，单独计费，费用很低）；
   - 或 macOS 自带 `say`（免费，但声音机械）。
2. **切换默认输入设备**为虚拟声卡（如 BlackHole 2ch）。
3. 在微信聊天窗口**按住 Option**，同时把音频播放到虚拟声卡，播完松开 → 语音条发出。
4. **恢复默认输入设备**为真实麦克风。

### 选定链路：TTS → FFmpeg → BlackHole → 微信 Mac 客户端

```
[TTS]            文字 → 音频文件（mp3/wav）
   │
[FFmpeg]         转码（单声道 / 统一采样率 / 音量标准化），并直接播放到 BlackHole
   │             ffmpeg -re -i voice.wav -f audiotoolbox -audio_device_index <BlackHole序号> -
   ▼
[BlackHole 2ch]  虚拟声卡：输出端收到的声音原样出现在输入端
   │
[微信 Mac]       默认输入设备 = BlackHole；按住 Option 录音 → 播完松开 → 语音条发出
```

各环节说明：

| 环节 | 作用 | 备注 |
|---|---|---|
| TTS | 回复文字 → 音频 | 百炼 CosyVoice / qwen-tts，或 `say` 做验证 |
| FFmpeg | 格式处理 + 播放 | `-re` 按实时速度播放；`audiotoolbox` 输出设备可**直接指定播放到 BlackHole，不用改系统默认输出**，Mac 照常外放其他声音 |
| BlackHole | 把"播放"变成"麦克风输入" | 需安装；装好后设备序号用下面的命令查 |
| 微信 | 录音并发送 | 需要把**系统默认输入**切到 BlackHole（`SwitchAudioSource -t input -s "BlackHole 2ch"`），发完切回 |

已验证（2026-11-04）：本机 ffmpeg 支持 `audiotoolbox` 输出，能列出并指定输出设备：

```bash
ffmpeg -hide_banner -f lavfi -i anullsrc -t 0.05 -f audiotoolbox -list_devices true -
```

当前列出的设备：MacBook Pro Microphone、MacBook Pro Speakers、Omi Recorder ×2、Apowersoft Audio Device（无 BlackHole）。

时序（bot 内单次发送）：

1. TTS 生成音频，ffmpeg 转码，得到时长 T 秒（需 ≤ 60 秒）
2. 切换默认输入 → BlackHole
3. 激活微信、确认在目标聊天窗口
4. 按下 Option（开始录音），稍等约 0.3 秒
5. ffmpeg 播放到 BlackHole（阻塞 T 秒）
6. 再等约 0.3 秒，松开 Option → 发送
7. 切回默认输入 → MacBook Pro Microphone（无论成功失败都要执行）

手动验证（2026-11-05，在「文件传输助手」里发给自己）：✅ 链路打通

- 已安装 BlackHole 2ch 和 SwitchAudioSource（`/usr/local/bin/SwitchAudioSource`）。
- **回环**：用 ffmpeg 播放到 BlackHole（`-f audiotoolbox -audio_device_index 1`），再从 BlackHole 录回（`-f avfoundation -i ":1"`）。录回的峰值和原文件一致（都是 -6.1 dB），声音原样传过去了。
- **微信录音**：把系统默认输入切到 BlackHole，**人手按住 Option**；1.5 秒后播放 2.7 秒的合成语音，播完再等 0.5 秒松开。语音条成功发出，手机上播放正常、内容完整、没有杂音。
- **结论**：
  - 微信用的是**系统默认输入设备**，切换系统默认输入就能生效。
  - 按下 Option 后等 1.5 秒再播放，开头不会被吞。
- 测试脚本用提示音告诉操作者按住或松开 Option，退出时一定会切回原来的输入设备。

ffmpeg 的设备序号（本机，2026-11-05）：
- audiotoolbox 输出：`[1] BlackHole 2ch`；
- avfoundation 输入：`[1] BlackHole 2ch`。
- 新增或拔出音频设备后，序号可能会变，接入代码时应按设备名查找序号。

自动化验证（2026-11-05）：✅ 程序按键可以触发录音

- **必须是右 Option**。人工测试时也是只有右 Option 有效。
  - AppleScript 的 `tell application "System Events" to key down option` 按的是**左 Option**，微信不响应，语音没有发出。
- **可行做法**：用 Quartz 发 `flagsChanged` 事件。
  - 按下：键码 61（右 Option），flags = `kCGEventFlagMaskAlternate | 0x40`（右 Alt 的设备位）。
  - 松开：flags = 0。
  - 事件发到 `kCGHIDEventTap`。

  ```python
  import Quartz as Q
  ev = Q.CGEventCreateKeyboardEvent(None, 61, down)
  Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
  Q.CGEventSetFlags(ev, (Q.kCGEventFlagMaskAlternate | 0x40) if down else 0)
  Q.CGEventPost(Q.kCGHIDEventTap, ev)
  ```

- **完整时序**（已跑通，语音条成功发出）：
  1. `tell application "WeChat" to activate`，等 1 秒；
  2. 输入切到 BlackHole；
  3. 按下右 Option；
  4. 等 1.5 秒；
  5. ffmpeg 播放到 BlackHole；
  6. 等 0.5 秒；
  7. 松开右 Option；
  8. 切回原输入。
- **收尾一定要执行**：无论成功或失败，都要「松开右 Option + 切回原输入」（脚本里用 `trap ... EXIT` 保证）。否则 Option 会一直处于按下状态，麦克风也一直停在 BlackHole 上。
- 前提：运行 bot 的终端需要有「辅助功能」权限。bot 现有的按键操作本来就依赖这个权限。

### 本机环境现状（2026-11-04）

- `ffmpeg`：已安装（`/usr/local/bin/ffmpeg`），可用于格式转换。
- `BlackHole`：2026-11-05 已安装（`brew install --cask blackhole-2ch`，需要管理员密码）。
- `SwitchAudioSource`：2026-11-05 已安装（`brew install switchaudio-osx`），用于命令行切换输入设备。
- 已有的虚拟声卡：Apowersoft Audio Device、Omi Recorder Audio Driver。能否作为回环使用未验证，建议仍用 BlackHole。
- 当前默认输入：MacBook Pro Microphone。

### 风险

- **发送前无法校验**：文字消息有"粘贴后复制回来逐字比对"的校验，语音没有。录到杂音、没录上或时机不对，都会直接发出，只能 2 分钟内手动撤回。
- **占用麦克风**：录音期间系统输入是虚拟声卡，会和会议、语音通话冲突。
- **按键触发未验证**：程序模拟的"按住 Option"能否被微信识别需实测；不行就改为鼠标点击麦克风按钮。
- **麦克风权限**：需确认微信已在系统设置中获得麦克风权限。

## TTS 选型：百炼声音复刻（自定义声音）

> 调研日期：2026-11-05。来源为百炼官方文档，见文末参考。仅查阅文档，未实测。

### 支持声音复刻的模型

| 系列 | 模型 | 地域 |
|---|---|---|
| Qwen-Audio-TTS | qwen-audio-3.1-tts-plus / flash、qwen-audio-3.0-tts-plus / flash | 北京、新加坡 |
| Qwen-Audio-Realtime | qwen-audio-3.1 / 3.0-realtime-plus / flash | 仅北京 |
| CosyVoice | cosyvoice-v3.5-plus / flash、cosyvoice-v3-plus / flash、cosyvoice-v2 | 北京全部；新加坡仅 v3-plus / v3-flash |
| MiniMax | speech-2.8-hd / turbo、speech-02-hd / turbo | 仅北京 |

- Realtime 系列用于实时语音对话。本项目是「先生成音频文件，再播放进微信」，用不上实时对话，因此不考虑。

### 复刻规则

- **费用**：
  - Qwen-Audio 与 CosyVoice：创建音色免费；合成时按目标模型计费。
  - MiniMax：复刻本身不收费；首次用复刻的声音合成时收 9.9 元解锁费，无免费额度。
- **复刻的声音绑定模型**：创建时要指定 `target_model`，之后只能在这个模型上用，不能跨模型。对比两个模型时，需用同一段录音各复刻一次。
- **有效期与数量**：默认长期有效；每个账号最多 1000 个，与「声音设计」共用这个额度。
- **流程**：
  1. 用 `voice-enrollment` 的 `create_voice` 传入参考音频（URL 或 Base64）和 `target_model`，拿到 voice_id。
  2. 用 `query_voice` 轮询，直到状态为 `OK`。
  3. 合成时 `voice` 填 voice_id，模型填同一个 `target_model`。
- **录音要求**（Qwen-Audio / CosyVoice）：
  - 格式与规格：WAV（16-bit）、MP3 或 M4A；10～20 秒为宜，最长 60 秒；≤ 10 MB；采样率 ≥ 16 kHz。
  - 内容：至少 5 秒连续清晰的说话，停顿 ≤ 2 秒；不要背景音乐、杂音、其他人声，不要唱歌。
- **替代方案**：「声音设计」用一段文字描述来生成声音，不需要录音。

### 复刻的声音能否控制情绪、语调

| 模型 | 复刻的声音能否带情绪 | 控制方式 |
|---|---|---|
| qwen-audio-3.1 / 3.0-tts-plus / flash | ✅ 文档原文：「系统音色、复刻音色和设计音色均支持在合成请求中通过 `instruction` 设置自然语言指令」 | ① `instruction` 参数，用自然语言描述，例如「开心、撒娇的语气」；② 在文本里直接写标签，如 `[excited]` `[sad]` `[whispers]` `[very slowly]` `[laughing]` `[sighing]`。标签对复刻的声音有没有限制，文档没写 |
| cosyvoice-v3.5-plus / flash | ✅ 这两个模型没有系统音色，指令控制就是给复刻 / 设计音色用的 | `instruction` 参数，用自然语言描述；最多 100 个字符，汉字算 2 个字符 |
| cosyvoice-v3-flash | ✅ 复刻的声音支持自然语言指令 | 同上 |
| cosyvoice-v3-plus | ❌ 文档原文：「复刻音色、设计音色不支持指令控制」 | — |
| MiniMax speech-2.8 | 文档没说复刻的声音能不能用 | `voice_setting.emotion`：happy / sad / angry / fearful / disgusted / surprised / calm；speech-2.8 系列不支持 whisper。不指定时，模型会按文本自动匹配情绪 |

- 各模型都有语速、音调、音量参数：
  - Qwen-Audio / CosyVoice：`rate` 0.5～2.0、`pitch` 0.5～2.0、`volume` 0～100。
  - MiniMax：`speed`、`pitch`、`vol`。
  - 这些参数对复刻的声音是否同样有效，文档没写。

### 初步结论（实测后已选定 qwen-audio-3.1-tts-plus，见下文「实测结果」）

- **候选**：`qwen-audio-3.1-tts-plus` 和 `cosyvoice-v3.5-plus`。两者都是各自系列的最新版，复刻的声音都支持情绪指令。
- **排除**：cosyvoice-v3-plus（复刻的声音不能控制情绪）、Realtime 系列（用不上）。
- **备选**：MiniMax，因为有 9.9 元解锁费。
- **地域**：已确认项目的百炼地址是北京地域（`cn-beijing`）。

### 尚未确认

- 合成单价：计费页没读到这几个模型的价格。
- 音质、情绪表现是否自然，以及 plus 和 flash 差多少：文档没有对比，需要实际听。
- 合成延迟：文档未提。

### 实测结果（2026-11-05）

**选定：`qwen-audio-3.1-tts-plus`**。两个模型的效果都不错，用户更喜欢 qwen 的声音。

- **录音样本**：iPhone 语音备忘录录制，m4a（AAC），单声道，44.1 kHz，17.4 秒，停顿都小于 1 秒，峰值 -4 dB。用 ffmpeg 转成 24 kHz 单声道 16-bit WAV 后上传。
- **复刻出的声音**（两个都已在百炼后台创建，状态 `OK`，长期有效）：

| 模型 | voice_id | 用途 |
|---|---|---|
| qwen-audio-3.1-tts-plus | `qwen-audio-3.1-tts-plus-nannan-4d9b8cad77e74df6903490a146e2e6ae` | **正式使用**，已写入 `.env`：`TTS_MODEL`、`TTS_VOICE_ID` |
| cosyvoice-v3.5-plus | `cosyvoice-v3.5-plus-nannan-c68b27b18e254cf6a47b6819998a63a8` | 对比用，暂时保留 |

- **测试内容**：3 句话（「别闹啦」「好哒 收到」「周活动快结束啦，记得去兑换奖励哦」），每句配 3 种语气（不加指令 / 「语气开心、轻快」/「撒娇的语气，声音软软的」），两个模型共 18 段，全部成功。
- **合成耗时**：非流式 HTTP，每段 1～2 秒（含下载）。
- **计费单位**：qwen-audio-3.1-tts-plus 的 usage 同时返回 token 和字符数（文档说明 plus 按字符计费）；cosyvoice 按字符计费。本次总量不到 200 字。

### 调用要点（实测可用）

- **地址**：用项目现有的业务空间地址，把 `DASHSCOPE_BASE_URL` 末尾的 `/compatible-mode/v1` 换成 `/api/v1/services/audio/tts/...`；鉴权用现有的 `DASHSCOPE_API_KEY`。
  - 复刻：`POST .../customization`，body 为 `{"model":"voice-enrollment","input":{"action":"create_voice","target_model":...,"prefix":"nannan","audio":"data:audio/wav;base64,...","language_hints":["zh"]}}`，从 `output.voice_id` 取 ID。之后用 `query_voice` 轮询，直到 `status` 为 `OK`。
  - 合成：`POST .../SpeechSynthesizer`，body 为 `{"model":"qwen-audio-3.1-tts-plus","input":{"text":...,"voice":<voice_id>,"format":"wav","sample_rate":24000,"instruction":"语气开心、轻快"}}`。`instruction` 放在 `input` 里，可以不传。
- **下载音频**：返回的是 `output.audio.url`（OSS 签名链接，24 小时有效）。**下载时不能带 Authorization 头**，否则 OSS 会返回 `SignatureDoesNotMatch` 的 XML 报错。
- **代理**：请求不走本地代理，与项目现有百炼客户端的处理方式一致。

### 下一步

1. 打通微信发语音的链路（方案 A：BlackHole 回环 + 模拟按住 Option 录音）。
2. 确定什么时候发语音、发什么内容。
3. 接入 bot：回复模型同时输出这句话的语气，作为 TTS 的 `instruction`。

## 建议推进步骤

1. **手动验证**：装好 BlackHole 后，在「文件传输助手」中按住 Option，用 `afplay` 播放一段 `say` 生成的语音，确认能发出可正常播放的语音条。
2. **确定触发时机**：例如只在有人说"发个语音""想听你声音"时发，或偶尔随机发。需要用户决定。
3. **接入 bot**：写成独立的发语音函数，默认关闭，用配置项开启。

> 按项目规则，安装软件、修改系统音频设置都需要用户明确同意后再执行。

## 参考

- [IT之家：微信 4.1.9 for Mac 发布](https://www.ithome.com/0/942/316.htm)
- [IT之家：PC 版微信 4.1.9 内测](https://www.ithome.com/0/939/485.htm)
- [百炼：声音复刻使用指南](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)
- [百炼：语音合成指令控制（realtime-tts-user-guide）](https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide)
- [百炼：CosyVoice 客户端事件 / 参数](https://help.aliyun.com/zh/model-studio/cosyvoice-client-events)
- [百炼：Qwen-Audio-TTS 客户端事件 / 参数](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-client-events)
- [百炼：MiniMax 同步语音合成 API](https://help.aliyun.com/zh/model-studio/minimax-synchronous-speech-synthesis-api)
- [百炼：声音设计](https://help.aliyun.com/zh/model-studio/voice-design-user-guide)
- [百炼：模型计费](https://help.aliyun.com/zh/model-studio/model-pricing)
