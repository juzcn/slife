# Slife

**终端 AI 智能体** — 与 LLM 对话，执行 Shell 命令、读写文件、搜索网页、调用 REST API、连接 MCP 服务器、派生子智能体并行工作、通过 MQTT 与其他 Slife 实例通信，并永久记忆一切。

```
┌────────────────────────────────────────────────────────────┐
│  终端 UI (Textual)                                         │
│  ─────────────────────────────────────────────────────────  │
│  Agent Service — LLM + 工具 + 循环 + MCP + A2A + 收件箱    │
│  ┌──────────┬─────────────┬──────────┬──────────────────┐  │
│  │ MCP 工具 │ A2A + MQTT  │ 子智能体  │ 内置插件          │  │
│  │  代理    │ 网格        │ 工作进程  │ ┌────┬────┬────┐ │  │
│  │          │             │          │ │MCP │记忆│微信│ │  │
│  └──────────┴─────────────┴──────────┴─┴────┴────┴────┘─┘  │
│  永久记忆 — 混合搜索 (grep + FTS5 + 语义)                    │
└────────────────────────────────────────────────────────────┘
```

## 安装

**零前提。** 安装脚本会自动安装 Python 3.13 和 uv（如需要），然后将 slife 安装到隔离环境中。无需 git、无需 Node.js、无需 C++ 编译器。

### 方式一：安装脚本（推荐）

**macOS / Linux / WSL：**

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
```

**Windows PowerShell：**

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
```

