# 语音功能验证清单（换机器测试用）

> 2026-11-07 整理。覆盖：语音发送链路、`send_voice.py`、定时推送补发语音、回复消息时发语音（dry_run 和正式发送）。
> 原理与踩坑见 `docs/sound-record.md`；回复语音的设计见 `docs/superpowers/specs/2026-11-07-voice-reply-design.md`。
> 每项后面的 `[ ]` 打勾，失败的写下现象和对应的日志行。

---

## 0. 准备

### 0.1 代码和文件

- [ ] 代码已同步到测试机。语音相关改动可能还没提交，确认对应的提交已经推送并拉下来了。
- [ ] 下面这些文件**不在 git 里**，要从原机器手动拷过去：

| 文件 | 用途 |
|---|---|
| `.env` | 百炼 Key、`TTS_MODEL`、`TTS_VOICE_ID`、白名单等 |
| `prompts/voice_reply.md` | 回复语音的 prompt。**缺失时回复语音不会启用**，启动日志会有 `[VoiceReply] 缺少 prompts/voice_reply.md` |

- [ ] 检查一下这些已在 git 里的文件拉下来没有：`data/scheduled_messages.json`、`data/voice_reply.json`、`data/persona.md`。

### 0.2 依赖和权限

```bash
brew install blackhole-2ch switchaudio-osx ffmpeg   # 装 BlackHole 后需要重启电脑
cd wechat-mac-rpa && .venv/bin/python -c "import Quartz, httpx; print('ok')"
```

- [ ] `SwitchAudioSource -a -t input` 的列表里有 `BlackHole 2ch`。
- [ ] 记下本机麦克风的名字（`SwitchAudioSource -c -t input`）：`________________`。不一定是 `MacBook Pro Microphone`。
- [ ] 终端或运行 bot 的程序已获得「辅助功能」权限，用来模拟按下右 Option。
- [ ] 微信已获得「麦克风」权限。
- [ ] 微信版本支持「按住右 Option 录语音」：在任意聊天里手动按住**右** Option 说话，松开后能发出语音。

### 0.3 配置

