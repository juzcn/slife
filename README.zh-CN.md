# Slife

**终端 AI 智能体** — 基于函数调用循环的最小化框架。与 LLM 对话，它能调用工具、永久记忆每一轮对话、协调其他智能体。

```
你: "找出所有 TODO 注释并为每个创建 GitHub Issue"
  → LLM 调用 search_content("TODO")
  → LLM 为每条调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，链接见上文。"
```

一个 TUI 窗口包裹一个 LLM 工具循环：10 个类别共 52 个原生工具（含 1 个保留的 harness 工具 `_sys_note`）、六个内置插件服务、始终开启的混合搜索记忆、视觉图片附件（`@path`/`@url`）、三种 API 后端运行时切换模型、智能体间（A2A）网格——一切都以统一的 OpenAI 风格函数定义呈现给 LLM。

需要 Python 3.13+。支持 Windows（原生 & WSL）、macOS 和 Linux。

## 安装

**零前提。** 安装脚本会自动安装 uv、Node.js 和 bun（如需要）。Mosquitto（仅 A2A MQTT 网格需要）会在安装过程中交互式询问。

### macOS / Linux / WSL

```bash
# 海外
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
# 国内
curl -fsSL https://gitee.com/juzcn/slife/raw/main/install.sh | bash
```

### Windows PowerShell

```powershell
# 海外
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
# 国内
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/install.ps1 | iex"
```

### 免安装试用

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

### 更新

重新运行安装脚本即可——通过对比旧环境的包列表，自动保留之前安装的可选包（llama-cpp-python、sentence-transformers）。

### 卸载

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/uninstall.sh | bash
curl -fsSL https://gitee.com/juzcn/slife/raw/main/uninstall.sh | bash   # 国内

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/uninstall.ps1 | iex"   # 国内
```

用户数据（`~/.slife/`、`~/.credstore/`）**不会删除**——如需彻底清除请手动删除。

### 相关工具

本仓库还发布两个独立的 PyPI 包，各自可独立安装（互不依赖）：

| 包 | 一键安装 | 用途 |
|---------|-------------------|---------|
| `slife` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh \| bash` | 智能体（本 README） |
| `credstore` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh \| bash` | 跨平台凭据存储 |
| `cc-switch` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh \| bash` | 生成 `~/.claude/settings.json` |

安装 slife 仅依赖 [credstore](credstore/README.md)——**不会**安装 cc-switch。详见 [cc-switch](cc-switch/README.md) 与 [credstore](credstore/README.md) 各自的 README。

三个包也都支持直接 `uv tool install <name>`（从 PyPI 安装）。每个包在其目录下都有独立的一键安装脚本（macOS/Linux/WSL 用 `install.sh`，Windows 用 `install.ps1`）与卸载脚本。

## 快速开始

```bash
credstore set-password              # 首次使用——加密备份
credstore set DEEPSEEK_API_KEY      # 存储 API Key（屏蔽输入）
slife
```

跨供应商共享密钥：

```bash
credstore copy DEEPSEEK_API_KEY BAILIAN_API_KEY
```

## 配置

密钥存 OS 密钥链，配置存 JSON5：

| 层 | 存储位置 | 内容 |
|---|---------|------|
| **密钥** | OS 密钥链 (credstore) | API Key — OS 级加密，另有加密 cryptfile 备份 |
| **配置** | `~/.slife/slife.json5` | `${VAR}` 引用 + 非敏感值 |

