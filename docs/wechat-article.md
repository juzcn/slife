# 我把 AI Agent 拆成了两个包发布到 PyPI

> 一个聊天窗口 + 工具就能跑的终端 AI Agent，以及一个独立的 MCP 代理服务。

---

## TL;DR

```bash
pip install slife        # 终端 AI Agent
pip install slife-mcp    # 独立 MCP 代理服务
```

GitHub: https://github.com/juzcn/slife

---

## slife 是什么

slife 是一个**终端里的 AI Agent** — 基于函数调用循环（function-calling loop），兼容 OpenAI API，支持 DeepSeek V4 的 thinking 模式。

它做的事很简单：你打字，LLM 推理，调用工具执行，返回结果。循环直到 LLM 觉得任务完成了。

```
你: "帮我查一下今天北京的天气"
LLM: → 调用 web_search
工具: → 搜索结果
LLM: → "今天北京晴，25°C..."
```

**核心理念是 Minimal Harness** — 框架只做 LLM 做不到的三件事：执行工具、维护对话状态、流式输出。其余的推理、规划、工具选择、错误恢复全部交给 LLM。

### 内置能力

| 工具 | 说明 |
|------|------|
| `execute_shell` | 执行 shell 命令 |
| `get_shell_command` | 自动适配 Windows/Linux/macOS 的 shell 语法 |
| `list_skills` / `use_skill` | 按需加载 Skill 文档，保持上下文精简 |
| MCP 工具 | 任意 MCP 服务器的工具（filesystem、搜索、数据库等） |

### 配置即代码

```json5
{
  models: {
    providers: {
      deepseek: {
        api_key: "${DEEPSEEK_API_KEY}",
        models: [
          { model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true },
        ],
      },
    },
  },
  active_model: "deepseek/deepseek-v4-pro",
  tools: [
    { type: "shell", timeout: 30 },
    { type: "skill", skills_dir: "skills" },
  ],
  mcp: {
    wrapper: { url: "http://127.0.0.1:9876/mcp" },
    servers: {
      filesystem: {
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
      },
    },
  },
}
```

API Key 用 `${ENV_VAR}` 引用，绝不硬编码在配置文件里。

---

## slife-mcp：被"拆出来"的独立服务

slife-mcp 原本是 slife 内部的一个子模块，负责管理对外部 MCP 服务器的连接。但在开发过程中我发现——**它完全可以独立存在**。

### 它做什么

MCP（Model Context Protocol）是 Anthropic 提出的开放协议，让 LLM 可以通过标准化接口调用外部工具。但每个 MCP 服务器是一个独立进程，直接管理多个服务器很麻烦。

slife-mcp 就是一个 **MCP 代理**：它帮你管理所有外部 MCP 服务器的连接，并把自己的端点暴露出来。任何 MCP 客户端（slife、Claude Desktop、你自己的 Agent）连上来就能用所有工具。

```
任何 MCP 客户端 ←→ slife-mcp ←→ filesystem / serper / 数据库 / ...
```

### 独立安装运行

```bash
pip install slife-mcp
slife-mcp                    # 自动检测模式，TTY → HTTP
slife-mcp --port 8888        # 自定义端口
```

**智能模式检测**：通过 `sys.stdin.isatty()` 判断 — 如果是终端直接运行就走 HTTP 模式，如果被 slife 作为子进程 pipe 启动就走 stdio。不需要 `--transport` 参数。

### 管理工具

slife-mcp 暴露了完整的管理 API：

| 工具 | 功能 |
|------|------|
| `mcp_add_server` | 运行时添加 MCP 服务器 |
| `mcp_remove_server` | 断开并移除服务器 |
| `mcp_list_servers` | 列出所有服务器及其状态 |
| `mcp_list_tools` | 列出所有可用工具 |
| `mcp_call_tool` | 调用指定服务器的工具 |
| `mcp_reload` | 重连刷新工具列表 |

---

## 为什么要拆成两个包

### 1. 关注点分离

slife 是 Agent — 对话、推理、调用工具。slife-mcp 是基础设施 — 管理连接、代理请求。它们通过 MCP 协议通信，不应耦合在一起。

### 2. 独立使用场景

slife-mcp 不只服务于 slife。理论上任何支持 MCP 的客户端都能用它：

- **Claude Desktop** — 在 `claude_desktop_config.json` 里配置 slife-mcp 的 URL
- **你自己的 Agent** — 通过 MCP 协议调用
- **多个 slife 实例** — 共享同一组 MCP 服务器连接

### 3. 独立版本迭代

slife-mcp 的连接池、重连策略、服务器生命周期管理，这些和 slife 的 Agent 逻辑完全无关。独立发布意味着可以各自迭代。

### 4. 零依赖

slife-mcp 只依赖 `fastmcp` 和 `json5`，不依赖 slife 的任何代码。一个 10KB 的包，干净利落。

---

## 架构一览

```
┌──────────────────────────────┐
│  slife (Textual TUI)         │  pip install slife
│  对话 / 推理 / 工具调用       │
│  slife/agent/loop.py         │
├──────────────────────────────┤
│  MCP Client                  │
│  slife/mcp/client.py         │  stdio / HTTP 双模式
│  slife/mcp/tool_adapter.py   │  MCP → slife Tool 适配
└──────────────┬───────────────┘
               │ stdio or HTTP
┌──────────────┴───────────────┐
│  slife-mcp (FastMCP)         │  pip install slife-mcp
│  MCP 代理 / 连接池管理        │
│  slife_mcp/server.py         │
│  slife_mcp/connection.py     │
├──────────────────────────────┤
│  外部 MCP 服务器              │
│  filesystem / serper / ...   │
└──────────────────────────────┘
```

---

## 技术细节

### Agent Loop

单循环，无预设策略：

```
用户输入 → 流式调用 LLM
  → thinking chunk → 实时展示
  → text chunk → 实时展示
  → tool call → 执行工具 → 结果回传 → 继续循环
  → 无 tool call → 返回最终回复
```

- DeepSeek V4 的 reasoning/thinking 实时流式输出
- 所有工具（原生 + MCP + Skill）注册在同一个 `ToolRegistry`
- `AgentEventHandler` 协议解耦 Agent 和 UI

### MCP 客户端

- **HTTP 探测**（0.5s 超时）：先检查是否有独立运行的 slife-mcp
- **回退子进程**：没有则通过 `sys.executable -m slife_mcp.server` 启动
- **asyncio.Queue 桥接**：stdlib 管道 ↔ MCP ClientSession

### MCP 连接池

- 纯 asyncio JSON-RPC，不用 anyio，避免与 FastMCP 的 TaskGroup 冲突
- 每个外部 MCP 服务器的 stdin/stderr 独立管理
- 优雅关闭：stdin close → SIGTERM → SIGKILL 三级递进

### TUI

基于 Textual，Claude Code CLI 风格：

- **ToolCallWidget** — 单个 Static 组件渲染，可折叠，展开显示参数和结果
- **Content.from_text(markup=False)** — 所有用户数据安全渲染，避免 MarkupError
- **AssistantMessage** — thinking 块 dim italic 展示，正文清晰呈现

---

## 下一步

- [ ] Serper 搜索工具实现（type = "serper"）
- [ ] Memory 系统 — 跨会话记忆
- [ ] 更多 LLM Provider 支持
- [ ] slife-mcp Docker 镜像

---

*slife — Silicon-based life. MIT License.*

*GitHub: https://github.com/juzcn/slife*
