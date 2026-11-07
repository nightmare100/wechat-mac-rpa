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

## 发送语音程序：`scripts/send_voice.py`

> 2026-11-05 新增。核心逻辑在 `src/action/voice_sender.py`，单元测试在 `src/tests/test_voice_sender.py`。
> 状态：合成部分已实测；程序的正式发送还**没有**实测（链路本身已经用测试脚本验证过，见上文）。

### 前提

- 已安装 BlackHole 2ch 和 SwitchAudioSource（见上文「本机环境现状」）。
- 运行命令的终端有「辅助功能」权限（模拟按右 Option 需要）。
- 微信已登录，并且**已经打开要发送的聊天窗口**。程序只发到当前窗口，不会切换会话。
- 发送期间（约「语音时长 + 3 秒」）不要碰键盘和鼠标。

### 命令（在 `wechat-mac-rpa` 目录下执行）

```bash
# 发到微信当前聊天窗口
.venv/bin/python scripts/send_voice.py "周活动快结束啦，记得去兑换奖励哦" --mood 开心

# 不指定心情时默认「平静」
.venv/bin/python scripts/send_voice.py "收到啦"

# 只合成，并用扬声器播放试听，不发微信
.venv/bin/python scripts/send_voice.py "测试一下" --mood 撒娇 --dry-run

# 列出所有可选心情
.venv/bin/python scripts/send_voice.py --list

# 微调语速、音量，补充语气描述（都可选，不填用接口默认值）
.venv/bin/python scripts/send_voice.py "大宝，讨厌" --mood 撒娇 --rate 0.9 --volume 60 --style "尾音拖长一点"
```

| 参数 | 范围 | 默认 | 说明 |
|---|---|---|---|
| `--rate` | 0.5–2.0 | 1.0 | 语速，直接传给接口 `input.rate` |
| `--volume` | 0–100 | 50 | 音量，`input.volume` |
| `--style` | 自由文字 | — | 接在心情语气后面，用「；」连接，一起作为 `instruction` 传 |

超出范围时直接报错，不会发请求。实测（2026-11-06，复刻声音，同一句话，心情为平静）的时长：默认 3.52s，`--rate 0.8` 4.50s，`--rate 1.3` 2.89s，`--style "语速很慢"` 5.52s。说明复刻声音认 `rate` 参数，用文字描述也能调慢。

接口也有 `pitch`（音调）参数，但实测对复刻声音没有明显作用，已从程序中删掉。要调高低，用 `--style`，例如 `"声音稍微高一点"`。

- 成功时输出 `✅ 已发送`，退出码为 0；失败时输出 `❌ 发送失败：原因`，退出码为 1。
- 文本里可以直接写富语言标签，会原样交给模型。例如 `"好啦[giggles]别闹了"`，可用的有 `[giggles]` `[laughing]` `[sighing]` `[gasp]` `[clears throat]` `[cough]` `[snorts]`。

### 可选心情（`--mood`）

每种心情 = **控制类标签**（加在文本开头）+ **语气说明**（`instruction`）。两者同时使用（用户决定）。

| 心情 | 标签 | 语气说明 |
|---|---|---|
| 平静（默认） | — | — |
| 开心 | `[excited]` | 语气开心、轻快 |
| 撒娇 | —（文档里没有对应的标签） | 撒娇的语气，声音软软的 |
| 调皮 | `[mischievously]` | 调皮、俏皮的语气 |
| 好奇 | `[curious]` | 好奇地问 |
| 惊讶 | `[amazed]` | 惊讶的语气 |
| 难过 | `[sad]` | 有点难过、低落 |
| 生气 | `[angry]` | 有点生气，但不凶 |
| 疲惫 | `[tired]` | 有气无力、很累 |
| 安慰 | `[empathetic]` | 温柔地安慰对方 |
| 不情愿 | `[reluctantly]` | 不太情愿的语气 |
| 悄悄话 | `[whispers]` | 小声说悄悄话 |

- 要增减心情或修改语气说明，改 `src/action/voice_sender.py` 里的 `MOODS`。
- 千问没有固定的 emotion 参数，只有两种控制方式：写进文本的标签，和自由文本的 `instruction`。
  - 文档共列出 30 个标签：控制类 23 个，如悲伤、惊叹、愤怒、兴奋、讽刺、好奇、无聊、疲倦、轻蔑、大喊、惊慌、调皮、共情、耳语、不情愿、哭泣、严肃、很慢、很快等；富语言类 7 个，即上面列出的拟声标签。
  - 文档**没有说明**复刻的声音是否支持标签。实测 12 种心情的时长：带标签的只比「平静」多 0.2～0.6 秒，不像把标签念了出来；但是否真的没念出来，还需要试听确认。

### 配置（`.env`，复用项目现有的百炼配置）

| 变量 | 说明 |
|---|---|
| `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` | 已有的百炼鉴权和业务空间地址；程序会把地址末尾换成 TTS 接口 |
| `TTS_MODEL` | `qwen-audio-3.1-tts-plus` |
| `TTS_VOICE_ID` | 复刻出的声音 ID（必须与 `TTS_MODEL` 对应） |
| `VOICE_INPUT_DEVICE` | 可选，虚拟声卡名，默认 `BlackHole 2ch` |

### 程序做了什么

