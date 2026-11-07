# 回复消息时不定时发语音 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 群聊回复时，在「被夸感谢 / 斗嘴调侃 / 安慰」场合按概率、在「要听语音」时傲娇地，用复刻声音发语音代替文字；第一版 dry_run 只记日志。

**Architecture:** LLM 生成回复时顺带输出 `voice` 标注（场合、心情、答应句）；纯逻辑模块 `voice_policy` 依据配置和硬条件决定发不发、发什么、用什么 style 与语速；`wechat_bot` 的发送段按决定调用已有的 `VoiceSender`，失败改发文字。

**Tech Stack:** Python 3、pytest、现有 `src/action/voice_sender.py`、`src/bot/scheduled_push.py`。

**Spec:** `docs/superpowers/specs/2026-11-07-voice-reply-design.md`

## Global Constraints

- 只在群聊（`is_group=True`）生效；私聊不调用策略、不计次数。
- 时间窗口 `[8:00, 22:00)`：8:00 可以，22:00 不可以。
- 语音文本去掉 @ 和首尾空白后不超过 20 字（主动与被要求都一样），超过不发语音。
- 主动场合概率：被夸感谢 0.15、斗嘴调侃 0.15、安慰 0.2；心情白名单：撒娇、调皮、开心、安慰、惊讶。
- 被要求：同一人 10 分钟内第 1/2/3+ 次答应概率 0.3/0.6/0.9；答应后清零；窗口外不计次；掷中但答应句超 20 字或不可念 → 按拒绝处理、计数不清零。
- 语速：去掉 @ 和标点后字数 n，`base = 0.9 + (min(n, 20) - 1) / 19 * 0.3`，叠加 `voice_rate_jitter` 浮动后限制在 [0.9, 1.2]，保留 2 位小数。
- style 与 `voice_rate_jitter` 读 `data/scheduled_messages.json`（`voice_styles` 按心情）。
- 配置 `data/voice_reply.json` 不存在或 `enabled=false` → 完全不启用，system prompt 不追加任何内容。
- 初版 `dry_run: true`。
- 测试命令：`.venv/bin/python -m pytest`（在 `wechat-mac-rpa` 目录）。
- **不要提交**：项目约定只在用户要求时提交，计划里没有 commit 步骤。
- 不改无关代码，不格式化、不重构。

## Review Focus

1. LLM 的 `voice` 字段是字符串、列表、缺字段、`scene` 不在枚举内、或 `generator` 是 Mock（`last_voice_meta` 为 Mock）→ 一律当作「无」，文字回复照常。（Task 2 `test_garbage_meta_is_none`、Task 4 `test_mock_generator_meta_ignored`）
2. 回复被 `_apply_at_policy` 加了「@张三 」前缀 → 计数和念出的文本都不含 @。（Task 2 `test_mentions_stripped_before_count_and_speech`）
3. 静默模式下非白名单群 → 不发语音，走原文字路径（被静默跳过）。（Task 4 `test_silent_non_whitelist_never_voices`）
4. Self-Refine 把回复改写成超过 20 字 → 不发语音（策略只看最终 replies）。（Task 2 `test_long_final_reply_no_voice`）
5. 语音发送失败（合成失败、窗口校验失败）→ 改发同样文字，且只发一次。（Task 4 `test_voice_failure_falls_back_to_text_once`）

---

### Task 1: 确认感知层如何识别自己发的语音条（调查，不写产品代码）

**Files:**
- Modify: `docs/superpowers/specs/2026-11-07-voice-reply-design.md`（§5 第 1 条补结论）

- [ ] **Step 1:** 请用户打开「文件传输助手」（里面有此前测试发的语音条），用 `screencapture` 截一张聊天区截图存到 scratchpad。
- [ ] **Step 2:** 用项目现有感知管线（`src/perception/smart_pipeline.py`，bot 实际使用的那条）解析这张截图，打印每条消息的 `text / sender_type / message_type`。会花一次视觉调用的费用，执行前告知用户。
- [ ] **Step 3:** 把结论写进设计说明 §5 第 1 条：自己的语音条被识别成什么。
  - 若被识别为 `SELF` 且文本是类似 `4″` 的时长或被忽略 → 不影响，继续 Task 2。
  - 若被识别为 `OTHER`（别人发的）或会触发回复 → **停下，向用户汇报**，再决定是否加一个感知层归一化任务。

### Task 2: 语音回复策略模块 `voice_policy` 与配置文件

**Files:**
- Create: `src/reply/voice_policy.py`
- Create: `data/voice_reply.json`
- Modify: `src/bot/scheduled_push.py`（新增公开函数 `load_mood_styles`）
- Test: `src/tests/test_voice_policy.py`

