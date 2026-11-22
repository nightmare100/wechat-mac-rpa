# 群聊表情包回复设计（Sticker Reply）

- 日期：2026-11-22
- 状态：已确认并实现

## 1. 目标与动机

让 Bot 在群聊回复中能够像真人一样偶尔搭配表情包图片，增强软萌女生群友的人设真实感，同时不破坏现有文字/语音边界、不刷屏、不增加维护负担。

### 非目标与反目标
- 不操作微信内置“收藏表情”面板（避免脆弱的 UI 坐标点击）。
- 不自动抓取/复用群友发来的陌生表情包（避免画风杂乱和合规风险）。
- 不把表情包变成每句必发的固定模板，也不在越界拒绝、严肃求助、对方严重难过时发表情包。

## 2. 方案选择（Approaches）

| 方案 | 做法 | 权衡 | 结论 |
|---|---|---|---|
| **方案 A（采用）** | `data/stickers/<标签>/` 本地分目录存图 + 模型输出可选 `{"sticker": {"tag", "mode"}}` + 程序 `StickerPolicy` 硬控概率/冷却/去重 | 语义准确，能区分 `append`/`replace`，增删文件夹零代码生效；开启群的 system prompt 增加 ~150 字 | **已选定** |
| 方案 B | 纯正则关键词后处理选图，不改 prompt | 零 prompt 开销，但易误判反讽/拒绝，难以判断何时适合纯图替代 | 放弃 |
| 方案 C | VLM 预打标 + 语义向量检索单张图 | 几十张常用表情包过度设计，引入额外预处理依赖和延迟 | 放弃 |

## 3. 素材目录与配置

### 3.1 素材目录（不入 Git）
默认目录：`data/stickers/`（按子文件夹名作为标签名，支持 `.gif`、`.png`、`.jpg`、`.jpeg`、`.webp`）：

```text
data/stickers/
├── 开心/
├── 谢谢/
├── 卖萌/
├── 无语/
├── 晚安/
└── 加油/
```

- 扫描规则：自动忽略隐藏文件、空目录及非图片文件；按目录 mtime/内容动态扫描，拖入新图或新建标签目录无需改代码。

### 3.2 配置文件（`data/sticker_reply.json`，不入 Git）
参考 `data/voice_reply.json` 风格，提供模板 `prompts/sticker_reply.json.example`：

```json
{
  "enabled": true,
  "dry_run": false,
  "stickers_dir": "data/stickers",
  "append_probability": 0.25,
  "replace_probability": 0.15,
  "cooldown_replies": 3,
  "cooldown_minutes": 3,
  "recent_no_repeat": 5,
  "replace_max_chars": 8
}
```

- **文件不存在或 `enabled` 为 `false`**：功能完全关闭，零副作用。
- **按群覆写**：接入现有 `data/group_config.json`，新增布尔键 `sticker_reply`（未配置时继承 `sticker_reply.json` 的 `enabled`）。

## 4. 提示词与输出契约

新增 `prompts/sticker_reply.md`（仅对启用 `sticker_reply` 且素材目录非空的群聊追加到 system prompt），动态填入当前可用标签列表（如 `开心、谢谢、卖萌、无语、晚安、加油`）：

- 允许模型在正常 JSON 中**可选**附带：
  ```json
  {"replies": ["@李桑 谢谢呀"], "sticker": {"tag": "谢谢", "mode": "append"}}
  ```
- `mode` 取值：
  - `append`：先发文字，再补发 1 张表情包（大多数场景）。
  - `replace`：只发 1 张表情包替代文字（仅限纯哈哈、收到、谢谢、晚安等极短反应）。
- 明确约束：只在自然合适时输出，不必每条都带；拒绝越界、严肃问题、对方明显难过时不输出 `sticker`。
- `ReplyGenerator` 解析 `last_sticker_meta`（与 `last_voice_meta` 同构；Self-Refine 采纳新稿时同步更新或清空，采用沉默时清空）。

## 5. 程序硬控策略（`src/reply/sticker_policy.py`）

纯逻辑模块，不碰微信 UI，输入 `(chat_name, replies, sticker_meta, voiced, now)`，输出 `StickerDecision(send_sticker, mode, image_path, tag, reason)`：

1. **互斥与前置检查**：
   - `voiced=True`（本轮已改发语音）→ 不发表情包。
   - `sticker_meta` 为空、或 `tag` 不在当前非空素材目录中 → 不发。
   - `mode == "replace"` 时，仅当 `len(replies) == 1` 且去掉 `@昵称` 后正文长度 `<= replace_max_chars`（默认 8 字）才允许替代；否则自动降级为 `append`（防止把有信息量的长回答误替换成纯图）。
2. **防刷屏冷却（按群独立）**：
   - 距离本群上次发表情包，回复条数 `< cooldown_replies`（默认 3 条）**或** 时间 `< cooldown_minutes`（默认 3 分钟）→ 不发。
3. **概率抽签**：
   - `append` 用 `append_probability`（默认 0.25），`replace` 用 `replace_probability`（默认 0.15）。
4. **选图去重**：
   - 从该 `tag` 目录下所有有效图片中排除最近 `recent_no_repeat`（默认 5）张已发图片后随机选 1 张；若目录图少于去重窗口，则放宽为仅排除上一张。

## 6. 发送与状态记录（`src/bot/wechat_bot.py`）

- 仅群聊且 `group_config.is_enabled(chat_name, "sticker_reply", default=True)` 时执行。
- `dry_run=True`：只打 `[StickerReply] 本会发表情: ...` 日志，不实际发图，文字照常发。
- `dry_run=False`：
  - `mode == "append"`：先正常 `_send_text` 发文字，再调用现成 `_send_image(chat_name, decision.image_path)` 发图；图片发送失败只记警告，不影响文字已发送状态。
  - `mode == "replace"`：直接调用 `_send_image`；若发图失败，自动回退发送原文字 `replies`，确保不漏回。
- 发送成功后，向 `pending_self_messages` 记一条 `[表情包: {tag}]`（方便上文知道自己刚发过表情），并调用 `sticker_policy.record_sent(chat_name, image_path, sent=True)`。

## 7. 测试与验证计划

1. `test_sticker_policy.py`：目录扫描、非法配置降级、语音互斥、replace 长度降级、`cooldown_replies`/`cooldown_minutes`、最近不重复选图、按群独立状态。
2. `test_reply.py` / `test_react_self_refine.py`：仅开启群追加 `sticker_reply.md`、`last_sticker_meta` 解析及 Self-Refine 修订后同步更新/清空。
3. `test_group_config.py`：按群覆写 `sticker_reply` 开关生效。
4. 现有核心测试套件回归通过（不触碰真实微信发送）。
