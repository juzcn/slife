# Slife

> **工具家族，一句话。** Slife 向 LLM 呈现三个家族的工具，调用侧无差别：开发者的
> **系统工具**——内置（`slife/tools/` 自动发现）与内置插件工具（一等公民、裸名，
> 如 `turn_search`、`mcp_set`）；用户自己 coding 的 **job**（`job-<函数名>`）；以及第三方的
> **外部 MCP server** 工具（`{server}__{tool}`，按需加载）。

**终端 AI 智能体** — 基于函数调用循环的最小化框架。与 LLM 对话，它能调用工具、永久记忆每一轮对话、协调其他智能体。

```
你: "找出所有 TODO 注释并为每个创建 GitHub Issue"
  → LLM 用 execute_shell 在代码库里 grep
  → LLM 为每条结果调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，链接见上文。"
```

一个 TUI 窗口包裹一个 LLM 工具循环：**默认 59 个内置工具**、横跨 12 个类别（含保留的 harness 工具 `_turn_prompt` 与 `_check_new_input`——由循环自动调用），**九个内部插件服务**（memdb、wechat、memfiles、sharefile、a2a、media、job-coding、MCP 网关 `mcp-gateway`，以及 **`local-embed`** 嵌入服务）、始终开启的混合搜索记忆、视觉图片附件（`@path`/`@url`）、三种 API 后端运行时切换模型、智能体间（A2A）网格——一切都以统一的 OpenAI 风格函数定义呈现给 LLM。

需要 Python 3.13+。支持 Windows（原生 & WSL）、macOS 和 Linux。

**双语界面。** TUI 跟随系统语言——中文系统显示中文，其它一律英文。启动时直接读取系统（Windows `GetUserDefaultUILanguage` / *nix 环境变量）；界面内的系统消息、批准提示、模型选择器、工具调用标签、状态栏均按系统语言渲染。LLM 可见内容（系统提示词、工具 schema）始终为英文，日志亦然。