**Interfaces:**
- Consumes: `src.action.voice_sender.MOODS`；`src.bot.scheduled_push.load_rate_jitter(path) -> float`、`_load_mood_styles(data: dict) -> dict[str, list[str]]`。
- Produces:
  - `scheduled_push.load_mood_styles(path: Path = CONFIG_PATH) -> dict[str, list[str]]`（包一层 `_load_mood_styles(_read_config(path))`）。
  - `VOICE_REPLY_CONFIG_PATH = PROJECT_ROOT / "data" / "voice_reply.json"`
  - `SCENES = ("被夸感谢", "斗嘴调侃", "安慰", "要听语音")`
  - `@dataclass(frozen=True) class VoiceReplyConfig: dry_run: bool; hours: tuple[int, int]; max_chars: int; rate_by_length: tuple[float, float]; moods: frozenset[str]; scenes: dict[str, float]; request_window_minutes: float; request_probabilities: tuple[float, ...]`
  - `load_voice_reply_config(path: Path = VOICE_REPLY_CONFIG_PATH) -> Optional[VoiceReplyConfig]`：不存在或 `enabled` 为假返回 `None`；单项非法时打印/记录 ❌ 并用 Global Constraints 里的默认值。
  - `parse_voice_meta(raw) -> Optional[dict]`：只接受 dict 且 `scene ∈ SCENES`、`mood ∈ MOODS`；`accept` 只在 `要听语音` 时保留且须为字符串；否则返回 `None`。
  - `strip_mentions(text: str) -> str`：去掉 `@所有人` 与 `@某某`（@ 到下一个空白或 U+2005 为止）。
  - `speech_len(text: str) -> int`：`strip_mentions` 后去掉空白和标点的字数。
  - `is_speakable(text: str) -> bool`：不含 `http`/`www.`、阿拉伯数字（含全角）、英文字母、emoji。
  - `rate_for_length(n: int, lo: float = 0.9, hi: float = 1.2, max_chars: int = 20) -> float`
  - `@dataclass class VoiceDecision: send_voice: bool; reason: str; scene: str = ""; text: str = ""; mood: str = ""; style: Optional[str] = None; rate: Optional[float] = None`
  - `class VoiceReplyPolicy(config: VoiceReplyConfig, mood_styles: dict[str, list[str]], rate_jitter: float, rng: random.Random = random.Random())`
    - `decide(chat: str, sender: str, replies: list[str], meta: Optional[dict], now: datetime) -> VoiceDecision`：`send_voice=True` 时，语音 `text` **替代整批 replies**。`meta` 先经 `parse_voice_meta`。
    - `record_sent(chat: str, was_voice: bool) -> None`：记录该群上一条回复是否语音。

