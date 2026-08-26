# Marker 分析 — 插入时机、处理与 TUI 渲染

> 目标文件无指定位置,放在 `docs/marks.md`。
> 本文档是 **live/restore 双世代 marker 体系的完整地图**——每个 marker 何时插入、如何被消费、在 TUI 里怎么渲染。

系统里有两代 marker 体系,它们不冲突但职责分层:**live 时代**(实时流)与 **restore 时代**(会话恢复)。

---

## 1. Live 时代(消息进入 LLM context 之前,`MessageHistory` 数组里)

这些是**正在进行的会话**里的 marker,LLM 在下一轮调用时能看到:

| Marker | 插入点 | 时机 | 消费者 |
|---|---|---|---|
| `[Heartbeat]` | `slife/agent/heartbeat.py:36-40` | 每 idle 60s 心跳触发,turn 的 `user_message` **本身就是**这个字面文本 | LLM(`agent.j2` 9节)、TUI 过滤(`HEARTBEAT_MARK`) |
| `[Schedule <name>]` | `slife/agent/schedules.py:89-97` `trigger_text()` | cron 触发 / `run_schedule_now` 手动补做 / `[Schedule missed]` 开机补报(`:284-293`) | LLM(`agent.j2`)、TUI 过滤 |
| `[Task {corr} from {src}]` | `slife/agent/service.py:2505` | 收到远程 A2A task 时,前置拼接在 task 文本上 | LLM(回合内任务引用) |
| `[Turn: N · start → end]` | `slife/agent/service.py:2032` `_annotate_saved_turn()` | turn **成功落库后**(拿到 rowid),追加到该 turn 的 user message **末尾** | 下一轮 LLM(精确引用)、restore 时 TUI 渲染 |
| `[TrimContext: N]` | `slife/agent/message_history.py:223-249` `append_trim_marker()`,由 `loop.py:559` `_trim_after_save()` 调用 | context 超 ceiling 压缩后,追加到**最后一条 assistant message 末尾** | LLM(被告知哪些旧 turn 被裁剪);TUI 通过 `handler.on_trim` 镜像(`chat.py:307` `set_trim_marker`) |

**保存路径的重要清洗**:`service.py:1846` 在写入 diary 前 `_MH.strip_trim_markers(turn_messages)` —— `[TrimContext: N]` 是**纯运行时**,永不落库(恢复出来的会话本身就是已裁剪状态,再报"过去被裁剪"无意义)。`[Turn: N]` 则反过来是**持久化**的,靠 `_annotate_saved_turn` 写回 live 历史。

---

## 2. Restore 时代(从 diary 重建,`slife/ui/restore.py`)

核心在 `marker_for_channel()`(`restore.py:82-103`)——**restore 时才生成**,由持久化的 `channel` 列驱动:

```
channel=human/空      →  无 marker(absence == human)
channel=subagent      →  [Subagent:{"subagent_name":...}]
channel=heartbeat     →  [Heartbeat:{}]
channel=schedule      →  [Schedule:{"name":...}]
channel=wechat        →  [Wechat:{}]
channel=其他(peer id) →  [Remote:{"peer_id":...}]
```

插入顺序(`restore.py:236-254`):

1. 先 `_strip_legacy()` 剥掉残留的旧 `[Heartbeat]`/`[Schedule …]` 前缀(旧行兼容)
2. marker 前置(identity 在前)
3. `[Turn: N]` footnote 追加到**末尾**(`turn_header()`,`restore.py:245`);heartbeat/schedule 合成触发 turn **跳过 header**

`parse_marker`(`slife/a2a/markers.py:59-82`)同时容错两种 legacy 无 JSON 前缀(`[Heartbeat]`、`[Schedule …]`),保证旧行 restore 统一。`render_marker` 与 `parse_marker` 是纯逆运算(`markers.py:48-82`)。

### 谁被排除在 LLM context 之外