```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → 运行时从密钥链解析
}

models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      api: "openai-completions",
      models: [{ model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true }],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

支持 `${VAR:-default}` 回退语法。密钥也可用 `keyring:service/key` URI 形式引用。

**sLife 不支持 credstore 的 cryptfile 模式，但与环境变量设置完全兼容。** sLife 的凭据解析无需密码、绝不弹窗——它通过 credstore 读 OS 密钥链，再回退到 `os.environ`。当 credstore 进入 cryptfile-only（无 OS 密钥链，例如 HPC 登录节点上内核 keyring 被 seccomp/策略屏蔽）时，sLife 不读加密备份；有三种用法：

1. **仅环境变量（独立于 credstore）：** 在 shell 中导出密钥即可
   ```bash
   export DEEPSEEK_API_KEY="sk-…"
   ```
   sLife 解析 `${VAR}` 时先查 `os.environ`（在 credstore 之前），导出的密钥正常工作。
2. **继续用 credstore cryptfile 模式管理，再注入到环境变量：** 照常存储（`credstore set-password`、`credstore set KEY`），然后注入到环境让 sLife 可见：
   ```bash
   credstore inject DEEPSEEK_API_KEY BAILIAN_API_KEY   # cryptfile-only 模式下会询问主密码
   # 重启 shell，或：eval "$(credstore inject DEEPSEEK_API_KEY)"
   ```
3. **明文写在配置文件里（容忍，但不建议）：** sLife 接受 `slife.json5` 中字面量 `api_key`。能用，但密钥明文落盘（`~/.slife/slife.json5`，chmod 0600）——优先用方法 1 或 2。

`credstore` 本身在 cryptfile-only 模式下功能完整（`set-password`、`set`、`get -p`、`inject`、`status`——见 [credstore/README.md](credstore/README.md)）。

**三种一等公民 API 后端：**

| `api` 字段 | 后端 | 供应商 |
|-----------|------|--------|
| `openai-completions` | OpenAI / DeepSeek / Ollama / MiniMax | Chat Completions |
| `anthropic-messages` | Claude / 百炼 (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

**每模型 `compat` 覆盖**（在模型条目中配置，或通过 `model_set` 配置）：

```json5
models: {
  providers: {
    bailian: {
      api: "anthropic-messages",
      models: [{
        model: "qwen3.8-max", name: "Qwen3.8 Max",
        reasoning: true,
        compat: { thinkingFormat: "openai" },  // anthropic 后端：模型总是思考，不发送 thinking 参数
      }],
    },
    scnet: {
      api: "openai-completions",
      models: [{
        model: "MiniMax-M3", name: "MiniMax M3",
        reasoning: true,
        compat: { thinking: "omit" },          // openai 后端：不发送 thinking 字段（网关对 enabled 形状报 400）
      }],
    },
  },
},
```

OpenAI 后端 `compat.thinking`：`"omit"` 不发送 thinking 字段（针对拒绝 `{"type": "enabled"}` 形状但原生会思考的网关），`"disabled"` 显式关闭，`"enabled"` 与默认行为一致。

运行时切换：`model_list` → `model_switch(ref="bailian/qwen3.8-max")`。

**密钥绝不会进入 LLM 上下文。** 用户输入、工具调用参数和每个工具结果在进入对话前都经过基于模式的脱敏——API Key 形态（`sk-*`、`ghp_*`、Bearer 令牌等）自动替换为 `<MASKED>`。

## 功能

### 工具

统一为 OpenAI 函数定义。LLM 看不出原生、插件与外部 MCP 工具的区别。

**10 个类别共 52 个原生工具** — 从 `slife/tools/` 自动发现（51 个 LLM 可见 + 1 个 harness `_sys_note`；`attach_image` 在活动模型不支持视觉时会被剔除，`install_python_package` 在随附配置中默认禁用）：

| 类别 | 工具 |
|------|------|
| System | `system_health`, `check_memdb`, `check_wechat`, `check_memfiles`, `check_sharefile`, `check_mcp`, `check_a2a`, `check_watchdog` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package`, `run_schedule_now` |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image`（把本地图片或 URL 注入对话）, `_sys_note`（上下文状态，loop 代调） |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| Meta | `list_native_tools`, `check_async`, `cancel_async`, `clear_context`, `set_max_iterations`, `notify_user` |

A2A 网格工具（`a2a_*`，共 8 个）和全部插件工具由插件承载，不属于原生工具集——见下文。

每个工具还额外接受三个框架元参数：`_timeout`（单次调用超时覆盖）、`_async`（后台执行，用 `check_async` 轮询）和 `_approve`（对话流内联审批行——Y 批准 / N 拒绝 / Esc 拒绝）。

**五个托管类别**（Skills / CLI / REST API / Models / MCP）支持 `X_list` / `X_set` / `X_remove`（+ 需要开关时 `X_set_enabled`）——所有 `X_set` 工具都是幂等 upsert。`model_set` 的 upsert 会**合并**进现有条目（局部更新会保留 `reasoning` / `input` / `compat` 等字段），并接受 `compat` dict 用于每模型的供应商覆盖。

**插件工具** — 运行时以 `{server}__{tool}` 代理注册：

| 服务器 | LLM 可见工具 |
|--------|-------------|
| `mcp` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools` |
| `memdb` | `turn_list`, `turn_search`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage`, `semantic_index_status`, `semantic_index_config`, `semantic_search_enable` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_send_typing`, `wechat_check_messages`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_save`, `diary_write`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `list_files`, `cabinet_search`, `cabinet_read`, `cabinet_embedding_check` |
| `sharefile` | `share_file` |
| `a2a` | `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |

内置插件工具若已自带服务器名前缀（`mcp_set`、`wechat_login`）则原样注册，其余按 `{server}__{tool}` 命名。外部 MCP 服务器（`slife.json5` → `mcp.servers`）一律以 `{server}__{tool}` 出现（如 `filesystem__read_file`）。

**Windows 下的命令执行。** `execute_shell` 在检测到的 shell 中运行——PowerShell 或 cmd（与系统提示报告的值一致，保证 LLM 写的语法真的能执行）——并用系统代码页解码输出（简体中文 Windows 为 GBK/cp936）。`run_python_script` 强制子 Python 以 UTF-8 运行（`-X utf8`），非 ASCII 输出不会导致子进程崩溃。

### 记忆 — 始终开启

每轮对话永久记录在 SQLite（`~/.slife/<agent>.db`）。四种搜索模式：

| 模式 | 适用场景 |
|------|---------|
| `grep` | 精确字符串 — 错误信息、文件路径、代码 |
| `fts5` | 主题/关键词搜索，带排序摘要 |
| `hybrid` | 语义召回（FTS5 + 向量 → RRF 融合） |
| `time` | 按日期浏览 |

嵌入后端：本地 GGUF（BGE-M3，离线）、HuggingFace transformers 或 OpenAI 兼容 API。无嵌入后端时关键词搜索照常工作。语义（hybrid）结果只在**当前模型的索引完整构建后**才返回——全量重建期间（新/换模型、重启中断续跑）hybrid 退回关键词搜索，索引完成后自动恢复。

每轮对话还记录两个时间戳——用户输入时间（`created_at`，输入框回车时刻）和 assistant 完成时间（`completed_at`）——在聊天中以灰色 `[HH:MM]` 标记显示（分别位于用户消息和 assistant 回复上）。用户消息会带一条紧凑的 **`[Turn: N · 开始 → 结束]`** 脚注（记忆 rowid 加该轮发生的时间），拼接到消息文本末尾——LLM 能区分新旧轮次、用 rowid 引用（`turn_read` / `turn_summarize`），用户在 TUI 里也能读到同一行。

### 自主心跳

空闲时，agent 按 `agent.heartbeat_interval` 秒（默认 60）获得一次自主思考/行动的窗口。它作为一个正常 turn 运行（独立会话，存入记忆）；回复契约：有值得说的话就输出内容，否则只输出一个 `.`。单独的 `.` 回复统一表示**沉默**——无论来自心跳、A2A 异步完成通知还是任何事件，都不会在聊天或会话恢复中显示；`[Heartbeat]` 触发消息被过滤，真正的自主回复显示为 `⚡ 自主`。这是涌现自发性行为的前提。

### 定时任务

让 agent 按计划做事——"每晚 12 点写日记"、"每周五总结本周"——它会注册一个 cron 定时任务（`scheduled_task_set`）。任务触发时，agent 把工作委派给一个 subagent worker 而非亲自执行，worker 完成后把结果作为**报告**存入文件柜（`save_cron_report`）。每次触发都有记录（`scheduled_run_list`），你可以查看跑了什么、产出了什么（`report_list` / `report_read`）。

定时任务**只在 Slife 运行时触发**。Slife 关闭期间到点的执行会被记为**错过（missed）**，已触发但未完成（中断、报错、重启）的会记为**未完成（failed）**，并在下次启动时一起呈现，agent 可以提议补做（`run_schedule_now`），你也可以跳过（`scheduled_run_skip`）。

### 图片与视觉

用 `@path` / `@url` 语法附加图片（带空格的路径可加引号），喂给支持视觉的模型：

```
看看这张截图 @D:\Downloads\error.png
```

支持视觉的模型以 base64 data URI 接收本地文件，HTTP(S) URL 直接透传；`attach_image` 工具允许智能体在对话中途附加图片。所有文件均不在终端内渲染——用系统默认程序打开，`share_file` 则通过 ngrok 隧道把任意本地文件发布为公开 HTTPS 链接（隧道离线时返回优雅错误）。

### 插件

六个内置插件，独立进程运行：

| 插件 | 角色 |
|------|------|
| **slife-mcp** | 外部 MCP 服务器网关（stdio / SSE / Streamable HTTP） |
| **slife-memdb** | 日记数据库 + 混合搜索 |
| **slife-wechat** | 双向微信消息 |
| **slife-memfiles** | 笔记 / 日记 / 文件柜 + 公开分享（Streamable HTTP 插件，`/share` 路由在同一端口；ngrok 隧道由插件自持）。笔记与日记双写为 markdown + SQLite 混合索引 |
| **slife-a2a** | A2A 网格通道（MQTT binding；仅在 broker 可达时启动） |
| **slife-media** | 非聊天类 AI 生成（图片 / 视频 / TTS / ASR），对接任意提供商——自持 `media:` 配置段与提供商无关的适配层（`dashscope-aigc`、`openai-images`）。工具：`generate_image`、`generate_video`、`text_to_speech`、`transcribe_audio` |