- [ ] **Step 1: 写失败测试** `src/tests/test_voice_policy.py`（用 `VoiceReplyConfig` 直接构造，`rng` 用固定种子或可控的假对象）：
  - `test_hours_window`：`now` 为 7:59 / 22:00 → `send_voice=False`；8:00 / 21:59 且 `rng` 必中 → `True`。
  - `test_proactive_scene_mood_and_probability`：`被夸感谢`+`调皮`、`rng.random()` 返回 0.14 → 发；0.16 → 不发；`安慰` 0.19 → 发；心情 `平静` → 不发；`scene` 为 `无`/缺失 → 不发。
  - `test_proactive_requires_single_short_reply`：两条 replies → 不发；21 字 → 不发；20 字 → 发。
  - `test_mentions_stripped_before_count_and_speech`：reply `"@张三 哼，算你有眼光"` → 发，`decision.text == "哼，算你有眼光"`。
  - `test_unspeakable_rejected`：含 `https://`、`8点`、`OK`、`😀` 各自 → 不发。
  - `test_long_final_reply_no_voice`：meta 合法但 reply 25 字 → 不发。
  - `test_no_consecutive_voice`：`record_sent(chat, True)` 后同群主动场合 → 不发；`record_sent(chat, False)` 后 → 可发；要听语音不受此限。
  - `test_request_escalation`：
    - 同一人 10 分钟内：第 1 次 `rng.random()=0.35` → 不发；第 2 次 0.55 → 发（0.55 < 0.6）。
    - 另一人第 1/2/3 次都是 0.85 → 不发、不发、发（0.85 < 0.9）。
    - 第 1 次之后隔 11 分钟再要，`0.35` → 不发（按第 1 次的 0.3 算）。
    - 答应后立刻再要，`0.35` → 不发（从第 1 次重新算）。
  - `test_request_accept_text_rules`：掷中但 `accept` 为 21 字 → 不发且下次仍按下一次计数；`accept` 合法 → `text == accept`；窗口外要求 → 不发且不计次。
  - `test_rate_for_length`：`rate_for_length(1) == 0.9`、`rate_for_length(20) == 1.2`、`rate_for_length(30) == 1.2`；`decide` 结果 `0.9 <= rate <= 1.2`（jitter 0.1，跑 100 次）。
  - `test_style_from_mood_pool`：`mood_styles={"调皮": ["a","b"]}` → `decision.style in ("a","b")`；心情无候选 → `style is None`。
  - `test_garbage_meta_is_none`：`parse_voice_meta` 对 `"x"`、`[]`、`{}`、`{"scene":"乱写","mood":"开心"}`、`{"scene":"安慰","mood":"暴躁"}`、`Mock()` 都返回 `None`。
  - `test_load_config`：缺文件 → `None`；`enabled:false` → `None`；完整文件 → 字段与 spec 一致；`scenes` 某项为 2 → 报错并用默认值。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest src/tests/test_voice_policy.py -q`，预期 ImportError 失败。
- [ ] **Step 3:** 实现 `src/reply/voice_policy.py` 和 `scheduled_push.load_mood_styles`。判定顺序：解析 meta → 时间窗口 → 分场合（主动：心情白名单、单条、字数、可念、上一条非语音、概率；要听语音：计数、概率、答应句字数与可念）。`reason` 用简短中文写明拦下的那一条（如 `超过20字`、`22点后`）。
- [ ] **Step 4:** 新建 `data/voice_reply.json`，内容与 spec §4.3 一致（`dry_run: true`），并加一个 `_说明` 字段说明各项。
- [ ] **Step 5:** 运行 `.venv/bin/python -m pytest src/tests/test_voice_policy.py src/tests/test_scheduled_push.py -q`，预期全部通过。

### Task 3: prompt 与生成器解析 `voice` 标注

**Files:**
- Create: `prompts/voice_reply.md`
- Modify: `src/reply/generator.py`（`__init__`、`generate` 中 `system_prompt` 生成之后、`_react_generate` 首次 `_parse_replies` 处）
- Test: `src/tests/test_reply.py`（追加）

**Interfaces:**
- Consumes: `voice_policy.parse_voice_meta`。
- Produces: `ReplyGenerator(..., voice_prompt: str = "")`；属性 `last_voice_meta: Optional[dict]`（每次 `generate` 开头重置为 `None`）。

- [ ] **Step 1:** 写 `prompts/voice_reply.md`：说明可在 JSON 里额外输出 `voice` 字段；列出 spec §3.3 的五种场合及「算 / 不算」表；`mood` 只能从 12 个心情里选；`要听语音` 时 `replies` 写傲娇拒绝、`voice.accept` 写勉强答应的一句（20 字以内）；三个输出示例照 spec §4.1。
- [ ] **Step 2: 写失败测试**（`src/tests/test_reply.py`）：
  - `test_voice_prompt_appended_only_for_group`：`voice_prompt="VOICE"` 时群聊 `last_system_prompt` 以 `"VOICE"` 结尾、私聊不含；`voice_prompt=""` 时与原来完全相同。
  - `test_last_voice_meta_from_first_generation`：LLM 首次返回 `{"replies":["哼"],"voice":{"scene":"被夸感谢","mood":"调皮"}}` → `last_voice_meta == {"scene":"被夸感谢","mood":"调皮"}`；返回不带 `voice` → `None`；Iterate 返回的 JSON 带不同 `voice` 也不覆盖首次结果。
- [ ] **Step 3:** 实现：`generate` 里 `system_prompt = self._system_prompt(...)` 之后，`if is_group and self.voice_prompt: system_prompt += "\n\n" + self.voice_prompt`；`_react_generate` 在首次成功 `_parse_replies` 的地方用 `parse_voice_meta(self._extract_json(text).get("voice"))` 赋值 `last_voice_meta`（仅当尚为 `None`）。`_iterate` 不动。
- [ ] **Step 4:** 运行 `.venv/bin/python -m pytest src/tests/test_reply.py src/tests/test_react_self_refine.py -q`，预期通过。

### Task 4: bot 发送段接入（dry_run、真发、兜底、记录）

**Files:**
- Modify: `src/bot/wechat_bot.py`（`__init__` 约 85–98 行；逐条发送段约 610–650 行；`_send_voice_line` 附近复用 `VoiceSender`）
- Test: `src/tests/test_bot.py`（追加，沿用 `bot` fixture 与 `test_partial_send_failure_does_not_retry_delivered_reply` 的写法）

**Interfaces:**
- Consumes: Task 2 的 `load_voice_reply_config`、`VoiceReplyPolicy`、`load_mood_styles`、`load_rate_jitter`；Task 3 的 `voice_prompt` 参数和 `last_voice_meta`；`ChatOpener(self.perception).open(chat) -> bool`；`VoiceSender.send(text, mood, rate=..., style=...) -> ActionResult`；`self.sender.silent_mode`、`self.sender.in_silent_whitelist(chat) -> bool`。
- Produces: `self.voice_policy: Optional[VoiceReplyPolicy]`、`self.voice_reply_config: Optional[VoiceReplyConfig]`；方法 `_try_send_voice_reply(chat_name: str, decision: VoiceDecision) -> ActionResult`。

- [ ] **Step 1: 写失败测试**（`bot.voice_policy` 用 Mock 返回指定 `VoiceDecision`，`bot._voice_sender` 用 Mock）：
  - `test_dry_run_logs_and_sends_text`：`voice_reply_config.dry_run=True`、决定发语音 → `sender.send` 照常发原 replies，`_voice_sender.send` 未调用，日志含 `[VoiceReply] 本会发语音`。
  - `test_voice_replaces_text_and_records_tag`：`dry_run=False` → `_voice_sender.send` 调用一次，`sender.send` 未调用，`mark_replied` 的回复文本为 `"[语音] 哼，算你有眼光"`，`record_sent(chat, True)`。
  - `test_voice_failure_falls_back_to_text_once`：`_voice_sender.send` 返回失败 → `sender.send` 以 `decision.text` 调用恰好一次，`record_sent(chat, False)`；`ChatOpener.open` 返回 False 同样兜底。
  - `test_silent_non_whitelist_never_voices`：`sender.silent_mode=True`、`in_silent_whitelist` 为 False → `_voice_sender.send` 未调用。
  - `test_private_chat_skips_policy`：私聊 → `voice_policy.decide` 未调用。
  - `test_mock_generator_meta_ignored`：`voice_policy` 为真实对象、`generator` 为 Mock（`last_voice_meta` 是 Mock）→ 不抛异常、文字照发。
  - `test_disabled_config_no_prompt`：`load_voice_reply_config` 返回 `None` → `voice_policy is None`，`ReplyGenerator` 收到 `voice_prompt == ""`。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest src/tests/test_bot.py -q`，预期新测试失败。