脚本会检查 Python 版本，按需安装 [uv](https://docs.astral.sh/uv/)，下载最新 slife 并安装到隔离环境中。如不放心，可先[查看脚本](install.sh)再执行。

### 方式二：uv tool install（需要 git）

```bash
uv tool install git+https://github.com/juzcn/slife.git
```

### 方式三：pipx（需要 git）

```bash
pipx install git+https://github.com/juzcn/slife.git
```

### 方式四：免安装试用

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

无需安装 — 在临时环境中下载、缓存并运行 slife。

安装后，`slife` 和 `credstore` 命令全局可用：

| 命令 | 位置 |
|------|------|
| `slife` | `~/.local/bin/slife` |
| `credstore` | `~/.local/bin/credstore` |
| 安装文件 | `~/.local/share/uv/tools/slife/` |
| 用户数据 | `~/.slife/`（首次运行时自动创建） |

### 卸载

```bash
uv tool uninstall slife
```

用户数据（配置、记忆数据库、微信会话、凭证备份）存放在 `~/.slife/`。开发环境下（当前目录存在 `slife.json5`），数据文件保留在项目目录中便于调试。如需删除：

```bash
rm -rf ~/.slife                            # 所有用户数据（生产环境）
credstore delete DEEPSEEK_API_KEY          # 删除已存密钥
credstore list                             # 列出所有已存凭证
```

### 可选扩展

Slife 默认安装保持精简。按需添加扩展：

| 扩展 | 包 | 作用 |
|------|-----|------|
| `embeddings` | `llama-cpp-python` | 本地 GGUF 嵌入模型，用于语义记忆搜索（离线、无 API 费用）。未安装时 FTS5 关键词搜索仍可正常使用。 |
| `mqtt` | `paho-mqtt` | A2A 智能体网格（`--agent <id>`）。未安装时子智能体仍可正常使用 —— 仅远程智能体发现需要 MQTT。 |

```bash
# 安装一个或两个扩展：
uv tool install "slife[embeddings]" --reinstall
uv tool install "slife[mqtt]" --reinstall
uv tool install "slife[embeddings,mqtt]" --reinstall
```

#### 配置本地嵌入模型

安装 `slife[embeddings]` 后，下载 GGUF 模型并配置：

```bash
# 1. 下载 GGUF 嵌入模型（BGE-M3，Q4_K_M 量化，约 300 MiB）
curl -LO https://huggingface.co/ChristianAzinn/bge-m3-gguf/resolve/main/bge-m3-Q4_K_M.gguf

# 2. 启动 slife，告诉智能体启用它：
slife
# > 启用本地嵌入，使用 bge-m3-Q4_K_M.gguf
```

智能体会调用 `memory_set_embedding` 写入配置并重载嵌入器 —— **无需重启**。验证：

```bash
slife
# > 检查嵌入状态
```

**Windows 用户**：`llama-cpp-python` 需要预编译 wheel（无需 C++ 编译器）。Vulkan 版本兼容所有 GPU，无 GPU 时自动回退 CPU：

```bash
uv tool install "slife[embeddings]" --reinstall
# 将平台 wheel 安装到工具的 venv 中：
uv tool run --from slife pip install "llama-cpp-python @ https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
```

其他 CUDA wheel：`v0.3.34-cu132`、`v0.3.34-cu125`；AMD：`v0.3.34-hip-radeon`。

#### 配置 MQTT 网格

安装 `slife[mqtt]` 后，启动 [Mosquitto](https://mosquitto.org/) broker 并以智能体身份运行：

```bash
# 终端 1 — 启动 broker（或使用已有实例）
mosquitto -p 1883

# 终端 2 — 以智能体身份启动 slife
slife --agent my-agent
```

如果 broker 地址不是默认值（`localhost:1883`），在 `~/.slife/slife.json5` 中配置：

```json5
mqtt: {
  broker: { host: "my-broker.local", port: 1883 },
}
```

## 快速开始

存储 API 密钥并启动：

```bash
credstore set-password                # 首次使用 — 设置加密备份
credstore set DEEPSEEK_API_KEY        # 密码输入，无回显
slife
```

默认配置（`slife.json5`）包含预配置的 MCP 服务器（文件系统、网页抓取、DuckDuckGo 搜索）—— 设置好模型密钥即可立即开始使用。

## 工作原理

Slife 是一个**函数调用循环**。你输入消息 → LLM 决定调用哪些工具 → Slife 执行并返回结果 → LLM 回应 → 循环往复。

```
你: "找出所有 TODO 注释并为它们创建 GitHub Issue"
  → LLM 调用 execute_shell("rg TODO")
  → LLM 逐一调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，全部链接见上方描述。"
```

## 配置

编辑 `slife.json5`。唯一必须设置的是**模型提供商 + API 密钥**：

```json5
models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      models: [
        { model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true },
      ],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

整个配置文件中支持 `${ENV_VAR}` 和 `${ENV_VAR:-default}` 语法 —— 值在启动时解析并注入到 `os.environ`。

### 凭证管理

Slife 内置 **credstore** — 基于 OS keyring 的跨平台凭证存储，附带 AES 加密文件备份。切勿将 API 密钥粘贴到 `slife.json5` 或聊天中。

#### 初始化（首次使用）

```bash
credstore set-password
```

创建 `./credentials.crypt` 并设置主密码。可通过 `CREDSTORE_FILE` 环境变量自定义路径。

#### 命令参考

| 命令 | 主密码 | 说明 |
|------|:------:|------|
| `set-password` | 设置 | 创建/修改主密码，初始化加密文件 |
| `set KEY` | 输入 | 原子双写（加密文件 + keyring） |
| `get KEY` | 否 | 仅 keyring，脱敏输出（`sk-5f…b722`） |
| `get KEY -p` | 输入 | 双重查询 keyring + 加密文件，明文输出 |
| `delete KEY` | 输入 | 从 keyring + 加密文件中删除 |
| `list` | 输入 | 列出所有已存凭证键名 |
| `reset-keyring` | 输入 | 从加密文件备份恢复 keyring |
| `reset-backup` | 输入 | 从系统 keyring 覆盖加密文件 |
| `status` | 否 | 显示后端状态 |

#### 存储 API 密钥

```bash
credstore set DEEPSEEK_API_KEY       # 密码输入 — 无回显、无 shell 历史
```

先写入加密文件，再写入系统 keyring。若 keyring 写入失败，回滚加密文件条目 — 两侧保持一致。

#### 灾难恢复

当系统 keyring 丢失数据时（如 Windows 密码变更后）：

```bash
credstore reset-keyring               # 从加密文件备份恢复所有凭证
```

智能体会自动注册引用 — 在对话中说"添加 DeepSeek 密钥"，它会调用 `config_secret_register`，将 `${DEEPSEEK_API_KEY}` 写入配置并提示你在终端运行 `credstore set DEEPSEEK_API_KEY`。

## 功能特性

### 工具系统

所有工具统一为 OpenAI function 定义 —— LLM 无法区分原生 Shell 命令、MCP 工具或 REST API 端点。

| 类别 | 示例 | 位置 |
|------|------|------|
| **原生** | `execute_shell`、`run_python_script`、`get_os_info` | `slife/tools/*.py` |
| **MCP / REST** | `filesystem__read_file`、`fetch__get`、`serper__search` | 通过 slife-mcp 代理 |
| **技能** | 按需插件，通过 `list_skills` / `use_skill` 使用 | `skills/` 目录 |
| **CLI** | 自动发现外部命令，通过 `cli_add_tool` 持久化 | 运行时注册 |
| **A2A** | 13 个协议工具 — 发现、路由、生命周期、广播 | `slife/tools/a2a.py` |

### 记忆系统

每次对话轮次都被永久记录。混合搜索（grep + FTS5 + 通过 vec0 的语义搜索）让 LLM 能够召回过去的工作。记忆系统作为内置插件运行（`slife/plugins/memory/`）—— 独立进程，崩溃不会与写入产生竞争。

```
memory_search("ConnectionError")            → 精确错误追踪
memory_search("MCP 配置", mode="fts5")      → 主题搜索
memory_search("那个 bug 修复", mode="hybrid") → 语义召回
memory_search(mode="time", since="2026-07") → 按日期浏览
```

通过 `--agent alice` 实现智能体隔离，每个智能体在数据目录拥有独立的数据库文件（`<agent_id>.db`）。嵌入模型支持本地 GGUF（离线）或 OpenAI 兼容 API。完整架构参见 [DESIGN.md § Permanent Memory](DESIGN.md#permanent-memory-slife-memory)。

### 插件系统

Slife 内置三个插件，均使用相同的 MCP stdio 协议：

| 插件 | 角色 |
|------|------|
| **slife-mcp** | 外部 MCP 服务器代理（stdio + HTTP）— 10 个管理工具 |
| **slife-memory** | 日记数据库 + 混合搜索（FTS5 + vec0 RRF） |
| **slife-wechat** | 通过 iLink ClawBot API 双向收发微信消息 |

第三方插件自动加载已在路线图中 —— 基础设施已就绪。

### A2A — 智能体间通信

两种传输方式，统一接口：**MQTT**（远程节点，通过 `--agent <id>` 启用）和**子智能体**（本地子进程，始终可用）。统一收件箱将人工键盘输入、微信、MQTT 和子智能体消息序列化到单个队列 —— 同一时间只有一个 AgentLoop 运行。

### 渐进式披露

并非所有工具都出现在每次 LLM 请求中。三类工具先使用轻量摘要：

| 类别 | 浏览 | 加载 |
|------|------|------|
| 记忆 | `memory_search` / `memory_list_recent` | `memory_open` |
| 技能 | `list_skills` | `use_skill` |
| MCP | `mcp_list_servers` / `mcp_list_tools` | `mcp_set_disclosure("eager")` |

## 快捷键

| 按键 | 操作 |
|------|------|
| `Ctrl+C` | 退出 |
| `Esc` | 取消 Agent 循环 |
| `Ctrl+L` | 聚焦输入框 |
| `Home` / `End` | 滚动到顶部 / 底部 |
| 任意键 | 自动聚焦输入框并输入 |

## CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--agent <id>` | `slife` | 智能体身份 — 记忆隔离键 & A2A 网格身份 |

## 环境要求

安装脚本自动处理一切。无需提前安装任何东西。

| 组件 | 状态 |
|------|------|
| Python ≥ 3.13 | 缺失时通过 uv 自动安装 |
| [uv](https://docs.astral.sh/uv/) | 缺失时自动安装 |
| Node.js | 可选 — 仅用于 npx MCP 服务器 |
| `llama-cpp-python` | 可选 — `slife[embeddings]` 提供本地 GGUF 嵌入 |
| `paho-mqtt` | 可选 — `slife[mqtt]` 提供 A2A MQTT 网格 |

## 开发

通过 `pyproject.toml` 检测开发模式 — 当 `[project] name == "slife"` 时，数据文件保留在项目目录中便于调试。生产环境使用 `~/.slife/`。

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync
uv run slife                      # 使用 ./slife.json5，数据文件在 ./
```

运行测试：

```bash
uv run pytest
```

## 设计哲学

Slife 是一个**最小化框架的智能体**。框架只做 LLM 物理上无法做到的事情：执行工具、维护对话状态、流式传输响应、持久化记忆。其他一切 —— 推理、规划、工具选择、错误恢复 —— 都是 LLM 的职责。

详见 [DESIGN.md](DESIGN.md) 了解完整架构、组件级文档和设计原理。

## 许可证

MIT