> **读者地图** — 挑一个适合你的入口：
>
> * **只想先试试** → [快速开始](#quick-start)
> * **要正式安装** → [安装](#install)
> * **要接模型 / API Key** → [配置](#configuration)
> * **想知道它到底能做什么** → [功能](#features)
> * **想让语义（混合）记忆搜索跑起来** → [语义记忆搜索 — 安装指南](#semantic-memory-search--installation-guide)
> * **日常使用**（快捷键、参数、健康检查）→ [使用方法参考](#usage-reference)
> * **要开发或调试 Slife 本身** → [开发](#development) — 开发者文档见 [DESIGN.md](DESIGN.md)

<a id="quick-start" name="quick-start"></a>

## 快速开始

```bash
credstore set-password              # 首次使用——加密备份
credstore set DEEPSEEK_API_KEY      # 存储 API Key（屏蔽输入）
slife
```

要在多个提供商之间共享同一个 API Key：

```bash
credstore copy DEEPSEEK_API_KEY BAILIAN_API_KEY
```

就这样。不做任何额外配置，你就得到核心循环：对话、工具调用、始终开启的记忆、子智能体、定时任务。其它一切——语义搜索、微信、媒体生成、外部 MCP 服务器、A2A 网格——都由下面的[配置](#configuration)与[功能](#features)按需开启。

<a id="install" name="install"></a>

## 安装

**零前提、开箱即用。** 安装脚本拉取最新的 `main`，**从源码**构建 slife（工作区 wheel——不用 PyPI，永远是当前最新代码），并用 uv 装进一个隔离的工具 venv。它会自动安装缺失的东西——uv、Node.js（`npx`）、bun、Mosquitto——然后把四份 **git 跟踪的配置**和随附的 **skills** 铺进各自的模块目录，并构建 MCP 工具目录，于是首次用户即可拥有完整工具集——本地 embeddings、外部 MCP 服务器、yt-dlp、browser-harness、A2A 网格——无需手工配置任何东西。在 WSL 上使用 Linux 原生运行时（Windows 可执行文件无法经 WSL interop 接收自定义环境变量）。

**语义 embedding 后端和模型刻意不属于安装的一部分**——后端依赖特定环境（CPU / CUDA / Metal），模型下载约 2 GB，所以这是由用户执行的步骤。按下面的 **[语义记忆搜索 — 安装指南](#semantic-memory-search--installation-guide)** 操作：安装一个后端、下载模型权重、配置 `HF_HUB_CACHE` / `BGE_M3_GGUF_PATH`，然后确认服务就绪。

### 环境要求

安装器是尽力而为的：它使用标准路径，对每个运行时尝试多种安装路线，遇到某个运行时不可用时**警告并继续**（只影响该运行时的功能），并且绝不静默替换为旧版或替代版本。传 `--core`（或设 `SLIFE_CORE=1`）可跳过可选 CLI 工具，做仅核心的轻量安装。

| 运行时 | 安装位置 / 方式 | 用途 |
|---|---|---|
| uv | 官方安装器 → `~/.local/bin`；Python 3.13 由 uv 管理 | 构建并运行 slife |
| Node.js (`npx`) | 包管理器（apt / brew / dnf / pacman / winget）→ 集群 `module load nodejs` → 官方 LTS tarball → `~/.local`（无 root 回退） | 基于 npx 的 MCP 服务器：`file-search`、`serper`、`tavily-mcp`、`github`、`amap-maps`、`filesystem` |
| bun | `~/.bun/bin` | `nvidia-nim` MCP 服务器 |
| Mosquitto | 包管理器（winget / apt / brew / dnf / pacman） | A2A MQTT 网格——尽力自动安装；broker 未运行时 A2A 保持禁用 |
| `cloudflared` | winget / Homebrew → 官方 release 二进制 → `~/.local/bin`（无 root、Linux） | `sharefile` 的 `cloudflare` 隧道 provider——尽力自动安装；没有它该 provider 不可用（可用 `SLIFE_SKIP_CLOUDFLARED=1` 跳过） |
| `ssh` | 随系统提供（Windows：可选的 *OpenSSH 客户端* 功能） | `sharefile` 的 `localhost.run` 隧道 provider——**仅检测**；启用 Windows 功能需要管理员权限 |
| `unzip`（Linux） | 包管理器 | bun 安装器依赖 |

**默认安装以下内容：**`yt-dlp` 与 `browser-harness`（两者都被 `--core` 跳过）、Mosquitto 与 `cloudflared`（总是尝试）、从随包默认值铺设的**四份配置**（`slife.yaml`、`tools.yaml` 和 `sharefile.yaml` → `~/.slife/`，`local_embed.yaml` → `~/.local-embed/`），以及随附 skills（`~/.slife/skills/`）和示例 jobs（`~/.slife/jobs/`）。

如果某个运行时装不上，安装器**警告并继续**——slife 本身仍会安装，只是需要该运行时的功能不可用。举例来说，在一台比 **glibc 2.28 / libstdc++ 3.4.29** 更老的 Linux 机器上，Node 的 no-root tarball 回退方案跑不起来（安装器会报告缺失的 `GLIBC_2.28` / `GLIBCXX_3.4.xx` 符号）。受支持的路线**不是**装旧版 Node——而是装一个为你的发行版构建的 Node（例如 HPC 集群上的 `module load nodejs`，或发行版自带的包）。装好后再重跑这个安装器——它会检测到已有的 `npx` 并跳过自己的 Node 安装。

如果你手工编辑 `tools.yaml`，改动会在下一次启动 wrapper 时生效——工具目录由连接实时重建，所以不存在离线重建步骤（安装后 `local-embed` 已在 PATH 上）。每个可选步骤都 **fail-open**：出错只会警告并继续，留下一个可用的核心。

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

重跑安装脚本即可升级 slife——它从最新的 `main` 重建，并保留你自定义过的东西：

- **可选包**（如 `sentence-transformers`、`llama-cpp-python`）会从旧的工具 venv 中捕获，在全新安装后重新加入，并与新的基础版本做 diff，避免任何重复。
- **已存在的配置、skills、示例 jobs** 一律保持原样，安装器**不再询问、绝不覆盖**。缺失的默认文件直接铺设；内容完全相同的静默跳过；当随包默认值发生变化时，新默认会被铺设到 `~/.slife/` 下作为**带版本号的参考副本**——配置与 jobs 形如 `<文件名>.<版本号>.<后缀>`（如 `slife.0.9.8.yaml`、`total_tokens.0.9.8.py`），skills 形如 `<名称>.<版本号>/`——每次写入都以 `seeded <文件> → <文件夹>` 的形式提示（如需应用，一条 `cp` / `Copy-Item` 即可）。重复安装会刷新同版本副本，旧版本副本保留作参考。判定 skill 是否不同，只看**随包默认自带的那些文件**是否有差异——只存在于用户副本里的文件（自己的笔记、`Thumbs.db`、Windows 工具读文件时留下的 `SKILL.md:Zone.Identifier`）不算默认值发生变化，不会触发副本。

### 卸载

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/uninstall.sh | bash
# 国内
curl -fsSL https://gitee.com/juzcn/slife/raw/main/uninstall.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"
# 国内
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/uninstall.ps1 | iex"
```

卸载器会移除 `slife` 与 `credstore` 两个工具命令（它们共享同一个 venv），以及单独安装的 `local-embed` 工具（如果存在）——还有它们在 `~/.local/bin` 的 wrapper。用户数据（`~/.slife/`、`~/.credstore/`、`~/.local-embed/`——配置与模型权重）**不会被删除**——如需彻底重置请手动删除。

### 相关工具

本仓库还附带三个独立的包——各自独立安装（MCP 网关作为内部插件**内置**在 slife 中；`local-embed` 同样是一个内部插件，但也**可以**作为独立服务运行——你可以自己先启动它，slife 会直接使用那个实例，而不再启动第二个）：

| 包 | 安装 | 用途 |
|---------|---------|---------|
| `slife` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh \| bash` | 智能体（本 README） |
| `credstore` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh \| bash` | 跨平台凭据存储 |
| `cc-switch` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh \| bash` | 生成 `~/.claude/settings.json` |
| `local-embed` | 随 slife 安装（内部插件；亦可独立运行） | 本地嵌入端点服务 |

安装 slife 依赖 [credstore](credstore/README.md)——它**不会**安装 cc-switch。详见 [cc-switch](cc-switch/README.md)、[credstore](credstore/README.md) 和 [local-embed](local-embed/README.md) 各自的 README。`slife`、`credstore`、`cc-switch` 各自带一键安装器（macOS / Linux / WSL 用 `install.sh`，Windows 用 `install.ps1`）和卸载器，都放在各自的包目录下；`local-embed` 作为 slife 依赖发布，提供 `local-embed` CLI。

<a id="configuration" name="configuration"></a>

## 配置

**密钥存凭据库，配置存 YAML：**

| 层 | 存储位置 | 内容 |
|-------|---------|----------|
| **密钥** | 凭据库（credstore） | API Key——OS 级加密，另有加密的 cryptfile 备份 |
| **配置** | `~/.slife/slife.yaml` | `${VAR}` 引用 + 非敏感值 |

### 密钥与 API Key

密钥绝不会出现在配置文件里。用 `credstore set <KEY>` 存储它们；配置中以 `${VAR}` 引用，Slife 在运行时解析——解析顺序为 **shell 环境变量 → credstore → 字面量默认值**（支持 `${VAR:-default}` 回退；密钥也可以用 `keyring:service/key` URI 引用）。

```yaml
env:
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}"   # → 运行时从 credstore 解析
```

**Slife 从不弹窗，也不读 credstore 的 cryptfile 备份。** 它只读系统 keyring，然后回退到 `os.environ`。如果没有可用的系统 keyring（例如 Linux 上 HPC 登录节点的内核 keyring 被 seccomp/策略屏蔽），可以用三种方法之一：

1. **只用环境变量**——在 shell 中导出密钥（`export DEEPSEEK_API_KEY="sk-…"`）；`os.environ` 在 credstore 之前被检查，所以导出的密钥正常工作。
2. **继续用 credstore 的 cryptfile 模式管理，但注入到环境变量**——照常存储凭据，然后把它们推入环境让 Slife 能看到：`credstore inject DEEPSEEK_API_KEY BAILIAN_API_KEY`（cryptfile-only 模式下会询问主密码），然后重启 shell 或 `eval "$(credstore inject DEEPSEEK_API_KEY)"`。
3. **明文写在配置文件里**（容忍，但不推荐）——`slife.yaml` 中的字面量 `api_key` 能工作，但密钥会明文落在磁盘上（`~/.slife/slife.yaml`，chmod 0600）。

`credstore` 本身在 cryptfile-only 模式下功能完整（`set-password`、`set`、`get -p`、`inject`、`status`——见 [credstore/README.md](credstore/README.md)）。

### 模型供应商

模型一次性在 `slife.yaml` 里配置好，之后在聊天中运行时切换（无需改文件）：

```yaml
models:
  providers:
    deepseek:
      base_url: "https://api.deepseek.com"
      api_key: "${DEEPSEEK_API_KEY}"
      api: "openai-completions"
      models:
        - model: "deepseek-flash"
          name: "DeepSeek Flash"
          reasoning: true
active_model: "deepseek/deepseek-flash"
job_coding_model: "bailian_personal/qwen3.6-flash"
```

`active_model` 是一个 `"provider/model"` 引用——Slife 聊天所用的模型。`job_coding_model` 是确定性 **jobs** 使用的 LLM——给它配一个与 `active_model` 不同的（通常更小/更快）模型，这样嵌套的一次性 job 调用永远不扰动 agent loop 的 prompt 缓存（缺省 → active model；每次调用可覆盖：`llm.chat(model=...)`）。

**三种一等公民 API 后端：**

| `api` 字段 | 后端 | 供应商 |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama / MiniMax | Chat Completions |
| `anthropic-messages` | Claude / 百炼 (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

**每模型 `compat` 覆盖**（在模型条目中配置，或通过 `model_set`），用于不遵循标准思考形状的网关：

```yaml
models:
  providers:
    bailian:
      api: "anthropic-messages"
      models:
        - model: "qwen3.8-max"
          name: "Qwen3.8 Max"
          reasoning: true
          compat:
            thinkingFormat: "openai"   # anthropic 后端：模型总是思考，不发送 thinking 参数
    scnet:
      api: "openai-completions"
      models:
        - model: "MiniMax-M3"
          name: "MiniMax M3"
          reasoning: true
          compat:
            thinking: "omit"           # openai 后端：不发送 thinking 字段（网关对 enabled 形状报 400）
```

OpenAI 后端上的 `compat.thinking`：`"omit"` 不发送 thinking 字段（给拒绝 `{"type": "enabled"}` 形状但原生思考的网关），`"disabled"` 显式关闭，`"enabled"` 与默认行为一致。

**运行时切换：** 用自然语言 `model_list` → `model_switch(ref="bailian/qwen3.8-max")`，或在活动模型不可用时用 `Ctrl+S` 内联选择器作为应急逃生门。`model_set` 是 upsert（**合并**——局部更新会保留模型的 `reasoning`/`input`/`compat` 字段），并接受 `compat` dict，所以每模型覆盖无需手工改文件。

**密钥绝不出现在 LLM 上下文里。** 用户输入、工具调用参数和每个工具结果在进入上下文前都经过基于模式的脱敏——API Key 形态（`sk-*`、`ghp_*`、Bearer 令牌等）自动打码。

<a id="features" name="features"></a>

## 功能

### 工具

全部统一为 OpenAI 函数定义——LLM 看不出系统工具（内置 + 内置插件）与外部 MCP 工具的区别。每个工具还额外接受三个元参数：`_timeout`（单次调用超时覆盖）、`_async`（后台执行，用 `check_async` 轮询）和 `_approve`（内联批准提示——Y 批准 / N 拒绝，Esc 拒绝）。

**12 个类别共 59 个内置工具**（从 `slife/tools/` 自动发现 60 个类；`install_python_package` 在随附配置中默认禁用）。保留的 harness 工具 `_turn_prompt`（每轮提示词）与 `_check_new_input`（插队模式下的轮中消息注入）由循环自动调用；`attach_image` 在 `@` 附件时自动调用——模型会读取它们的产出，但被嘱咐不要调用它们。`attach_image` 对无视觉模型会在调用时拒绝（它从不被隐藏）。

| 类别 | 工具 |
|----------|-------|
| System | `system_health`, `system_tools_list`, `check_async`, `cancel_async`, `clear_context`, `set_max_iterations`, `notify_user`, `wait_minutes`（暂停本轮，稍后自动继续）, `add_user_pref`（把偏好记录到 `USER.md`） |
| Execution | `execute_shell`, `run_python_script`, `install_python_package`（默认禁用） |
| Schedule | `scheduled_task_set`, `scheduled_task_remove`, `scheduled_task_list`, `scheduled_run_list`, `scheduled_run_skip`, `run_schedule_now` |
| Job | `job-list`、`job-write`、`job-remove`、`job-run` + 每个已注册 job 一个工具（`job-<name>`），由 `job-coding` 插件提供 |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image`（给视觉模型喂图片）, `_turn_prompt`（每轮提示词，自动调用）, `_check_new_input`（轮中消息注入，自动调用） |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| embeddings | `embeddings_model_list`, `embeddings_model_set`, `embeddings_model_switch`, `embeddings_model_remove`, `embeddings_enable` |
| mcp | `mcp_tool_load` |

**托管类别**（Skills / CLI / REST API / Models / MCP）支持 `X_list` / `X_set` / `X_remove`（+ 有开关时 `X_set_enabled`）——所有 `X_set` 工具都是幂等 upsert；`model_set` **合并**进现有条目，因此聚焦某一字段的改动不会悄悄剥掉模型的 `reasoning`/`input`/`compat`。`rest_api_set` 把 OpenAPI 描述的外部 API 注册为一个由 `mcp-openapi-proxy`（Low-Level Mode，默认模式）支撑的 server——spec 里的每个端点成为一个带类型的 `{name}__{endpoint}` 工具。

**插件工具** — 内置插件以裸名注册（`[<server>]` 描述前缀）；外部 MCP 服务器以 `{server}__{tool}` 出现：

| 服务器 | 工具 |
|--------|-------|
| `mcp-gateway` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools` |
| `memdb` | `turn_list`, `turn_search`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_save`, `diary_write`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `list_files`, `cabinet_search`, `cabinet_read`, `report_save`, `report_list`, `report_read` |
| `sharefile` | `share_file`, `sharefile_unshare` |
| `a2a` | `a2a_send_task`, `a2a_send_task_async`, `a2a_send_message`, `a2a_send_message_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast`, `a2a_set_task_done`（完成收到的任务并发布结果） |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| `job-coding` | `job-list`, `job-write`, `job-remove`, `job-run` + 每个已注册 job 一个工具（如 `job-translate`） |

**所有工具共用一个目录，由阈值管理。** 第三方能力只能作为 `tools.yaml` 里的标准 MCP 服务器接入（`mcp` + `rest-api` 两个 section——任何 stdio / SSE / Streamable HTTP 服务器都可以，无需 Slife SDK；REST API 就是放在 `rest-api` section 里的普通 MCP 服务器，条目格式完全相同）。所有类别——builtin、job、plugin、mcp、rest-api、skill、cli——共用同一个 `tools.db`；LLM 用 `tool_search` 跨全部类别检索（grep / 关键词 / 语义混合，按目录的列过滤），再用 `func-tool-load(full_name)` 载入具体工具——按工具而非按服务器，所以一个上千工具的大服务器只会注入真正用到的那几个。不是"存在就被注入"：新工具生来是 `unloaded`，只有 `func-tool-load` 能把它放进工具列表（例外是白名单——harness 对、系统元工具、固定注入的 `skill_use` / `system_health`——以及 `tools.yaml` 里标了 `autoload: true` 的条目；而 `autoload` 的条目会**一直**是 loaded：它是唯一能赢过模型自己 unload 的配置决定）。这份列表是上下文预算，不是许可：只要有执行实例，工具按名字就能调用（load 状态不拦截调用），载入的作用是把工具的 schema 放到模型面前。注入列表由阈值封顶（默认 100，可在 `tools.yaml` 调整），harness 在轮次边界淘汰最久未用的工具，从不淘汰 `autoload` 的。服务器生命周期每个家族一个开关（`mcp_set_enabled` / `rest_api_set_enabled`）——现代 MCP 协议没有要开关的 session，所以 enable 即连接、之后调用时若掉线会懒重连——并且**启动时所有 enabled 服务器都会被拉起**（只 spawn、不读工具列表；列表留给第一个真正需要它的调用方）；服务器连不上时它的工具被标记 `error`，因此死连接永远不会被注入。目录在每次（重）连接时实时同步——不存在离线重建步骤。完整设计见 **[TOOL-SYSTEM.md](docs/TOOL-SYSTEM.md)**。

**Windows 下的命令执行。** `execute_shell` 在检测到的 shell 中运行——PowerShell 或 cmd（与系统提示报告的值一致，保证 LLM 写的语法真的能执行）——并用系统代码页解码输出（中文 Windows 为 GBK/cp936）。`run_python_script` 强制子 Python 以 UTF-8 运行（`-X utf8`），这样非 ASCII 输出不会让子进程崩溃。

### 记忆 — 始终开启

每轮对话永久记录在 SQLite（`~/.slife/<agent>.db`），并有四种搜索方式：

**记忆是核心功能——agent 绝不在记忆失效时静默运行。** 若记忆数据库损坏（缺列、损坏或磁盘错误），agent 会响亮地失败而非假装正常：无法恢复的会话在启动时报错中止；无法保存的轮次会冻结收件箱并显示红色横幅——不再处理新轮次，直到数据库修复并重启 agent。

| 模式 | 适用场景 |
|------|----------|
| `grep` | 精确字符串 — 错误信息、文件路径、代码 |
| `fts5` | 主题/关键词搜索，带排序摘要 |
| `hybrid` | 语义召回（FTS5 + 向量 → RRF 融合） |
| `time` | 按日期浏览 |

Embeddings 是 `slife.yaml` 中**一级顶层的 `embeddings` 配置段**（由 `memdb` + `memfiles` 共享），由内置 `embeddings_*` 工具管理；运行时索引状态由 `system_health` 上报。每个 provider 都是 **OpenAI 兼容端点**（`base_url` + `api_key`）；`active_model`（"provider"——例如 `"local_embed"` 或 `"siliconflow"`）以配置为准。**`local-embed` 服务**（由 slife 作为内部插件启动为你启动——若你自己已经在跑一个，则用你启动的那个实例）在 `http://127.0.0.1:17347/v1` 提供本地 GGUF/transformer 模型，每个模型**加载一次**、由 `memdb` 与 `memfiles` 共享——不重复加载——它自己没有 "active model"（客户端请求它想要的模型，因此一个模型绝不会被加载两次）。**没有嵌入后端时关键词搜索照样工作。** 语义（hybrid）结果只在当前模型的索引完整构建后才返回——重建运行期间 hybrid 退回关键词搜索，索引进度完成时自动恢复。

每轮对话还记录两个时间戳——你的输入时间（`created_at`，敲下回车的那一刻）和 assistant 的完成时间（`completed_at`）——以灰色 `[HH:MM]` 标记显示。用户消息带一条紧凑的 **`[INFO: {"turn_id": N, "begin": …, "end": …}]`** 脚注（turn id 加发生时间），让 agent 能用 turn id 引用轮次（`turn_read` / `turn_summarize`）——你在 TUI 里也读到同一行。

每轮对话还会保留它的**来源渠道（channel）**——`human`、`wechat`、subagent、心跳、A2A peer，或 `system`（Slife 自身）——因此会话恢复时每个气泡都带正确的来源前缀：`You>`、`Wechat>`、`Subagent(<name>)>`、`Heartbeat>`、`A2A(<agent>)`。进入的微信消息还会以 **`[Wechat:{...}]`** 前缀到达模型——JSON 里带有 `wechat_send_message` 回复所需的 `peer_wechat_id` / `context_token`。

### 自主心跳

空闲时，agent 按 `agent.heartbeat_interval` 秒（代码默认 60，随附模板设为 1800）获得一个周期性的自主窗口。它作为一个正常 turn 运行（独立的一轮，存入记忆）；回复契约是：有值得说的话就输出真实内容，否则只输出一个 `.`。单独的 `.` 回复表示**沉默**——来自任何事件（心跳、A2A 异步完成通知等）的 `.` 都不会渲染到聊天或会话恢复里；`[Heartbeat]` 触发消息被过滤，真正的自主回复显示为 `⚡ 自主`。这是涌现自发性行为的前提。

### 定时任务

让 agent 按计划做事——"每晚 12 点写日记"、"每周五总结本周"——它会注册一个 cron 定时任务（`scheduled_task_set`）。任务名同时也是执行它的 worker 的名字，所以请用简短的 ASCII 标识符。任务触发时，agent 把工作派发给一个以任务名命名的 subagent worker（`run_schedule_now`）而非亲自执行，worker 完成后把结果作为**报告**存入文件柜（`report_save`）并通知你。每次触发都有记录（`scheduled_run_list`），所以你能看到跑了什么、产出了什么（`report_list` / `report_read`）。任务在创建时**必须有描述**——它就是 worker 的指令，因此空任务不可能存在。`run_schedule_now` 接受 `clone_context=True`，在任务依赖你正在讨论的内容时给 worker 一份当前对话的克隆。

任务**只在 Slife 运行时触发**。下次启动时，一次性扫描会结算上一会话留在 `scheduled_run_list` 里的记录：没跑完的记为**未完成（failed）**，Slife 关闭期间到点没做的记为**错过（missed）**。不做任何提示、也不等你的输入——未完成或错过的运行仍可用 `run_schedule_now` 补做（立即触发），或用 `scheduled_run_skip` 关闭。

### Jobs — 确定性、代码定义

对于**定义明确、可重复**的工作——翻译、摘要、抽取、分类、格式化——用 **Job** 运行一个代码定义的函数、只传它声明的参数，而不是把整个会话拖进一个 agent turn。Job 就是 `~/.slife/jobs/` 下的普通 `.py` 文件（一个公开函数 = 一个 job 工具；写法和内置的 `translate` / `summarize` 样例见 `job-coding` skill）。插件每次启动重载它们，并支持实时管理：

- `job-list` — 查看已注册的 job
- `job-write` / `job-remove` — 新增/改动（创建或替换一体；写入出错自动回滚）或删除 job；它的工具立即出现/消失，重启后依然生效
- `job-run` — 按名执行任意 job，或直接调用该 job 自己的工具

需要大模型的 job 通过它自己导入的 `llm` 句柄**一次性**调用（`from slife.plugins.job_coding import llm`——没有任何东西被自动注入）：一次狭窄、显式的 `llm.chat`，走 `job_coding_model`——一个独立于会话 active model 配置的模型，让 job 保持便宜、永不扰动 agent 的 prompt 缓存。任何对话历史、系统提示词、agent loop 都到不了 job。

job 还能通过 `mcp` 句柄（`from slife.plugins.job_coding import mcp`）驱动 `tools.yaml` 里配置的**任意外部 MCP server**——裸 MCP，一句一次工具调用：`await mcp.call(server, tool, args)`。调用走 mcp-gateway 的持久连接，因此任何外部 server 都绝不会被二次启动，而且能触达**主 agent 尚未加载的工具**。`mcp.call` 永不抛异常：网关不可达、server 掉线/被禁用、工具不存在，都返回一个清晰的 `Error: ...` 字符串供 job 分支判断。

### 图片与视觉

用 `@path` / `@url` 语法附加图片（带空格的路径可加引号），喂给支持视觉的模型：

```
看看这张截图 @D:\Downloads\error.png
```

支持视觉的模型以 base64 data URI 接收本地文件，HTTP(S) URL 原样透传；`attach_image` 工具让 agent 能在对话中途附加图片（本地来源上限 20 MB，防止误把超大文件 base64 进上下文）。终端里从不渲染任何内容——文件用系统默认程序打开，`share_file` 通过可插拔隧道 provider（ngrok / localhost.run / Cloudflare Quick Tunnel，由 `sharefile.yaml` 选择）把任意本地文件发布为公开 HTTPS 链接（隧道离线时 `share_file` 返回优雅错误）。

### 插件

九个内部插件各自作为独立子进程运行，每个都在中央插件 spec 中声明一行、由同一套统一生命周期驱动（spawn → MCP 握手就绪 → watchdog → health）。其中之一——**mcp-gateway**——是外部 MCP 服务器的网关：第三方能力只能作为 `tools.yaml` 里的标准 MCP 服务器接入，绝不再作为 Python 插件。

| 插件 | 角色 |
|--------|------|
| **mcp-gateway** | MCP 网关——代理外部 MCP 服务器（stdio / SSE / Streamable HTTP）并持有这些连接。管理：`mcp_set`、`mcp_set_enabled`、`mcp_remove`、`mcp_list`、`mcp_list_tools`（有截断——其余用 `tool_search` 找）；工具经 `func-tool-load` 按需载入 |
| **memdb** | 对话记录数据库 + 混合搜索 |
| **wechat** | 双向微信消息 |
| **memfiles** | 笔记 / 日记 / 文件 / 报告文件柜（私有）。笔记、日记与报告双写为 markdown + SQLite 混合索引。所有保存工具都返回本地路径——绝不自动发布 |
| **sharefile** | 公开文件分享——`share_file` 把本地文件发布为公开 HTTPS URL（同端口的 `/share` 路由；隧道从 `sharefile.yaml` 配置，可插拔） |
| **a2a** | 基于 MQTT 的 A2A 网格通道（仅在 broker 可达时启动） |
| **media** | 来自任意 provider 的非聊天式 AI 生成（图片、视频、TTS、ASR）——自持 `media:` 配置段与跟 provider 无关的适配层。工具：`generate_image`、`generate_video`、`text_to_speech`、`transcribe_audio` |
| **job-coding** | 确定性 jobs 作为 MCP 工具——`~/.slife/jobs/` 里的代码定义函数按声明的参数精确执行；一次性 LLM 调用走 `llm.chat`、用 `job_coding_model`。工具：`job-list`、`job-write`、`job-remove`、`job-run` + 每个 job 一个 `job-<函数名>` |

所有内置插件都跑一个**看门狗（watchdog）**，崩溃时自动重启（指数退避 1s→30s，最多连续 5 次失败），只有在插件稳定运行约 60 秒后才恢复重启计数。就绪遵循 MCP 标准（`initialize` 握手只在插件自身 init 成功后才完成）；**必需插件**（`plugins.required`——随附配置里是 `memdb` 与 `memfiles`）是核心：无法就绪时**中止启动**而不是带病运行。外部/从属依赖——外部 MCP 服务器、隧道、微信登录、媒体 provider、A2A broker——从不阻塞就绪：它们不可控、运行时会自愈，并经由 `system_health` 里的状态工具单独上报。`local-embed` 属于插件而非外部依赖，但同样**不是**必需插件，所以它启动失败也不会中止启动——它和其他子进程一样被 spawn，并上报自己的状态。

### A2A — 智能体间网格

A2A 协议运行在可插拔的传输 **binding**（当前为 MQTT）上，让多个智能体——同一台机器或不同机器——互相发现、发送任务与消息、共享结果：

- **网格工具**（统一 `a2a_` 前缀）：`a2a_send_task`、`a2a_send_task_async`、`a2a_send_message`、`a2a_send_message_async`、`a2a_get_task_result`、`a2a_cancel_task`、`a2a_list_agents`、`a2a_list_tasks`、`a2a_agent_card`、`a2a_broadcast`、`a2a_set_task_done`（完成收到的任务并发布结果）。入站的 peer 消息/任务以 **`[A2A:{"from": …, "task_id": …}]`** 前缀到达模型（`from` 是发送方 peer——永远不是接收者自己；`task_id` 只在任务时出现——无状态消息只带 peer），自动推送的异步结果以 **`[A2A-PUSH:…]`** 前缀；TUI 显示 `A2A(<peer>)>`。任务由模型显式用 `a2a_set_task_done` 完成——harness 不再自动回发。`a2a` 插件只在 MQTT broker 可达时启动。
- **Subagent 是本地 worker，不是 A2A peer**：`spawn_subagent` / `subagent_send_task` / `subagent_get_task_result` / ……创建共享你的插件、一次处理一个任务的子进程 worker（对忙碌 worker 的同步发送会自动转为异步入队）。异步结果自动推送到你的聊天（`mode="auto"`，默认）或只能轮询（`mode="poll"`）。Subagent 绝不清空你的收件箱——所有回复与管理都属于主 agent。

所有消息——人类输入、微信、MQTT、subagent 结果——都流经单一收件箱队列，逐轮处理。

<a id="semantic-memory-search--installation-guide" name="semantic-memory-search--installation-guide"></a>

## 语义记忆搜索 — 安装指南

语义（混合）记忆搜索——跨越 `memdb` 轮次与 `memfiles` 笔记按含义召回——需要一键安装器**刻意不带**的**两样东西**：一个本地嵌入**后端**（Python 包，依赖平台）和**模型权重**（由你下载——服务器从不自动下载）。关键词搜索（`grep` / `fts5` / `time`）不需要这些即可工作。设置是一个**用户手动**步骤；每个环节都 fail-open，所以缺后端也留下一个可用的纯关键词搜索核心。

**它是怎么拼起来的。** Slife 把每个嵌入 provider 都当成 OpenAI 兼容端点（`base_url` + `api_key`）。`local-embed` 服务——由 slife 作为内部插件启动，也可以通过 `local-embed` CLI 独立运行——把每个本地模型**加载一次**，并在 `http://127.0.0.1:17347/v1` 提供（`POST /v1/embeddings`、`GET /v1/models`、`GET /health`）。`memdb` 与 `memfiles` 都调用这个端点，所以一个模型永远不会被加载两次。local-embed **没有 "active model"**——每个请求都指名它要的模型；slife 的 `embeddings.active_model`（例如本地守护进程用 `"local_embed"`、云 provider 用 `"siliconflow"`——随附配置默认 `"siliconflow"`）选择用哪个 provider 做嵌入。

### 1. 安装后端依赖

把后端**装进 slife 的工具 venv**——与 `local-embed` 运行在同一个解释器。用它的**根目录**引用这个 venv：`"$(uv tool dir)/slife"`——这在 **macOS、Linux 与 Windows 上都能用**（uv 在 venv 内部定位解释器，所以你无需知道究竟是 `bin/` 还是 `Scripts/`）：

| 后端 | 命令 |
|---------|---------|
| **Transformer · 无 NVIDIA GPU**（Linux / WSL / Windows） | 先 `uv pip install --python "$(uv tool dir)/slife" --index-url https://download.pytorch.org/whl/cpu torch`，再 `uv pip install --python "$(uv tool dir)/slife" sentence-transformers` |
| **Transformer · 有 NVIDIA GPU，或 macOS** | `uv pip install --python "$(uv tool dir)/slife" sentence-transformers` |
| **GGUF · CPU（Linux / WSL / macOS）** | `uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34` |
| **GGUF · NVIDIA CUDA（Linux）** | `CMAKE_ARGS="-DGGML_CUDA=on" uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34`（需要工具包**和** NVIDIA 设备） |
| **GGUF · macOS Metal** | `CMAKE_ARGS="-DGGML_METAL=on" uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34` |
| **GGUF · Windows CPU** | `uv pip install --python "$(uv tool dir)/slife" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.34` |
| **GGUF · Windows CUDA** | `uv pip install --python "$(uv tool dir)/slife" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 llama-cpp-python==0.3.34`（把 `cu124` 换成驱动支持的 CUDA 版本——`cu118`、`cu121`…`cu125`、`cu130`、`cu132`） |

- llama-cpp-python **没有 PyPI wheel**（只有 sdist），所以 Linux / WSL / macOS 三行会**源码编译**——这是标准构建——需要 **C 编译器 + CMake ≥ 3.21**（macOS：Xcode CLT clang；Linux：`build-essential` + `cmake`）。GPU 两行传 `CMAKE_ARGS` 选择后端。**Windows 没有默认 C 工具链**，所以它改用上游预编译的 wheel——CPU 或 CUDA，都不需要 MSVC。
- 两个后端可以共存——在**一次** `uv pip install` 里都装上（例如 `sentence-transformers` 加上 `llama-cpp-python` 的 CPU 行写进一条命令）。装两次会替换掉第一个安装。
- `sentence-transformers` 会带上 `torch`，而 Linux 上 PyPI 的 `torch` 是 **CUDA 构建**：十几个 `nvidia-*` 运行时 wheel（约 2.5 GB），无论有没有 GPU——里面没有 `nvcc`，所以在无 GPU 的机器上它们什么都启用不了。这正是上面 CPU 行先装 PyTorch 官方索引的 `torch` 的原因：第二条命令随后发现 torch 已满足，不会再拉这些 wheel。事后再换成 CPU 构建会让这些 wheel 变成孤儿，清理：`uv pip freeze --python "$(uv tool dir)/slife" | grep ^nvidia | cut -d= -f1 | xargs uv pip uninstall --python "$(uv tool dir)/slife"`。`llama-cpp-python` 完全不依赖 torch。
- 若后端缺失，`local-embed` 会记录一条针对你平台的精确安装命令，而不是静默失败。

### 2. 下载模型权重

默认离线——`HF_HUB_OFFLINE=1`，**不自动下载**。你自己通过下面两条路线之一把权重准备好。`hf` CLI 不随后端提供——一次性安装即可：`uv tool install "huggingface-hub[cli]"`（也可以给任何 `hf` 命令加 `uvx --from huggingface-hub` 前缀）。

**Transformer 路线（默认配置，约 2 GB）。** 随附配置的模型是 `BAAI/bge-m3`；把它下载进 HF 缓存，无需改配置：

```bash
hf download BAAI/bge-m3                                    # → ~/.cache/huggingface/hub
HF_ENDPOINT=https://hf-mirror.com hf download BAAI/bge-m3  # 国内镜像
```

**GGUF 路线（离线单文件）。** 用你信得过的任何量化版 BGE-M3 GGUF——这些是社区转换，没有唯一权威来源（优先高保真的 `Q8_0`，约 635 MB；更重的量化更小）。从任何来源获取（HF 单文件拉取、浏览器、`wget`/`curl`），然后放到默认路径并让客户端指向 `bge-m3` 模型：

```bash
hf download <owner>/<repo> <model>.gguf --local-dir ~/.local-embed/models   # HF 单文件拉取
mv ~/.local-embed/models/<model>.gguf ~/.local-embed/models/bge-m3-Q8_0.gguf     # 期望的默认路径
```

`models` 映射里的每个模型都作为**对等（peer）**被提供——没有 `active_model`；客户端在每个请求上指名模型（现有配置里过时的 `active_model` 键会被忽略）。

### 3. 配置 HF 缓存与 GGUF 路径

一切——host、port、models、backend——都住在 **`local_embed.yaml`** 里，由安装器铺设（路径解析：`$LOCAL_EMBED_FILE` > slife 项目根目录（开发）> `~/.local-embed/local_embed.yaml`）。值支持 `${VAR}` / `${VAR:-default}` 展开，**shell 环境变量优先于配置**。随附文件已经带有可移植的占位符——通常你只需设置环境变量或改两行：

```yaml
env:
  HF_HUB_CACHE: "${HF_HUB_CACHE:-~/.cache/huggingface/hub}"   # transformer 仓库解析到哪
  HF_HUB_OFFLINE: "${HF_HUB_OFFLINE:-1}"          # 1 = 永不自动下载；0 = 允许按需下载
models:
  "BAAI/bge-m3":
    backend: "transformer"
    model: "BAAI/bge-m3"
  "bge-m3":
    backend: "gguf"
    gguf_path: "${BGE_M3_GGUF_PATH:-~/.local-embed/models/bge-m3-Q8_0.gguf}"
port: 17347
```

| 设置 | 含义 |
|---------|---------|
| `env.HF_HUB_CACHE` / `HF_HUB_CACHE` | Transformer 路线解析 HF repo id 的位置。默认 `~/.cache/huggingface/hub`。若你的模型下载到别的缓存，把它指过去——否则仓库会被静默重新拉取。 |
| `env.HF_HUB_OFFLINE` / `HF_HUB_OFFLINE` | `"1"`（默认）——离线；模型必须已经在缓存/磁盘上。`"0"`——允许模型加载器访问网络（无托管下载/镜像回退）。 |
| `models."bge-m3".gguf_path` / `BGE_M3_GGUF_PATH` | GGUF 路线的 `.gguf` 文件。`~` 会展开；shell 里的 `BGE_M3_GGUF_PATH` 覆盖配置默认值。 |

请求会指名它们想要的模型（slife 的 provider `model` id——transformer 路线是 `"BAAI/bge-m3"`，GGUF 路线是 `"bge-m3"`）。改动在 local-embed 服务下一次启动时生效（重启 slife）。

**CLI 替代方案** —— `local-embed`（安装后已在 PATH）upsert 一个模型配置并钉住端口（幂等，不影响其它模型）：

```bash
local-embed set BAAI/bge-m3 --HF_HUB_CACHE ~/.cache/huggingface/hub
local-embed set-gguf bge-m3 --path ~/.local-embed/models/bge-m3-Q8_0.gguf
```

### 4. 让服务就绪 — 验证

直接启动 slife 即可——它会为你启动 `local-embed`（同一个服务也在 PATH 上，即 `local-embed` CLI）。自己先启动它是可选的：如果**端口上已经有实例在服务**，slife 会直接使用那个实例，而不会再加载一份模型——这正是 Windows 上的 slife 与 WSL 里的 agent 共用一个服务的方式。参见 [local-embed → Adopting a running service](local-embed/README.md#adopting-a-running-service)。

**模型加载是延迟的**——第一次嵌入时才加载（GGUF 几秒，约 2 GB 的 transformer 最多一分钟）。从聊天里或通过 HTTP 验证：

- **在聊天里** — 让 agent 运行 `system_health`（`memdb`/`memfiles` 组件会报告语义门：`semantic_ready`、模型、pending embeddings）。
- **通过 HTTP**（服务独立运行在固定端口上）：

```bash
curl http://127.0.0.1:17347/health            # {status, backend, model, dimension, loaded}
curl http://127.0.0.1:17347/v1/models         # 每个已配置模型 + active 标志
curl http://127.0.0.1:17347/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model": "bge-m3", "input": ["hello world"]}'   # 返回一个真实向量
```

健康状态：`/health` → `status: ok`；`system_health` → `embeddings` 组件探测活动嵌入端点（可达 = `ok`，并报出本次会话嵌入用的模型——无论这个端点是本地守护进程还是硅基流动这类云 provider），`memdb`/`memfiles` 组件显示 `semantic_ready`。当服务不可达（后端缺失、权重缺失、仍在加载）时，slife **优雅降级为关键词搜索**——`system_health` 报告原因，一旦当前模型的索引完整构建，hybrid 结果自动恢复。

### 故障排查

| 症状 | 修复 |
|---------|-----|
| 日志：`backend_unavailable … reason=llama_cpp_not_installed` / `sentence_transformers_not_installed` | 按你的平台跑第 1 步安装——日志会打印精确命令。 |
| Transformer 路线在 `HF_HUB_OFFLINE=1` 下加载失败 | 仓库不在缓存里——跑 `hf download BAAI/bge-m3`，并确保 `HF_HUB_CACHE` 指向持有它的缓存。 |
| GGUF 路线加载失败 | `gguf_path` 处文件缺失——检查 `BGE_M3_GGUF_PATH` / `gguf_path`，以及客户端请求的是 `"bge-m3"`（所有已配置模型都是对等——没有任何东西被挡在 `active_model` 后面）。 |
| `system_health` 显示 `embeddings` 为 `unavailable` | 活动嵌入端点没有应答 `GET /v1/models`。如果该 provider 是 `local_embed`，请看同一份报告里的 `local-embed` 那一行——服务由 slife 自己启动，所以缺失意味着插件启动失败（原因在它的日志里；**非** local-embed 的服务占着 17347 端口就会这样）。否则修云 provider 的 key：`api_key` 按 `${VAR}` → env → credstore 解析。该组件只探测**活动的** provider，行首的 key 就是该 provider 的 id。 |
| 首次嵌入非常慢 | transformer 下载/预热延迟到第一次嵌入；后续调用很快。 |

### 可选扩展（手动安装）

上面那些 embedding 后端就是同类的 "可选扩展"——供直接手动安装（uvx / git 检出）或在 slife venv 里重新加入后端：

| 扩展 | 启用功能 |
|-------|---------|
| `local-embed[gguf]` | 通过 llama-cpp-python 的本地 GGUF 嵌入（离线，约 300 MB） |
| `local-embed[transformer]` | 通过 sentence-transformers 的 HuggingFace transformer 嵌入（约 2 GB） |
| `slife[gguf]` / `slife[transformer]` / `slife[embeddings]` | 旧版进程内嵌入（默认不使用） |

```bash
# 工具安装（安装脚本）——装进 slife 的工具 venv：
uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34   # slife[gguf]
uv pip install --python "$(uv tool dir)/slife" sentence-transformers      # slife[transformer]

# uvx / git 检出——没有工具 venv；把扩展加进临时环境：
uvx --with llama-cpp-python==0.3.34 --from git+https://github.com/juzcn/slife.git slife
```

各平台的 wheel 选型见第 1 步的表（Windows 用 `--extra-index-url …/whl/cpu` 或 `…/whl/cu124`，Linux/Metal 的 GPU 构建用 `CMAKE_ARGS`）；无 GPU 的 Linux 机器先装 CPU 版 `torch`——两者都在第 1 步的要点里。

<a id="usage-reference" name="usage-reference"></a>

## 使用方法参考

### 键盘快捷键

键帽（`Ctrl+C`、`Esc` 等）通用不变；其后的动作词随界面语言本地化。

| 按键 | 动作 |
|-----|--------|
| `Ctrl+C` | 退出 |
| `Esc` | 取消 Agent Loop |
| `Ctrl+S` | 切换模型（内联选择器——输数字选，Esc 取消） |
| `Home` / `End` | 滚动到顶部 / 底部 |
| `Ctrl+Y` | 复制结果（工具调用上） |
| `Enter` / `Space` | 展开/收起思考块（助手消息上） |
| `↑` / `↓` | 输入历史导航 |
| `Shift+Enter` | 输入框内换行 |

### CLI

| 参数 | 说明 |
|------|-------------|
| `--agent <id>` | 智能体标识 — 独立对话记录数据库 + A2A 网格名称（默认：`slife`） |
| `--lang <en\|zh>` | 界面语言 — 强制英文 / 中文（默认：按 OS 区域自动检测） |
| `<配置路径>` | 位置参数 — 使用指定的配置文件（其父目录成为数据目录） |

### 健康与日志

* **`system_health`** 一次调用报告每个子系统的实时状态——先说问题和该怎么处理，然后每个健康组件一行——它是 agent 唯一的健康工具（各子系统的 `check_*` 函数是内部实现）。感觉任何东西不对劲时都让 agent 跑一下。
* **日志**在 `~/.slife/logs/`（每个会话一个文件，`event_name key=value` 行格式，DEBUG+；插件继承会话 id）。终端保留给 TUI——除了聊天，什么都不往终端打印。

<a id="development" name="development"></a>

## 开发

Slife 是一个代码库、几份文档，按读者拆分：

* **[DESIGN.md](DESIGN.md)** — 面向代码开发者的架构与实现：agent loop、上下文工程、工具系统、插件架构、MCP 网关、记忆、A2A。
* **[PLUGIN_CONTRACT.md](docs/PLUGIN_CONTRACT.md)** — 插件系统的权威规范（中央 `PluginSpec` 表、registry、统一生命周期），给所有写插件的人。
* **[CONTEXT_HARNESSING.md](docs/CONTEXT_HARNESSING.md)** — Slife 每轮如何策划模型上下文：渠道、标记、`_turn_prompt` harness 工具对。

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

开发模式自动检测（从源码树运行时）：数据文件留在项目目录里。生产安装（uv tool / pipx / pip）一律使用 `~/.slife/`——即使在 checkout 目录或 home 目录里启动也不会误判。CI 在 Ubuntu、macOS 与 Windows 上用 Python 3.13 跑测试套件（测试针对构建出的 wheels 运行）。

## 许可证

MIT