- [ ] **Step 3:** 实现：
  - `__init__`：读配置；启用时构造 `VoiceReplyPolicy(config, load_mood_styles(), load_rate_jitter())`，并把 `prompts/voice_reply.md` 内容作为 `voice_prompt` 传给 `ReplyGenerator`；未启用时两者分别为 `None` 和 `""`。
  - 发送段：`if self.voice_policy and is_group:` 调 `decide(chat_name, to_reply[-1].sender, replies, getattr(self.generator, "last_voice_meta", None), datetime.now())`，每次记 `[VoiceReply] chat=… sender=… scene=… decision=… reason=…`。
  - `dry_run` → 只记 `[VoiceReply] 本会发语音: {text} mood=… style=… rate=…`，走原逻辑。
  - 真发 → `_try_send_voice_reply`：静默检查 → `ChatOpener.open` → `VoiceSender.send`；成功则 `sent_replies=["[语音] " + text]`、写 pending self 消息与 `log_send`；失败则 `sender.send(decision.text, chat_name=chat_name)` 走原成功/失败分支。
  - 无论哪条路径，发送结束后 `voice_policy.record_sent(chat_name, was_voice)`。
- [ ] **Step 4:** 运行 `.venv/bin/python -m pytest src/tests -q`，预期全部通过（基线 513 passed）。

### Task 5: 文档

**Files:**
- Modify: `docs/sound-record.md`（新增「Bot 集成：回复消息时发语音」一节）
- Modify: `docs/superpowers/specs/2026-11-07-voice-reply-design.md`（§4.1：改为启用时给群聊追加 `prompts/voice_reply.md`，不改 `data/persona.md`）

- [ ] **Step 1:** `sound-record.md` 新节写明：配置文件位置与各字段、dry_run 怎么看日志（`grep "\[VoiceReply\]"`）、如何正式开启（`dry_run` 改 `false` 后重启）、私聊不发、时间窗口、20 字与语速规则。
- [ ] **Step 2:** 更新设计说明 §4.1 的 prompt 修改方式。
- [ ] **Step 3:** 向用户汇报：改动文件清单、测试结果、需要重启 bot 才生效、观察期建议看哪些日志。
