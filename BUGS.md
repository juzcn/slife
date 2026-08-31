[16:27] slife> 好问题，这个值得认真回答。作为 agent，我每天"看"的就是这份 system prompt + 工具 schema，确实有几个地方让我困惑或觉得可以改。按严重程度排：

## 🔴 能造成实际困扰的

**1. `_sys_note` 的指令自相矛盾**
系统提示明确写着 *"Never call single-underscore tools (`_sys_note`)... treat them as reserved"*，但实际每个 turn 系统提醒都会让我调用它来获取上下文状态。我每次都要纠结：这算不算违反规则？建议：要么把它改成真正 auto-invoked（当前 reminder 说 "call _sys_note" 其实就是让我手动调），要么把"reserved"改成"仅由系统提醒触发时调用"。

**2. 上下文使用率数字不可信**
`_sys_note` 报告的 context usage 波动很怪：243 → 21,913 → 44,451 → 243 → 46,324... 我在 turn 4 时被裁剪过吗？没有收到明确的"移除通知"，但数字从 44k 掉到 243 又涨回来。我无法判断**真实的**上下文占用和是否发生了裁剪。改进：`_sys_note` 返回里明确标注 trim 事件和当前加载的 turn 范围（而不是只给 token 数）。

**3. `mcp_tool_load` 必须下一轮才能用**
返回 "callable from the next turn" —— 意味着 search → load → call 至少要 3 轮往返。如果 load 后能立即同步拿到 schema 并在**同一轮**调用，能省很多延迟和 token。

## 🟡 设计层面可以更好

**4. 三个 meta-参数在 60+ 个工具里重复**
`_timeout` / `_async` / `_approve` 每个工具 schema 都重复一遍，占了不少 token（我光看 schema 就花掉很多上下文）。可以做成全局约定写一次，工具 schema 只写业务参数。

**5. `turn_summarize` 的时机很尴尬**
说明里说 turn 内调用会"applied when it completes"——但我根本不知道 turn 什么时候结束，也无法预知。结果就是所有历史 turn 的 summary/tags 都是空的（你刚才在 turn_search 里看到了）。建议：harness 在 turn 结束后**自动**生成摘要，或者至少让我能对**上一轮**补打。

**6. 微信消息读取是破坏性的**
`wechat_check_messages` 说明 "consumed on read"——我读一次消息就没了。如果我在重启边界或误读时调用，消息可能丢失且无法恢复。建议加一个 non-destructive 的 peek 模式。

**7. memdb / memfiles 的语义索引状态要查两次**
`memdb_semantic_status` 和 `memfiles_semantic_status` 是两个工具，我每次都要各调一次。既然共享同一 embedding 配置，可以合并成一个返回双状态。

## 🟢 小改进

- **`attach_image` 在 vision disabled 时仍然暴露**——我永远不该调用它（模型收不了图），却还占着工具位。disabled 时应该直接从我的工具列表隐藏。
- **`turn_search` 的 distance 是原始 L2 距离**（我看到 18~22 的值），没有归一化，我很难判断"多大算相关"。给个 0-1 的分数或阈值提示会清晰很多。
- **`_timeout` 默认值不透明**——MCP 服务器拉起、媒体生成这类慢操作，schema 没写默认超时是多少，我经常得猜要不要传 `_timeout`。
- **`wechat_login` 的二维码只有 ASCII**——今天已经实测翻车（终端渲染不可靠）。工具应该直接返回图片路径或二维码内容，让我能生成 PNG 打开。

---

最讽刺的是第 1 条：**系统提示让我别调 `_sys_note`，但系统提醒又让我调它**。这是我最想改的。如果你在维护这套 harness，这几条应该是具体的、可落地的反馈。要不要我挑其中一条（比如 `_sys_note` 的语义澄清，或 mcp load 同轮可用）展开讲讲期望的行为？