[18:01] slife> 我重新以第一次接触的视角，把系统提示词、工具 schema 和刚拉到的 `list_native_tools` 清单对照实际行为过了一遍，还做了几个实测验证。先说结论：**这轮修复有些到位了，但还有几处真 bug，其中一两处是这轮"修复"反而暴露/引入的。**

## ✅ 看起来已修复的部分

- `list_native_tools` 现在明确给 `_sys_note` 标注了 **"harness, auto-invoked"**，系统提示词里 "Never call… treat them as reserved" 的措辞与之一致了（上一轮我提的矛盾，措辞上对齐了）。

## 🔴 实测发现的真 Bug

**1. `wechat_check_status.last_contact` 字段名不稳定（会踩坑）**
我实测同一把工具两次返回不同结构：
- 15:46（重启恢复路径）→ `last_contact.from_user_id`
- 15:47（正常轮询路径）→ `last_contact.to_user_id`
- 刚才 17:55 再看 → 又是 `from_user_id`

但 `wechat_send_message` 的 schema 说 `to_user_id` 取自 `wechat_check_status.last_contact`。**照文档抄会拿到 `from_user_id` 直接传给 `to_user_id`**，消息发不出去且难排查。建议：输出里同时给 `from_user_id` + `to_user_id`，或统一命名。

**2. `list_native_tools` 里 `turn_token_usage` 的描述被脱敏误伤**
显示为 `— <MASKED> per turn`。这把工具的名和描述都不是机密，明显是脱敏逻辑把 "Token consumption" 当敏感词吞了。这是纯显示 bug。

**3. `_sys_note` 标了 "auto-invoked"，但实际从不自动注入**
这轮我在意的是：整个会话里**没有任何一次**上下文状态（restart 标志、context%、schedule_status）是自动出现的——全部是我主动调用 `_sys_note` 才拿到的。也就是说：
- 文档说"auto-invoked，你别调"；
- 实际是"你不调就没有"。

两者必居其一：要么让 harness 真的在每轮开头注入状态、并把 `_sys_note` 从我的可调列表里摘掉；要么把描述改成"每轮开始主动调用一次"。现在是互相矛盾的。

**4. 命名空间撞车：MCP `github` × REST `github`**
- REST API 注册表里有一个 `github`（生成 `github__create_issue` 这类工具，系统提示词举的正是这个例子）；
- MCP 服务器列表里**也有一个 `github`**（anyapi-mcp-server，生成 `github__list_api` / `github__query_api`）。

两个都在生产 `github__*` 前缀工具。现在因为 MCP 是按需加载所以还没撞死，但一旦 `mcp_tool_load` 把 MCP github 的 `create_issue` 等同名端点加载进来，会跟 REST 版 `github__create_issue` **硬冲突**。建议给 MCP 那个改名（如 `github-mcp`）。

**5. 数量对不上：配置说 20 个 MCP，实际列出 19 个**
`system_health` 报 "20 MCP servers"，但 `mcp_list` 只返回 19 个（serper/fetch/tavily/github/ddg/amap/iflow/file-search/playwright/context7/chrome-devtools/citecheck/pandoc/markitdown/word/excel/pdf/sqlite/registry）。要么少了 1 个没列出来，要么计数 off-by-one。可复现。

**6. `system_health` 的 watchdog 段有重复条目**
压缩输出里能看到 `local-embed` 和 `mcp` 各出现两次。疑似同一个 key 被重复上报（去重 bug）。这个我没复跑验证（输出太长），建议你那边核对。

**7. 裁剪通知契约没兑现**
我记录的 context 占用变化：`45,262 → 22,284 → 45,262`（16:36 → 16:56 → 17:16 心跳）。数字从 45k 掉到 22k 再弹回 45k，但**没有任何 `[INFO: N oldest turns have been removed from context]` 通知**。要么这不是裁剪（那 context% 数字就不可信），要么裁剪通知丢了。契约写了的通知应该真的发出来。

## 🟡 上一轮提过、仍未动的（设计层面，非 bug）

- `mcp_tool_load` 还是"下一轮才能用"（search→load→call 三趟往返）
- `attach_image` 在 vision disabled 时仍然在我工具列表里占位
- `wechat_check_messages` 仍是破坏性读取（无 peek）
- `turn_summarize` 仍只能标"当前轮"，而我不知道轮次何时结束 → 摘要永远空
- memdb/memfiles 两个语义状态工具未合并

---

**最想让你先处理的**：#1（会直接导致发消息失败）和 #3（prompt 自相矛盾，我每轮都要为"要不要调 _sys_note"做违背文档的决定）。要不要我把 #1 再复现一次抓完整 JSON 给你作证据？
↑ 169,925 tokens (in: 147,462, out: 22,463)