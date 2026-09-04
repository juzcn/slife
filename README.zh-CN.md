# Slife

> **术语。** 项目术语的权威定义（面向模型与面向开发者的术语一致）见
> **[Glossary.md](Glossary.md)**。本 README 直接使用这些术语，不再重复定义。

**终端 AI 智能体** — 基于函数调用循环的最小化框架。与 LLM 对话，它能调用工具、永久记忆每一轮对话、协调其他智能体。

```
你: "找出所有 TODO 注释并为每个创建 GitHub Issue"
  → LLM 调用 search_content("TODO")
  → LLM 为每条调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，链接见上文。"
```

一个 TUI 窗口包裹一个 LLM 工具循环：12 个类别共 65 个原生工具（含 1 个保留的 harness 工具 `_sys_note`）、六个内置插件服务外加独立的 `mcp-plugin` MCP 网关、始终开启的混合搜索记忆、视觉图片附件（`@path`/`@url`）、三种 API 后端运行时切换模型、智能体间（A2A）网格——一切都以统一的 OpenAI 风格函数定义呈现给 LLM。

需要 Python 3.13+。支持 Windows（原生 & WSL）、macOS 和 Linux。

**双语界面。** TUI 跟随系统语言——中文系统显示中文，其它一律英文。启动时通过 [`sys-lang`](https://pypi.org/project/sys-lang/) 检测（Windows `Get-Culture` / *nix locale）；界面内的系统消息、批准提示、模型选择器、工具调用标签、状态栏均按系统语言渲染。LLM 可见内容（系统提示词、工具 schema）始终为英文，日志亦然。

## 安装

**零前提、开箱即用。** 安装脚本**从源码（最新的 `main`）** 构建 slife（不用 PyPI，永远是当前最新代码），并自动安装 uv、Node.js、bun 与 Mosquitto（缺失时；WSL 上装 Linux 原生版本——Windows 可执行文件无法经 WSL interop 接收自定义环境变量）。随后把三份 **git 跟踪的配置文件**作为开箱即用默认值写到各自的模块目录（`~/.slife/slife.json5`、`~/.local-embed/local_embed.json5`、`~/.mcp-plugin/mcp-plugin.json5`）——首次用户即可拥有完整工具集（本地 embeddings、外部 MCP 服务器、yt-dlp、browser-harness、A2A 网格），无需手工配置任何东西。重跑安装器会升级 slife，并对每一份**已存在的配置逐个询问是否覆盖**。传 `--core`（或设 `SLIFE_CORE=1`）可做仅核心的轻量安装，跳过可选工具。

### 环境要求

安装脚本对每个运行时使用**标准安装路径**——不会探测或适配你的系统。若你的环境不满足某运行时的要求，安装器会报告不兼容并指引手动路线；绝不会静默安装旧版或替代版本。

| 运行时 | 安装位置 | 要求 |
|---|---|---|
| uv | `~/.local/bin` | 任意现代 Linux/macOS/Windows；Python 由 uv 管理（3.13） |
| Node.js (LTS v22) | `~/.local/bin`（官方 tarball） | Linux 上需 **glibc ≥ 2.28 / libstdc++ ≥ 3.4.29**。老发行版（如 CentOS 7）**无法运行**官方二进制——安装器会报告缺失的 `GLIBC_2.28` / `GLIBCXX_3.4.xx` 符号。 |
| bun | `~/.bun/bin` | 现代 Linux/macOS/Windows |
| Mosquitto | 包管理器（winget / apt / brew / dnf / pacman） | A2A MQTT 网格需要——**静默自动安装** |

默认装好：`yt-dlp`、`browser-harness`、Mosquitto（A2A 网格），以及三份配置（`slife.json5`、`local_embed.json5`、`mcp-plugin.json5`）并接通外部 MCP 服务器。

**语义记忆搜索由用户按需设置**（后端依赖所在环境——CPU / CUDA / Metal，模型下载约 2 GB——都不该阻塞安装）。安装后在一个终端里执行：先选一个后端，再下载模型：

```bash
# 后端（选其一）
uv pip install --python "$(uv tool dir)/slife/bin/python" sentence-transformers            # 最简单，全平台可用
uv pip install --python "$(uv tool dir)/slife/bin/python" llama-cpp-python==0.3.34   # CPU（Linux/WSL/macOS，源码编译，需 C 编译器 + CMake）
CMAKE_ARGS="-DGGML_CUDA=on" uv pip install --python "$(uv tool dir)/slife/bin/python" llama-cpp-python==0.3.34  # NVIDIA CUDA
CMAKE_ARGS="-DGGML_METAL=on" uv pip install --python "$(uv tool dir)/slife/bin/python" llama-cpp-python==0.3.34 # macOS Metal
# Windows 无默认 C 工具链 — 用上游预编译 CPU wheel（唯一 workaround）：
uv pip install --python "$(uv tool dir)/slife/bin/python" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.34  # Windows CPU

# 模型 — 默认离线（HF_HUB_OFFLINE=1，不自动下载）。先自己下载：
# `hf download BAAI/bge-m3`（~2 GB 到 HF 缓存，huggingface.co → hf-mirror.com 自动 fallback），
# 或放一个更轻的 ~100 MB GGUF 到默认路径 ~/.local-embed/models/bge-m3-q4_k_m.gguf（或设 BGE_M3_GGUF_PATH）。
# （不做按需下载 — 模型必须已预下载到缓存。）
```

若手工编辑了 `mcp-plugin.json5`，改动在下一次启动 wrapper 时生效——工具目录由持久连接在内存中重建，不存在离线重建命令。所有可选步骤都**fail-open**：出错只警告、继续，核心安装始终可用。

若你的 Linux 低于 glibc 2.28，Node 官方 tarball 无法运行。受支持的路线**不是**安装旧版 Node——而是安装为你的发行版构建的 Node（例如 HPC 集群中的 `module load nodejs`，或发行版自带包）。装好后再运行安装器——它会检测到已有的 `npx` 并跳过自己的 Node 安装。

当某个运行时不可用时，安装器会**警告并继续**——slife 本身仍会安装，只是依赖该运行时的功能不可用（如基于 npx 的 MCP 服务器 `file-search`、`serper`、`tavily-mcp`、`github`、`amap-maps`、`filesystem`）。

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

重新运行安装脚本即可——通过对比旧环境的包列表，自动保留之前安装的可选包（llama-cpp-python、sentence-transformers）。若配置已存在，安装器会**问一次**是否要**重置为随包默认值**（默认否，保留你的配置）；配置只在缺失时自动写入。

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

本仓库还发布四个独立的 PyPI 包，各自可独立安装：

| 包 | 安装 | 用途 |
|---------|-------------------|---------|
| `slife` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh \| bash` | 智能体（本 README） |
| `credstore` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh \| bash` | 跨平台凭据存储 |
| `cc-switch` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh \| bash` | 生成 `~/.claude/settings.json` |
| `mcp-plugin` | 随 slife 安装，或 `uv tool install mcp-plugin` | 外部 MCP 服务器网关 |

安装 slife 依赖 [credstore](credstore/README.md) 与
[mcp-plugin](mcp-plugin/README.md)——**不会**安装 cc-switch。详见
[cc-switch](cc-switch/README.md)、[credstore](credstore/README.md) 与
[mcp-plugin](mcp-plugin/README.md) 各自的 README。

`slife`、`credstore`、`cc-switch` 各自有独立的一键安装脚本
（macOS/Linux/WSL 用 `install.sh`，Windows 用 `install.ps1`）与卸载脚本，位于
各自的包目录；`mcp-plugin` 没有自带安装脚本——随 slife 安装，或从 PyPI
`uv tool install mcp-plugin`。

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
// job-coding 插件用的大模型 — 复用 models.providers 的 provider/model 引用。
// 应当与 active_model 配成**不同**（通常更小更快）的模型：job 的一次性嵌套调用
// 不能扰动主 agent 在 active model 上的 prompt-cache。缺省 → active model。
job_coding_model: "bailian_personal/qwen3.6-flash",
```

支持 `${VAR:-default}` 回退语法（解析顺序：shell 环境变量 → credstore → 字面量默认值）。密钥也可用 `keyring:service/key` URI 形式引用。

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

**12 个类别共 65 个原生工具** — 从 `slife/tools/` 自动发现（64 个 LLM 可见 + 1 个 harness `_sys_note`；`attach_image` 在活动模型不支持视觉时会被剔除，`install_python_package` 在随附配置中默认禁用）：

| 类别 | 工具 |
|------|------|
| System | `system_health`, `check_memdb`, `check_wechat`, `check_memfiles`, `check_local_embed`, `check_sharefile`, `check_media`, `check_job_coding`, `check_mcp`, `check_a2a`, `check_watchdog`, `list_native_tools`, `check_async`, `cancel_async`, `clear_context`, `set_max_iterations`, `notify_user` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` |
| Schedule | `scheduled_task_set`, `scheduled_task_remove`, `scheduled_task_list`, `scheduled_run_list`, `scheduled_run_skip`, `run_schedule_now` |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image`（把本地图片或 URL 注入对话）, `_sys_note`（上下文状态，loop 代调） |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| embeddings | `embeddings_model_list`, `embeddings_enable`, `embeddings_model_set`, `embeddings_model_remove`, `embeddings_model_switch` |
| mcp | `mcp_tool_load` |

A2A 网格工具（`a2a_*`，共 8 个）和全部插件工具由插件承载，不属于原生工具集——见下文。

每个工具还额外接受三个工具元参数：`_timeout`（单次调用超时覆盖）、`_async`（后台执行，用 `check_async` 轮询）和 `_approve`（对话流内联审批行——Y 批准 / N 拒绝 / Esc 拒绝）。

**五个托管类别**（Skills / CLI / REST API / Models / MCP）支持 `X_list` / `X_set` / `X_remove`（+ 需要开关时 `X_set_enabled`）——所有 `X_set` 工具都是幂等 upsert。`model_set` 的 upsert 会**合并**进现有条目（局部更新会保留 `reasoning` / `input` / `compat` 等字段），并接受 `compat` dict 用于每模型的供应商覆盖。

**插件工具** — 运行时以 `{server}__{tool}` 代理注册：

| 服务器 | LLM 可见工具 |
|--------|-------------|
| `mcp` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools` |
| `memdb` | `turn_list`, `turn_search`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_save`, `diary_write`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `list_files`, `cabinet_search`, `cabinet_read` |
| `sharefile` | `share_file` |
| `a2a` | `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| `job_coding` | `job-list`, `job-create`, `job-edit`, `job-remove`, `job-run` + 每个已注册 job 一个工具（如 `translate`） |

内置插件工具若已自带服务器名前缀（`mcp_set`、`wechat_login`）则原样注册，其余按 `{server}__{tool}` 命名。外部 MCP 服务器（`slife.json5` → `mcp.servers`）一律以 `{server}__{tool}` 出现（如 `filesystem__read_file`）。

**Windows 下的命令执行。** `execute_shell` 在检测到的 shell 中运行——PowerShell 或 cmd（与系统提示报告的值一致，保证 LLM 写的语法真的能执行）——并用系统代码页解码输出（简体中文 Windows 为 GBK/cp936）。`run_python_script` 强制子 Python 以 UTF-8 运行（`-X utf8`），非 ASCII 输出不会导致子进程崩溃。

### 记忆 — 始终开启

每轮对话永久记录在 SQLite（`~/.slife/<agent>.db`）。四种搜索模式：

**记忆是核心功能——agent 绝不在记忆失效时静默运行。** 若记忆数据库损坏（缺列、损坏或磁盘错误），agent 会响亮地失败而非假装正常：无法恢复的会话在启动时报错中止；无法保存的轮次会冻结输入流并显示红色横幅——新轮次停止处理，直到数据库修复并重启 agent。

| 模式 | 适用场景 |
|------|---------|
| `grep` | 精确字符串 — 错误信息、文件路径、代码 |
| `fts5` | 主题/关键词搜索，带排序摘要 |
| `hybrid` | 语义召回（FTS5 + 向量 → RRF 融合） |
| `time` | 按日期浏览 |

Embeddings 是 `slife.json5` 顶层**一级配置段**（`embeddings`，memdb + memfiles 共享），由 native tools 管理：`embeddings_model_list` / `embeddings_model_set` / `embeddings_model_switch` / `embeddings_model_remove` / `embeddings_enable`（分类 `embeddings`）。每个 provider 是一个 **OpenAI 兼容端点**（`base_url` + `api_key`），`active_model`（`"provider/model"` 或裸 `"provider"`）以配置为准；本地 GGUF/transformer 模型经 **local-embed** 插件在 `http://127.0.0.1:17347/v1` 提供，加载一次、memdb 与 memfiles 共享。无嵌入端点时关键词搜索照常工作。语义（hybrid）结果只在**当前模型的索引完整构建后**才返回——全量重建期间（新/换模型、重启中断续跑）hybrid 退回关键词搜索，索引完成后自动恢复。

每轮对话还记录两个时间戳——用户输入时间（`created_at`，输入框回车时刻）和 assistant 完成时间（`completed_at`）——在聊天中以灰色 `[HH:MM]` 标记显示（分别位于用户消息和 assistant 回复上）。用户消息会带一条紧凑的 **`[INFO: {"turn_id": N, "begin": …, "end": …}]`** 脚注（turn id 加该轮发生的时间），拼接到消息文本末尾——LLM 能区分新旧轮次、用 turn id 引用（`turn_read` / `turn_summarize`），用户在 TUI 里也能读到同一行。

每轮对话还会保留其**来源渠道（channel）**——`human`、`wechat`、subagent、心跳、A2A peer，或 `system`（Slife 自身）——因此会话恢复时每个气泡都带正确的来源前缀：`You>`、`Wechat>`、`Subagent(<name>)>`、`Heartbeat>`、`A2A(<agent>)`。A2A peer 的 agent 名随轮次一起保存并跨重启保留；`system` 轮次（如定时任务触发）会存储但不显示在聊天里。进入的微信消息还会以 **`[WECHAT]`** 前缀到达模型（该标记仅面向模型，TUI 显示时会剥离，因为气泡前缀已经显示 `Wechat>`）；对方的微信用户 id 可从 `wechat_check_status.last_contact.peer_wechat_id` 获取。

### 自主心跳

空闲时，agent 按 `agent.heartbeat_interval` 秒（代码默认 60，随附模板设为 600）获得一次自主思考/行动的窗口。它作为一个正常 turn 运行（独立会话，存入记忆）；回复契约：有值得说的话就输出内容，否则只输出一个 `.`。单独的 `.` 回复统一表示**沉默**——无论来自心跳、A2A 异步完成通知还是任何事件，都不会在聊天或会话恢复中显示；`[Heartbeat]` 触发消息被过滤，真正的自主回复显示为 `⚡ 自主`。这是涌现自发性行为的前提。

### 定时任务

让 agent 按计划做事——"每晚 12 点写日记"、"每周五总结本周"——它会注册一个 cron 定时任务（`scheduled_task_set`）。任务名同时也是执行它的 worker 名，所以用简短 ASCII 标识符。任务触发时，agent 把工作派发给一个以任务名命名的 subagent worker（`run_schedule_now`）而非亲自执行，worker 完成后把结果作为**报告**存入文件柜（`report_save`）并通知你。每次触发都有记录（`scheduled_run_list`），你可以查看跑了什么、产出了什么（`report_list` / `report_read`）。

定时任务**只在 Slife 运行时触发**。下次启动时，一次性扫描会结算上一会话留在 `scheduled_run_list` 里的记录：没跑完的记为**未完成（failed）**，Slife 关闭期间到点没做的记为**错过（missed）**。启动不做任何提示、不打扰你——需要的话仍可用 `run_schedule_now` 补做（立即触发），或用 `scheduled_run_skip` 关闭。启动后下一次到点会正常触发。

### Jobs — 确定性、代码定义

对于**定义明确、可重复**的工作——翻译、摘要、抽取、分类、格式化——用 **Job** 跑一个代码定义的函数、只传它声明的参数，而不是把整个会话拖进一个 agent turn。Job 就是 `~/.slife/jobs/` 下的普通 `.py` 文件（一个公开函数 = 一个 job 工具；写法和内置的 `translate` / `summarize` 样例见 `job-coding` skill）。插件每次启动重载该目录，并支持实时管理：

- `job-list` — 查看已注册的 job
- `job-create` / `job-edit` / `job-remove` — 新增、修改（编辑出错自动回滚）、或删除 job；工具立即出现/消失，重启后依然生效
- `job-run` — 按名执行任意 job，或直接调用该 job 自己的工具

需要大模型的 job 通过注入的 `llm` 句柄**一次性**调用——`llm.chat` 用 `job_coding_model`（独立于会话 active model 配置的模型，job 调用便宜且不扰动主 agent 的 prompt-cache）。任何对话历史、系统提示词、agent loop 都到不了 job。

### 图片与视觉

用 `@path` / `@url` 语法附加图片（带空格的路径可加引号），喂给支持视觉的模型：

```
看看这张截图 @D:\Downloads\error.png
```

支持视觉的模型以 base64 data URI 接收本地文件，HTTP(S) URL 直接透传；`attach_image` 工具允许智能体在对话中途附加图片。所有文件均不在终端内渲染——用系统默认程序打开，`share_file` 则通过 ngrok 隧道把任意本地文件发布为公开 HTTPS 链接（隧道离线时返回优雅错误）。

### 插件

七个内置插件（外加独立的 `mcp-plugin` MCP 网关），独立进程运行：

| 插件 | 角色 |
|------|------|
| **slife-mcp** | 外部 MCP 服务器网关（stdio / SSE / Streamable HTTP）——独立包 `mcp-plugin`，经 `plugins.external` 注册 |
| **slife-memdb** | 对话记录数据库 + 混合搜索 |
| **slife-wechat** | 双向微信消息 |
| **slife-memfiles** | 笔记 / 日记 / 文件柜（私有）。所有保存工具返回本地路径——绝不自动发布。笔记与日记双写为 markdown + SQLite 混合索引 |
| **slife-sharefile** | 公开文件分享——唯一工具 `share_file` 把本地文件发布为公开 HTTPS URL（同端口的 `/share` 路由；ngrok 隧道由插件自持） |
| **slife-a2a** | A2A 网格通道（MQTT binding；仅在 broker 可达时启动） |
| **slife-media** | 非聊天类 AI 生成（图片 / 视频 / TTS / ASR），对接任意提供商——自持 `media:` 配置段与提供商无关的适配层（`dashscope-aigc`、`openai-images`）。工具：`generate_image`、`generate_video`、`text_to_speech`、`transcribe_audio` |
| **slife-job-coding** | 确定性 **Job** 系统（MCP 工具形态）——`~/.slife/jobs/` 里的代码函数按声明的参数精确执行；一次性 LLM 调用走 `llm.chat`、用 `job_coding_model`。工具：`job-list`、`job-create`、`job-edit`、`job-remove`、`job-run` + 每个 job 一个工具 |

外部 MCP 服务器在 `slife.json5` → `mcp.servers` 中配置——任何 stdio、SSE 或 Streamable HTTP MCP 服务器均可接入，无需 Slife SDK。带 `url` 的服务器自动探测 SSE，探测失败回退到 Streamable HTTP；Streamable 响应可能是单个 JSON body 或 SSE 流（两者都支持）。

所有插件均运行 **看门狗（watchdog）** 进程，崩溃时自动重启（指数退避 1s→30s，最多 5 次）。MCP 网关的看门狗重启后还会重新连接所有外部服务器。运行时健康检查——`check_memdb`、`check_wechat`、`check_memfiles`、`check_local_embed`、`check_sharefile`、`check_media`、`check_job_coding`、`check_mcp`、`check_a2a`、`check_watchdog`——监控应用级状态并经 `system_health` 汇总；看门狗纯属进程级。

就绪遵循 MCP 标准：插件在其 `initialize` 握手完成时即视为就绪——服务器只有在自己初始化（FastMCP lifespan）成功后才会应答握手，而插件会在初始化期间建立自身的服务能力（memdb 与 memfiles 要求 store 可用；其余插件无本地要求，能应答即就绪）。不再有 `__ready` 探测工具。外部/从属依赖——外部 MCP 服务器、ngrok 隧道、微信登录、媒体供应商、A2A broker、嵌入后端——从不阻塞就绪：它们不可控、运行时会自愈，并通过各自的状态工具单独上报。只有在每个插件进程都收敛（ready / skipped / failed——lifespan 无法满足要求会以启动失败上报并由看门狗重试）后，服务才对用户输入开放，因此输入绝不会跑在插件启动之前。

### A2A — 智能体间通信（网格）

A2A 协议（JSON-RPC 操作与 Message/Task/AgentCard 数据形状，镜像官方 a2a-python 参考接口）运行在可插拔的传输 **binding** 上——当前为 MQTT。**`a2a` 插件**承载 LLM 可见工具与 `A2AClient`，仅在 broker 可达时启动：
- **网格工具**（统一 `a2a_` 前缀）：`a2a_send_task`、`a2a_send_task_async`、`a2a_get_task_result`、`a2a_cancel_task`、`a2a_list_agents`、`a2a_list_tasks`、`a2a_agent_card`、`a2a_broadcast`。
- **本地 worker 不是 A2A**：`spawn_subagent`、`list_subagents`、`stop_subagent`、`subagent_send_task`、`subagent_send_task_async`、`subagent_get_task_result`、`subagent_list_tasks`、`subagent_cancel_task`。一个 worker 一次处理一个任务；对忙碌 worker 的同步发送会自动转异步入队（返回 task_id）并告知。

A2A 唯一已实现的传输 binding 是 MQTT——把 `transport` 设为任何其他值会禁用 A2A 并打印警告，而不是导致启动崩溃。所有消息——人类输入、微信、MQTT、子智能体结果——通过单一收件箱队列逐个处理。

## 键盘快捷键

按键名（`Ctrl+C`、`Esc` 等）通用不变；其后跟随的动作词随界面语言本地化。

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
| `--agent <id>` | 智能体标识 — 独立对话记录数据库 + A2A 网格名称（默认：`slife`） |
| `--lang <en\|zh>` | 界面语言 — 强制英文 / 中文（默认：按系统区域自动检测） |
| `<配置路径>` | 位置参数 — 使用指定的配置文件（其父目录成为数据目录） |

## 可选扩展

这些 embedding 后端**不由一键安装器默认安装**——它们是「语义记忆搜索」的可选设置（见[安装](#macos--linux--wsl)，任选一个后端+模型）。下表供手动安装（uvx / git 检出）或补装后端：

| 扩展 | 启用功能 |
|------|---------|
| `local-embed[gguf]` | 本地 GGUF 嵌入（llama-cpp-python，离线，~300 MB） |
| `local-embed[transformer]` | HuggingFace 变换器嵌入（sentence-transformers，~2 GB） |
| `slife[gguf]` / `slife[transformer]` / `slife[embeddings]` | 旧版进程内嵌入（默认不再使用） |

**Linux / macOS** — 从源码编译（附加包统一用 `uv pip install`）：

```bash
uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34    # slife[gguf]
uv pip install --python "$(uv tool dir)/slife" sentence-transformers        # slife[transformer]
```

**Windows** — 预编译 wheel（无需 C++ 编译器）；uv 已配置使用 llama-cpp-python 的 CPU wheel 索引。详见[安装文档](https://github.com/juzcn/slife#optional-extras)。

## 开发

权威术语见 [Glossary.md](Glossary.md)；设计与架构见 [DESIGN.md](DESIGN.md)。

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