- `to_openai_messages`(`message_history.py:314-344`):剥离内部字段(`thinking`→`reasoning_content`、`images`、`is_error`),但**不碰**上述 marker——它们本就是给 LLM 看的
- 心跳/定时 trigger 的 user message 就是 marker 本身,天然进 context
- 注意:`is_autonomous_trigger`/`is_schedule_trigger`(`schedules.py:49-66`)是**按文本前缀**判定的,由 `_annotate_saved_turn`(`service.py:2016`)用来跳过心跳 turn 的 `[Turn: N]` 注记

---

## 3. TUI rendering — 完整链路

### 3.1 Live 渲染

| 消息类型 | 入口 | 渲染 |
|---|---|---|
| human 输入 | `app.py:800` `add_user_message(raw, prefix="You> ", timestamp)` | `[HH:MM] **You>** text` |
| WeChat | `app.py:846` `prefix="You(Wechat)> "` | 同上但前缀不同 |
| A2A task | `app.py:836` `prefix="{source}(a2a)"` | `← source`(`add_a2a_task_message` 在 `chat.py:171` 是另一种左箭头样式,但 `_on_a2a_activity` 实际走 `add_user_message`) |
| assistant 流式 | `ui/handler.py:66-84` `TUIHandler._ensure_assistant` | `AssistantMessage`:`⟐ 思考`(可折叠)+ `[HH:MM] prefix> 正文` + token usage 页脚 |
| 心跳 act | `service.surface_autonomous` → `app.py:684` | `name_prefix="⚡ 自主: "` |
| 定时 act | `service.surface_schedule` → `app.py:691` | `name_prefix="📅 定时: "` |
| 工具调用 | `ui/handler.py:185` `on_tool_call` | `ToolCallWidget`,跳过 `_` 前缀 harness 工具 |

**子代理 live 渲染**:`handler.py:420` 的默认工厂——所有无自带 handler 的 turn(远程 A2A、subagent 完成通知)共用**同一个** `assistant_prefix=self._assistant_prefix`(即 `Jack> `)。`subagent` 完成通知(`service.py:2627`)live 时**没有专属前缀**——它 `source=SUBAGENT` 被 `inbox.py:177` 判为本地(非 remote),不会触发 task_received,靠默认 TUIHandler 渲染。

### 3.2 Restore 渲染(`restore.py` Phase 3)

- **user 消息**前缀由 `restore_prefix()`(`restore.py:125-145`)从 **marker 解析**:
  - 无 marker → `You> `(human)
  - `[Wechat]` → `You(Wechat)> `
  - `[Subagent]` → `⚙️ subagent> `(`i18n.py:64`,en) —— 与 live 不同!live 时 subagent 完成显示为 `Jack> `,restore 后变成 `⚙️ 子代理> `
  - `[Remote:{peer_id}]` → `{peer_id}(a2a)`
- **user 消息的 `[Turn: N]` footnote** 由 `chat.py:215-224` 专门检测 `TURN_HEADER_PREFIX` 渲染为 **dim italic**(机器元数据样式)
- **assistant 消息**:合成触发 turn(heartbeat/schedule)按 marker 的 kind 决定前缀(`restore.py:368-388`)——`📅 定时` 或 `⚡ 自主`,content 为 `.` 的静默回复直接跳过(`:350`)
- 工具 widget 用 `tool_results`/`tool_errors` lookup(`:266-276`),`is_error` 字段优先、legacy 回退 `startswith("Error")`
- **关键技巧**:restore 期间 `chat_view._autoscroll=False`(`:456`),重建完 `scroll_end()` 只滚一次,消除逐 widget 滚动抖动

---

## 4. 观察到的几处不一致(候选修复)

1. **Subagent 前缀双轨**:live 用默认工厂的 `Jack> `,restore 用 `⚙️ subagent> `。`restore_prefix` 的 docstring 声称"matches the real-time display prefixes used during live operation",但 subagent 这格并不成立。
2. **`add_a2a_task_message` 疑似死代码**:`chat.py:171` 定义了专属的 `← source` 左箭头样式,但实际 A2A task 走 `_on_a2a_activity("task_received")` 的 `add_user_message(prefix="{source}(a2a)")`——前者可能从未被调用。
3. **`Unknown` kind 常量**:`markers.py:29` 定义了 `UNKNOWN`,但 `restore_prefix` 对未知 kind 落到 `You> `,`Unknown` 无处使用。
