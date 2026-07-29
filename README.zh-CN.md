# Slife

**终端 AI 智能体** — 基于函数调用循环的最小化框架。与 LLM 对话，它能调用工具、永久记忆、通过 MQTT 协调其他智能体。

```
┌─────────────────────────────────────────────────────────────┐
│  终端 UI (Textual)                                          │
│  ────────────────────────────────────────────────────────── │
│  Agent Loop — LLM + 工具 + 流式 + 记忆 + A2A + 收件箱      │
│  ┌───────────┬──────────┬───────────┬─────────────────────┐ │
│  │ MCP 代理  │ A2A 网格 │ 子智能体   │ 内置插件            │ │
│  │ (网关)    │ (MQTT)   │ (工作进程) │ 记忆 · MCP · 微信   │ │
│  └───────────┴──────────┴───────────┴─────────────────────┘ │
│  永久记忆 — 混合搜索 (grep + FTS5 + 语义)                   │
└─────────────────────────────────────────────────────────────┘
```

## 安装

**零前提。** 安装脚本会自动安装 uv 和 Node.js（如需要）。

### 安装脚本（推荐）

**macOS / Linux / WSL：**

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
```

**Windows PowerShell：**

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
```

### 免安装试用

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

### 更新

重新运行安装脚本即可——自动保留之前安装的可选包（llama-cpp-python、sentence-transformers）。

### 卸载

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/uninstall.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"
```

卸载只移除二进制文件。用户数据（`~/.slife/`、`~/.credstore/`）会列出但**不删除**——如需彻底清除请手动删除。

## 快速开始

```bash
credstore set-password              # 首次使用——设置加密备份
credstore set DEEPSEEK_API_KEY       # 存储 API Key（屏蔽输入）
slife
```

默认配置内置了预配置的 MCP 服务器：文件系统+Shell、代码搜索、网页抓取、搜索引擎。

## 工作原理

Slife 是一个**函数调用循环**：你输入 → LLM 决定调用哪些工具 → Slife 执行 → LLM 响应 → 循环。

```
你: "找出所有 TODO 注释并为每个创建 GitHub Issue"
  → LLM 调用 search_content("TODO")
  → LLM 为每条调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，链接见上文。"
```

每一轮对话都永久记录。重启后自动恢复最近的对话。

## 配置

双层模型——密钥存 OS 密钥链，配置存 JSON5：

| 层 | 存储位置 | 内容 |
|---|---------|------|
| **密钥** | OS 密钥链 (credstore) | API Key、Token — OS 级加密 |
| **配置** | `~/.slife/slife.json5` → `env:` | `${VAR}` 引用 + 非敏感值 |

```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → 运行时从密钥链解析
}

models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      models: [{ model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true }],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

**密钥绝不会进入 LLM 上下文。** 所有工具输出在到达模型前都会经过脱敏处理——API Key 模式自动替换为 `<MASKED>`。

## 功能

### 工具

统一为 OpenAI 函数定义。LLM 看不出原生工具、MCP 工具和 A2A 工具的区别。

| 类别 | 描述 |
|------|------|
| **原生** | 系统信息、Python 执行、环境/配置管理、技能加载、CLI 发现 |
| **MCP / REST** | 文件系统、Shell、代码搜索、网页抓取、任意 MCP 服务器（stdio 或 HTTP） |
| **技能** | 通过 `list_skills` / `use_skill` 按需加载的插件 |
| **CLI** | 自动发现的外部命令，跨重启持久化 |
| **A2A** | 智能体发现、任务路由、子智能体、广播（13 个工具） |

### 记忆 — 始终开启

每轮对话永久记录。四种搜索模式：

| 模式 | 适用场景 |
|------|---------|
| `grep` | 精确字符串 — 错误信息、文件路径、代码 |
| `fts5` | 主题/关键词搜索，带排序摘要 |
| `hybrid` | 语义召回（FTS5 + vec0 向量搜索，RRF 融合） |
| `time` | 按日期浏览 |

嵌入后端：本地 GGUF（BGE-M3，~300 MB，离线）、HuggingFace transformers 或 OpenAI 兼容 API。无嵌入后端时关键词搜索照常工作。

### 插件

三个内置插件作为独立进程运行在 Streamable HTTP 传输上：