- [ ] `.env` 里有 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`（北京业务空间）、`TTS_MODEL=qwen-audio-3.1-tts-plus`、`TTS_VOICE_ID=qwen-audio-3.1-tts-plus-nannan-…`。音色属于百炼账号，换机器后用同一个 Key 就能用。
- [ ] 准备一个**测试群**，写进 `.env` 的 `SILENT_WHITELIST`。下面所有「真发」的测试都只在这个群里做，**不要在正式公会群里测**。

---

## 1. 自动化测试

```bash
cd wechat-mac-rpa && .venv/bin/python -m pytest src/tests -q
```

- [ ] 结果是 `542 passed, 13 skipped`，或者更多 passed、0 failed。

---

## 2. 语音链路（`send_voice.py`）

### 2.1 只合成，用扬声器播放

```bash
.venv/bin/python scripts/send_voice.py "测试一下，能听到吗" --mood 开心 --dry-run
```
- [ ] 扬声器能放出囡囡的声音，没有念出 `[excited]` 这类标签。
- [ ] 试几种参数：`--rate 0.8`（明显更慢）、`--rate 1.3`（明显更快）、`--style "尾音拖长一点"`。
- [ ] 超出范围会报错，不会发请求：`--rate 3` 报「rate 取值范围 0.5–2.0」。

### 2.2 真发到文件传输助手

1. 打开微信「文件传输助手」，**不要碰键盘鼠标**。
2. 执行：
   ```bash
   .venv/bin/python scripts/send_voice.py "程序正式版测试，开心模式" --mood 开心
   ```
- [ ] 终端输出 `✅ 已发送`。
- [ ] 文件传输助手里多了一条语音，点开播放，内容完整，开头没有被吞掉。
- [ ] 发完后执行 `SwitchAudioSource -c -t input`，输入设备已经恢复成发送前的麦克风。
- [ ] 发完后右 Option 没有被卡住：随便打几个字母，没有出现特殊符号。

### 2.3 异常时的恢复（可选）

- [ ] 发送过程中按 Ctrl+C 中断：输入设备仍会恢复，右 Option 也会松开。
- [ ] 如果遇到被 `kill -9` 这类强制结束的情况，手动恢复：`SwitchAudioSource -t input -s "本机麦克风名"`，再按一下右 Option。

---

## 3. 感知层识别语音条（正式开启回复语音前必做）

> 对应设计说明 §5 第 1 条和计划 Task 1。

1. 打开文件传输助手，让聊天区里能看到 2.2 发出的语音条。
2. 截图，然后用 bot 的感知管线识别一次。这会产生一次视觉识别费用。
- [ ] 记录 bot 自己发的语音条被识别成：`sender_type=______  message_type=______  text=______`
- [ ] **通过的条件**：被识别为 `SELF`（自己发的），或者直接被忽略。
- [ ] **不通过**：被识别为 `OTHER`（别人发的）或者会触发回复。这时**不要开启**回复语音的正式发送，把结果反馈回来。

---

## 4. 定时推送补发语音

> 用一条临时任务测试，测完删掉。概率为 1 表示必定发语音。

1. 在 `data/scheduled_messages.json` 的 `schedules` 里临时加一条任务，`time` 设为**当前时间 + 3 分钟**：
   ```json
   {"time": "HH:MM", "messages": ["测试定时推送"], "voice_probability": 1,
    "voice_messages": [{"text": "这是定时推送的语音测试哦", "mood": "开心"}]}
   ```
   上一条任务的末尾要补一个逗号，最后一条后面不能有逗号。
2. 启动 bot：`caffeinate -d .venv/bin/python run_bot.py`。
- [ ] 启动时输出里有 `定时推送: … HH:MM(每天) → 测试群`，没有 ❌ 报错。
- [ ] 到点后测试群里**先**出现 `@所有人 前缀：测试定时推送` 文字，**然后**出现一条语音。
- [ ] 日志里有 `[ScheduledPush] HH:MM 语音推送到 '测试群' 完成: {... 'style': ..., 'rate': ..., 'seed': ...}`。style 来自 `voice_styles`，rate 在配置值 ±`voice_rate_jitter` 范围内。
- [ ] 测完删掉这条临时任务，确认顶层 `voice_probability` 仍是你想要的值（目前是 0，即关闭）。

---

## 5. 回复语音：dry_run 观察（`data/voice_reply.json` 里 `dry_run: true`）

1. 确认 `data/voice_reply.json` 里是 `"enabled": true, "dry_run": true`，然后重启 bot。
- [ ] 启动输出里有 `• 回复语音: 已启用，dry_run（只记日志）`。
2. 在测试群里用另一个号发下面这些消息，每条等 bot 回复后再发下一条。看日志用：`grep "\[VoiceReply\]" data/logs/*.log | tail -30`。

| # | 发什么 | 期望的 scene | 期望结果 |
|---|---|---|---|
| 5.1 | `@囡囡 你今天好可爱` | 被夸感谢 | 有 `scene=被夸感谢` 的记录；多试几次，大约 15% 会出现 `本会发语音` |
| 5.2 | `@囡囡 你是不是又偷懒了，笨蛋` | 斗嘴调侃 | `scene=斗嘴调侃` |
| 5.3 | `@囡囡 今天抽卡又歪了，好难过` | 安慰 | `scene=安慰` |
| 5.4 | `@囡囡 今晚几点开打？` | 无 | **没有** scene，或 `decision=不发 reason=无语音场合` |
| 5.5 | `@囡囡 发个语音听听` | 要听语音 | `scene=要听语音`；群里收到一句傲娇拒绝的**文字** |
| 5.6 | 5 分钟内同一个人再发两次 5.5 | 要听语音 | reason 依次为 `第2次,0.6`、`第3次,0.9`，答应时会出现 `本会发语音` |
| 5.7 | `@囡囡 你让他发个语音给我` | 无 | **不应**判成要听语音 |
| 5.8 | 私聊囡囡说「你好可爱」 | — | **没有任何** `[VoiceReply]` 记录 |

- [ ] 上面每一行都符合期望。把判错的那几条记下来：`________________`
- [ ] dry_run 期间，群里**只出现文字**，没有任何语音。
- [ ] 普通回复的风格和长度没有明显变化（群聊 prompt 多了一段，留意一下）。
- [ ] 22:00 以后发 5.1：reason 是 `不在8-22点`。
- [ ] 回复带链接、数字或英文，或者超过 20 字时：reason 是 `含链接/数字/英文/表情` 或 `超过20字`。

---

## 6. 回复语音：正式发送（先通过第 3 节和第 5 节）

1. 把 `data/voice_reply.json` 改成 `"dry_run": false`，然后重启 bot。
- [ ] 启动输出里有 `• 回复语音: 已启用，正式发送`。
2. 为了不用反复试运气，可以**临时**把 `scenes` 里的概率都改成 `1.0`，`request.probabilities` 改成 `[1.0, 1.0, 1.0]`，测完改回来。

| # | 操作 | 期望 |
|---|---|---|
| 6.1 | 发 5.1 | 群里收到一条**语音**，不发文字；日志有 `[VoiceSender] … success=True`；聊天记录里记的是 `[语音] …` |
| 6.2 | 紧接着再发一条 5.1 | 这次是**文字**，reason 是 `上一条已是语音` |
| 6.3 | 发 5.5 | 收到答应的那句**语音**（概率为 1 时） |
| 6.4 | 发送过程中切到别的聊天（模拟窗口不对） | 不发语音，在原来的群里改发文字，日志有 `当前窗口校验失败`；**绝不能**发到别的聊天 |
| 6.5 | 临时把 `.env` 的 `TTS_VOICE_ID` 改错，重启后发 5.1 | 合成失败后改发文字（保留 @）；日志有 `语音发送失败，改发文字` |
| 6.6 | 发送过程中有人用麦克风（例如开着会议） | 发送那几秒对方听不到你说话，发完自动恢复（这是已知限制，只做确认） |

- [ ] 每条语音的语速随字数变化：短句偏慢（约 0.9），长句偏快（约 1.2）。日志里 `[VoiceSender] … tune={'rate': …}` 能看到具体值。
- [ ] 测完把概率改回：被夸感谢 0.15、斗嘴调侃 0.15、安慰 0.2，被要求 `[0.3, 0.6, 0.9]`，然后重启。

---

## 7. 测完恢复

- [ ] 删掉第 4 节加的临时定时任务。
- [ ] `data/voice_reply.json` 的 `dry_run` 按决定设置；不确定就保持 `true`。
- [ ] `.env` 里改过的 `TTS_VOICE_ID` 改回正确值。
- [ ] 输入设备是本机麦克风：`SwitchAudioSource -c -t input`。
- [ ] 文件传输助手和测试群里的测试语音可以手动删掉。

---

## 结果汇总

| 节 | 结果 | 备注 |
|---|---|---|
| 1 自动化测试 | | |
| 2 语音链路 | | |
| 3 感知层识别 | | |
| 4 定时推送语音 | | |
| 5 回复语音 dry_run | | |
| 6 回复语音正式发送 | | |
