# Slife

**终端 AI 智能体** — 基于函数调用循环的最小化框架。与 LLM 对话，它能调用工具、永久记忆、协调其他智能体。

```
你: "找出所有 TODO 注释并为每个创建 GitHub Issue"
  → LLM 调用 search_content("TODO")
  → LLM 为每条调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，链接见上文。"
```

## 安装

**零前提。** 安装脚本会自动安装 uv、Node.js 和 bun（如需要）。

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

重新运行安装脚本即可——自动保留之前安装的可选包（llama-cpp-python、sentence-transformers）。

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

## 快速开始

```bash
credstore set-password              # 首次使用——加密备份
credstore set DEEPSEEK_API_KEY       # 存储 API Key（屏蔽输入）
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
| **密钥** | OS 密钥链 (credstore) | API Key — OS 级加密 |
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

**三种一等公民 API 后端：**

| `api` 字段 | 后端 | 供应商 |
|-----------|------|--------|
| `openai-completions` | OpenAI / DeepSeek / Ollama | Chat Completions |
| `anthropic-messages` | Claude / 百炼 (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

运行时切换：`list_models` → `switch_model(ref="bailian/qwen3.8-max")`。

**密钥绝不会进入 LLM 上下文。** 所有工具输出经过脱敏——API Key 模式自动替换为 `<MASKED>`。

## 功能

### 工具

统一为 OpenAI 函数定义。LLM 看不出原生工具和 MCP 工具的区别。

**12 个原生类别** — 从 `slife/tools/` 自动发现：

| 类别 | 工具 |
|------|------|
| System | `system_health`, `check_embedding`, `check_wechat` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `list_skills`, `use_skill`, `add_skill`, `remove_skill`, `skill_set` |
| CLI | `cli_list_tools`, `cli_add_tool`, `cli_remove_tool`, `cli_set_tool` |
| Models | `list_models`, `add_model`, `remove_model`, `switch_model` |
| A2A | 13 个工具 — 智能体发现、任务路由、子智能体生命周期、广播 |
| MemFiles | `save_content_or_files`, `expose_file`, `include_image` |
| Display | `show_image` |

**五类托管工具**（MCP / Skills / CLI / REST API / Native）支持 `list` / `add` / `remove` / `set`——所有 `add` 工具是幂等的 upsert。

加上内置 **MemDB** 工具：`memory_search`、`memory_open`、`memory_summarize`、`memory_count`、`memory_list_recent` 等。

### 记忆 — 始终开启

每轮对话永久记录。四种搜索模式：

| 模式 | 适用场景 |
|------|---------|
| `grep` | 精确字符串 — 错误信息、文件路径、代码 |
| `fts5` | 主题/关键词搜索，带排序摘要 |
| `hybrid` | 语义召回（FTS5 + 向量 → RRF 融合） |
| `time` | 按日期浏览 |

嵌入后端：本地 GGUF（BGE-M3，离线）、HuggingFace transformers 或 OpenAI 兼容 API。无嵌入后端时关键词搜索照常工作。

### 图片与视觉

用 `@path` 语法附加图片，终端内联显示：

```
看看这张截图 @D:\Downloads\error.png
```

两级渲染：**Sixel**（全彩，Windows Terminal / WezTerm / iTerm2 / Kitty）→ **HalfcellImage**（彩色 Unicode 半块字符，所有真彩终端）。支持视觉的模型通过 memfiles 隧道以 HTTPS URL 接收图片——上下文中无 base64。

### 插件

四个内置插件，独立进程运行：

| 插件 | 角色 |
|------|------|
| **slife-mcp** | 外部 MCP 服务器网关（stdio + HTTP） |
| **slife-memdb** | 日记数据库 + 混合搜索 |
| **slife-wechat** | 双向微信消息 |
| **slife-memfiles** | 文件服务器 + ngrok 隧道（视觉 API） |

外部 MCP 服务器在 `slife.json5` → `mcp.servers` 中配置。任何 stdio 或 HTTP MCP 服务器均可接入——无需 Slife SDK。

### A2A — 智能体间通信

三种传输，统一接口：**MQTT**（通过 Mosquitto broker 连接远程智能体）、**HTTP Streamable**（直连）、**子智能体**（本地子进程，始终可用）。所有消息通过单一收件箱队列。

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
| `--agent <id>` | 智能体标识 — 记忆隔离 + A2A 网格名称（默认：`slife`） |

## 可选扩展

| 扩展 | 启用功能 |
|------|---------|
| `llama-cpp-python` | 本地 GGUF 嵌入（离线，~300 MB） |
| `sentence-transformers` | HuggingFace 变换器嵌入（~2 GB） |

**Linux / macOS** — 从源码编译：

```bash
uv tool install "slife[gguf]" --reinstall
```

**Windows** — 预编译 wheel（无需 C++ 编译器）。详见[安装文档](https://github.com/juzcn/slife#optional-extras)。

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
uv run pytest --cov=slife --cov=credstore --cov-report=term-missing
```

开发模式自动检测：数据文件保留在项目目录中。生产安装使用 `~/.slife/`。

## 架构

详见 **[DESIGN.md](DESIGN.md)** — 设计哲学、Agent Loop、工具系统、插件契约、MCP 网关、记忆数据库、A2A 网格、凭证安全模型和完整项目结构。

## 许可证

MIT