| 插件 | 角色 |
|------|------|
| **slife-mcp** | 外部 MCP 服务器网关（stdio + HTTP） |
| **slife-memory** | 日记数据库 + 混合搜索 |
| **slife-wechat** | 通过 iLink ClawBot 收发微信 |

外部 MCP 服务器在 `slife.json5` 中配置，启动时自动连接。第三方 MCP 服务器无需 Slife SDK——任何 stdio 或 HTTP MCP 服务器均可接入。

### A2A — 智能体间通信

三种传输，统一接口：

| 传输 | 适用场景 |
|------|---------|
| **MQTT** | 通过 Mosquitto broker 连接远程智能体 |
| **HTTP Streamable** | 智能体直连 |
| **子智能体** | 本地子进程（始终可用） |

统一收件箱将人类、微信、MQTT 和子智能体消息序列化到单个队列中。

### 渐进式披露

并非所有工具都在每次请求中呈现。三类采用轻量摘要：

| 类别 | 浏览 | 加载 |
|------|------|------|
| 记忆 | `memory_search` | `memory_open` |
| 技能 | `list_skills` | `use_skill` |
| MCP | `mcp_list_tools` | `mcp_set_disclosure("eager")` |

## 键盘快捷键

| 按键 | 动作 |
|------|------|
| `Ctrl+C`（输入框内） | 退出 |
| `Ctrl+C`（其他位置） | 复制 |
| `Esc` | 取消 Agent Loop |
| `Ctrl+L` | 聚焦输入框 |
| `Home` / `End` | 滚动到顶部 / 底部 |

## CLI 参数

| 参数 | 说明 |
|------|------|
| `--agent <id>` | 智能体标识 — 记忆隔离键 + A2A 网格名称（默认：`slife`） |

## 可选扩展

| 扩展 | 启用功能 |
|------|---------|
| `slife[gguf]` | 本地 GGUF 嵌入（离线） |
| `slife[transformer]` | HuggingFace 变换器嵌入（~2 GB） |
| `slife[embeddings]` | 以上两者 |

**Linux / macOS** — 从源码编译（这些平台默认有 C 编译器）：

```bash
uv tool install "slife[gguf]" --reinstall
```

**Windows** — 默认没有 C++ 编译器，需使用预编译 wheel。根据你的环境选择：

| 环境 | Wheel | 说明 |
|------|-------|------|
| 无编译器，任意 GPU 或无 GPU | `v0.3.34-vulkan` | 最安全 — 有 GPU 用 Vulkan，否则回退 CPU |
| NVIDIA GPU + CUDA 12 | `v0.3.34-cu132` | CUDA 12.x |
| NVIDIA GPU + CUDA 11 | `v0.3.34-cu125` | CUDA 11.x |
| AMD GPU | `v0.3.34-hip-radeon` | ROCm |

```powershell
$py = (uv tool list --show-paths 2>$null | Select-String 'slife v' | Out-String) -replace '.*\((.*?)\).*', '$1\Scripts\python.exe'
uv pip install --python $py "llama-cpp-python @ https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
```

**首次使用** — 下载 GGUF 模型并启用：

```bash
curl -LO https://huggingface.co/ChristianAzinn/bge-m3-gguf/resolve/main/bge-m3-Q4_K_M.gguf
```

然后启动 slife 告诉它：`启用本地嵌入，模型文件 bge-m3-Q4_K_M.gguf`

## 开发

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync --all-extras

uv run credstore set-password        # 首次使用
uv run credstore set DEEPSEEK_API_KEY
uv run slife
```

开发模式自动检测：数据文件保留在项目目录中。生产安装使用 `~/.slife/`。

```bash
# 运行测试
uv run pytest

# 含覆盖率
uv run pytest --cov=slife --cov=credstore --cov-report=term-missing
```

## 架构

Slife 是一个**最小化框架的智能体**。框架只做 LLM 物理上无法做到的事：执行工具、维护对话状态、流式响应、持久化记忆。其余一切——推理、规划、工具选择、错误恢复——由 LLM 负责。

详见 **[DESIGN.md](DESIGN.md)**：Agent Loop、工具系统、插件契约、MCP 网关、记忆数据库、A2A 网格、凭证安全模型和项目结构。

## 许可证

MIT