外部 MCP 服务器在 `slife.json5` → `mcp.servers` 中配置——任何 stdio、SSE 或 Streamable HTTP MCP 服务器均可接入，无需 Slife SDK。带 `url` 的服务器自动探测 SSE，探测失败回退到 Streamable HTTP；Streamable 响应可能是单个 JSON body 或 SSE 流（两者都支持）。

所有插件均运行 **看门狗（watchdog）** 进程，崩溃时自动重启（指数退避 1s→30s，最多 5 次）。MCP 网关的看门狗重启后还会重新连接所有外部服务器。运行时健康检查——`check_memdb`、`check_wechat`、`check_memfiles`、`check_sharefile`、`check_mcp`、`check_a2a`、`check_watchdog`——监控应用级状态并经 `system_health` 汇总；看门狗纯属进程级。

### A2A — 智能体间通信（网格）

A2A 协议（JSON-RPC 操作与 Message/Task/AgentCard 数据形状，镜像官方 a2a-python 参考接口）运行在可插拔的传输 **binding** 上——当前为 MQTT。**`a2a` 插件**承载 LLM 可见工具与 `A2AClient`，仅在 broker 可达时启动：
- **网格工具**（统一 `a2a_` 前缀）：`a2a_send_task`、`a2a_send_task_async`、`a2a_get_task_result`、`a2a_cancel_task`、`a2a_list_agents`、`a2a_list_tasks`、`a2a_agent_card`、`a2a_broadcast`。
- **本地 worker 不是 A2A**：`spawn_subagent`、`list_subagents`、`stop_subagent`、`subagent_send_task`、`subagent_send_task_async`、`subagent_get_task_result`、`subagent_list_tasks`、`subagent_cancel_task`。一个 worker 一次处理一个任务；对忙碌 worker 的同步发送会自动转异步入队（返回 task_id）并告知。

A2A 唯一已实现的传输 binding 是 MQTT——把 `transport` 设为任何其他值会禁用 A2A 并打印警告，而不是导致启动崩溃。所有消息——人类输入、微信、MQTT、子智能体结果——通过单一收件箱队列逐个处理。

## 键盘快捷键

| 按键 | 动作 |
|------|------|
| `Ctrl+C` | 退出 |
| `Esc` | 取消 Agent Loop |
| `Ctrl+S` | 切换模型（内联选择器——输数字选，Esc 取消） |
| `Home` / `End` | 滚动到顶部 / 底部 |
| `Ctrl+Y` | 复制结果（工具调用上） |
| `Enter` / `Space` | 展开/收起思考块（助手消息上） |
| `↑` / `↓` | 输入历史导航 |
| `Shift+Enter` | 输入框内换行 |

## CLI 参数

| 参数 | 说明 |
|------|------|
| `--agent <id>` | 智能体标识 — 独立日记数据库 + A2A 网格名称（默认：`slife`） |
| `<配置路径>` | 位置参数 — 使用指定的配置文件（其父目录成为数据目录） |

## 可选扩展

| 扩展 | 启用功能 |
|------|---------|
| `slife[gguf]` | 本地 GGUF 嵌入（llama-cpp-python，离线，~300 MB） |
| `slife[transformer]` | HuggingFace 变换器嵌入（sentence-transformers，~2 GB） |
| `slife[embeddings]` | 以上两者 |

**Linux / macOS** — 从源码编译：

```bash
uv tool install "slife[gguf]" --reinstall
```

**Windows** — 预编译 wheel（无需 C++ 编译器）；uv 已配置使用 llama-cpp-python 的 CPU wheel 索引。详见[安装文档](https://github.com/juzcn/slife#optional-extras)。

## 开发

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync --all-extras

uv run credstore set-password
uv run credstore set DEEPSEEK_API_KEY
uv run slife

# 测试
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

开发模式自动检测（从源码树运行时）：数据文件保留在项目目录中。生产安装（uv tool / pipx / pip）一律使用 `~/.slife/`——即使在 checkout 目录或 home 目录下启动也不会误判。CI 在 Ubuntu、macOS 和 Windows 上使用 Python 3.13 运行测试。

## 架构

详见 **[DESIGN.md](DESIGN.md)** — 设计原则、Agent Loop、工具系统、插件契约、MCP 网关、记忆数据库、A2A 网格、凭证安全模型和完整项目结构。

## 许可证

MIT