1. 合成：调用百炼，再从 OSS 下载 wav（下载时不带鉴权头）。
2. 检查：
   - 语音时长不超过 58 秒（微信单条上限 60 秒，减去前后等待的时间）；
   - 按名字查找 BlackHole 在 ffmpeg 里的设备序号，找不到就直接失败，不做任何操作。
3. 激活微信 → 输入切到 BlackHole → 按下右 Option → 等 1.5 秒 → 播放 → 等 0.5 秒。
4. 收尾（无论成功还是失败都执行）：松开右 Option，把输入切回原来的设备，删除临时 wav。

### 已知坑

- **wav 时长不能读文件头**：百炼返回的 wav 头里，长度字段是流式占位值（约 2 GB）。用 `wave.getnframes()` 会算成 44739 秒，所以要按实际字节数计算。
- **播放中途失败也会松开 Option**：这时微信可能已经录下半截语音并发出去，程序会返回失败，需要人工检查。
- **发送前无法校验内容**：语音没有文字消息那样的「复制回来比对」，录坏了也会直接发出，只能在 2 分钟内手动撤回。

## Bot 集成：定时推送补发语音

配置在 `data/scheduled_messages.json`，逻辑在 `src/bot/scheduled_push.py`。

- 到点后照常发带 @所有人 的文字。文字发送**成功后**，按 `voice_probability` 的概率，从该条的 `voice_messages` 里随机选一句，再补发一条语音。
- `voice_probability`：取值 0～1。顶层是默认值，每条定时任务可以单独覆盖。不配置时为 0，即不发语音。有多个群时，每个群各自掷一次概率。
- `voice_messages`：每句写成 `{"text", "mood", "rate", "volume", "style"}`，除 `text` 外都可选，参数含义和 `send_voice.py` 相同。也可以直接写一个字符串，此时心情为平静、语速 1.0。没有配置 `voice_messages` 的定时任务只发文字。
- 随机性：
  - `style` 可以写成列表，每次随机选一个。
  - 顶层 `voice_styles` 按心情配 style 候选，当前 8 种心情各 10 个。句子没写 style 时，从它心情对应的候选里随机选；句子自己写了 style 的，优先用句子的。
  - 实测 style 对效果影响很大，连节奏都会变。例如同一句撒娇，换 style 后时长在 4.6 秒到 6.7 秒之间。
  - 实测同时发 4 个合成请求会触发百炼限流（HTTP 429 `Throttling.RateQuota`），改成一条一条发就正常。bot 每次只发一条语音，不受影响。
  - 顶层 `voice_rate_jitter`（0～0.5，不配置为 0）让语速在配置值的基础上随机浮动，没配语速的句子以 1.0 为基准。当前配置为 0.05。
  - 每次合成都随机换 `seed`（0～65535）。`seed` 对复刻声音有多大影响还没有实测。
- 心情名称不存在或数值超出范围的条目，在 bot 启动时报 ❌ 并跳过。
- 发语音前会重新打开群并校验标题，校验不通过就不发，避免发错群。语音失败只记日志，不重试，不影响已发出的文字。
- 发一条语音会让主循环等待大约「合成时间 + 语音时长 + 3 秒」。

## Bot 集成：回复消息时发语音

设计见 `docs/superpowers/specs/2026-11-07-voice-reply-design.md`，逻辑在 `src/reply/voice_policy.py`，配置在 `data/voice_reply.json`，prompt 在 `prompts/voice_reply.md`。配置和 prompt 都改完要重启 bot。

- **只在群聊**，并且只在 **8:00–22:00**（不含 22:00）发。
- LLM 生成回复时会顺带标注场合和心情，`{"voice": {"scene", "mood", "accept"}}`。**发不发由程序决定**：
  - **主动发**：场合为被夸感谢（0.15）、斗嘴调侃（0.15）或安慰（0.2），括号里是概率。还要同时满足：心情在白名单内（撒娇、调皮、开心、安慰、惊讶），只有 1 条回复，去掉 @ 后不超过 20 字，不含链接、数字、英文、表情（包括 `[捂脸]` 这类），上一条回复不是语音。
  - **被要求时（傲娇）**：同一个人 10 分钟内第 1、2、3 次及以后，答应的概率分别是 0.3、0.6、0.9。答应就把 `accept` 那句作为语音发出，并且这个人的计数清零；不答应就发 `replies` 里拒绝的那句文字。时间窗口外不计次数。
- 语音**替代**文字。语速按字数从 0.9（1 字）线性涨到 1.2（20 字），再叠加 `voice_rate_jitter` 浮动，最后限制在 0.9–1.2。style 从 `voice_styles` 里按心情随机选。
- 发送前会做三件事：检查静默白名单，校验当前窗口就是这个群，然后合成并发送。任何一步失败都会改发文字：主动场合发原来的回复（保留 @），被要求时发答应的那句。
- 聊天记录和记忆里记成 `[语音] 内容`。
- **观察期**：`dry_run: true` 时只写日志、照常发文字。看日志用：
  ```bash
  grep "\[VoiceReply\]" data/logs/*.log | tail -50
  ```
  每条有标注的回复都会记 `scene=… decision=发/不发 reason=…`；命中的还会多一行 `本会发语音: 文本 mood=… style=… rate=…`。确认标注准确后，把 `dry_run` 改成 `false` 再重启，就正式开启了。
- **正式开启前还要确认一件事**：bot 自己发出的语音条会被感知层识别成什么（设计说明 §5 第 1 条）。

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
