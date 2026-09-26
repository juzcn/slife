# Slife

**终端 AI 智能体** — 基于函数调用循环的最小化框架。与 LLM 对话，它能调用工具、永久记忆每一轮对话、协调其他智能体。

```
你: "找出所有 TODO 注释并为每个创建 GitHub Issue"
  → LLM 用 execute_shell 在代码库里 grep
  → LLM 为每条结果调用 github__create_issue(...)
  → LLM: "已创建 7 个 Issue，链接见上文。"
```

一个 TUI 窗口包裹一个 LLM 工具循环：**60 个工具**开箱即用，横跨 13 个类别，始终开启的混合搜索记忆、视觉图片附件（`@path`/`@url`）、三种 API 后端运行时切换模型、智能体间（A2A）网格——外加九个内置服务：记忆、微信、markdown 文件柜、公开文件分享、A2A 网格、图片/视频/语音生成、确定性 jobs、MCP 网关，以及本地 embeddings。

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
> * **要开发或调试 Slife 本身** → [从源码运行](#development)
> * **要改 Slife 的代码** → [DESIGN.md](DESIGN.md)——子系统、机制、不变量

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

**零前提、开箱即用。** 安装脚本拉取最新的 `main`，**从源码**构建 slife（工作区 wheel——不用 PyPI，永远是当前最新代码），并用 uv 装进一个隔离的工具 venv。它会自动安装缺失的东西——uv、Node.js（`npx`）、bun、Mosquitto——再把随附的配置、skills 与示例 jobs 铺到位，于是首次用户即可拥有完整工具集——本地 embeddings、外部 MCP 服务器、yt-dlp、browser-harness、A2A 网格——无需手工配置任何东西。在 WSL 上使用 Linux 原生运行时（Windows 可执行文件无法经 WSL interop 接收自定义环境变量）。

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

**默认安装以下内容：**`yt-dlp` 与 `browser-harness`（两者都被 `--core` 跳过）、Mosquitto 与 `cloudflared`（除非 `--core`，否则会尝试）、从随包默认值铺设的**四份配置**（`slife.yaml`、`tools.yaml` 和 `sharefile.yaml` → `~/.slife/`，`local_embed.yaml` → `~/.local-embed/`），以及随附 skills（`~/.slife/skills/`）和示例 jobs（`~/.slife/jobs/`）。

如果某个运行时装不上，安装器**警告并继续**——slife 本身仍会安装，只是需要该运行时的功能不可用。举例来说，在一台比 **glibc 2.28 / libstdc++ 3.4.29** 更老的 Linux 机器上，Node 的 no-root tarball 回退方案跑不起来（安装器会报告缺失的 `GLIBC_2.28` / `GLIBCXX_3.4.xx` 符号）。受支持的路线**不是**装旧版 Node——而是装一个为你的发行版构建的 Node（例如 HPC 集群上的 `module load nodejs`，或发行版自带的包）。装好后再重跑这个安装器——它会检测到已有的 `npx` 并跳过自己的 Node 安装。

如果你手工编辑 `tools.yaml`，改动会在下一次启动时生效。每个可选步骤都 **fail-open**：出错只会警告并继续，留下一个可用的核心。

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

### 分词器词表（仅用于修复）

Slife 用 `tiktoken`（OpenAI 的 BPE）度量上下文大小，其 3.6 MB 的词表在首次使用时
下载。安装器已经取过一次；这里讲的是如何修复它。该下载**没有超时**，所以在慢速或
被代理限速的链路上它会卡住而不是报错——提前取一次，可以把它挡在 agent 首轮之外：

```bash
mkdir -p ~/.cache/tiktoken
curl -fL --retry 3 -o ~/.cache/tiktoken/fb374d419588a4632f3f557e76b4b70aebbca790 \
  https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken
```

文件名是该 URL 的 SHA-1（即 tiktoken 的缓存键），文件必须**恰好 3613922
字节**。存在但更短的文件会被拒绝使用，且 Slife 会报告出来——删掉它就会重新下载。

### 免安装试用

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

<a id="development" name="development"></a>
### 从源码运行

从源码运行（用于开发或调试）：

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync

uv run credstore set-password
uv run credstore set DEEPSEEK_API_KEY
uv run slife

# 测试
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

`uv sync` 是**精确同步**：它会把检出目录的 `.venv` 对齐到 lock，而 lock 里没有任何 embedding 后端（它们是按平台手动安装的——见[重新加入后端（手动安装）](local-embed/README.md#re-adding-a-backend-manual-installs)）。如果你想在开发 venv 里保留 `llama-cpp-python` 或 `sentence-transformers` 做真实端到端运行，要么用 `uv sync --inexact`，要么同步后重新装上后端。

从源码树运行时，数据文件留在项目目录里；任何已安装的副本一律使用 `~/.slife/`。

### 更新

重跑安装脚本即可升级 slife——它从最新的 `main` 重建，并保留你自定义过的东西：

- **可选包**（如 `sentence-transformers`、`llama-cpp-python`）会从旧的工具 venv 中捕获，在全新安装后重新加入，并与新的基础版本做 diff，避免任何重复。
- **已存在的配置、skills、示例 jobs** 一律保持原样，安装器**不再询问、绝不覆盖**。缺失的默认文件直接铺设；内容完全相同的静默跳过；当随包默认值发生变化时，新默认会被铺设到 `~/.slife/` 下作为**带版本号的参考副本**——配置与 jobs 形如 `<文件名>.<版本号>.<后缀>`（如 `slife.0.9.8.yaml`、`total_tokens.0.9.8.py`），skills 形如 `<名称>.<版本号>/`——每次写入都以 `seeded <文件> → <文件夹>` 的形式提示，需要它时一条 `cp` / `Copy-Item` 即可应用。重复安装会刷新同版本副本，旧版本副本保留作参考。判定 skill 是否不同，只看**随包默认自带的那些文件**是否有差异，因此你自己往 skill 里加的文件不会触发参考副本。

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
| **工具配置** | `~/.slife/tools.yaml` | 按类别的工具定义——`builtin` / `plugin` / `mcp` / `rest-api` / `job` / `cli` / `skill`（见下面插件表） |

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

每个工具接受三个元参数：`_timeout`（单次调用超时覆盖）、`_async`（后台执行，用 `check_async` 轮询）和 `_approve`（内联批准提示——Y 批准 / N 拒绝，Esc 拒绝）。

**13 个类别共 60 个内置工具**（随附配置中 `install_python_package` 默认禁用）。两个保留的 harness 工具由 Slife 自己驱动、而非模型——`_turn_prompt`（每轮提示词，每轮一次）和 `_check_new_input`（轮中消息注入）——而 `attach_image` 会在 `@` 附件时替你调用，*同时*模型自己也可以调用它；无视觉能力的模型会在调用时被拒绝。

| 类别 | 工具 |
|----------|-------|
| System | `system_health`, `system_tools_list`, `check_async`, `cancel_async`, `set_max_iterations`, `set_midturn_input`（轮中抢先开/关）, `notify_user`, `wait_minutes`（暂停本轮，稍后自动继续）, `add_user_pref`（把偏好记录到 `USER.md`） |
| Execution | `execute_shell`, `run_python_script`, `install_python_package`（默认禁用） |
| Schedule | `scheduled_task_set`, `scheduled_task_remove`, `scheduled_task_list`, `scheduled_run_list`, `scheduled_run_skip`, `run_schedule_now` |
| Job | `job-<name>`——你写在 `~/.slife/jobs/` 里的每个 job 一个工具；通过 `job-list` / `job-write` / `job-remove` / `job-run` 增删，这四个属于 `job-coding` 插件 |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_list_tools`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image`（给视觉模型喂图片）, `_turn_prompt`（每轮提示词，由 Slife 驱动） |
| Harness | `_check_new_input`（轮中消息注入，由 Slife 驱动） |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| embeddings | `embeddings_model_list`, `embeddings_model_set`, `embeddings_model_switch`, `embeddings_model_remove`, `embeddings_enable` |
| ToolSystem | `tool_search`（搜索全部类别）、`func_tool_load`（载入一个工具——mcp/rest-api 的也走它）、`_func_tool_unload`（按名卸载） |

**托管类别**（Skills / CLI / REST API / Models / MCP）支持 `X_list` / `X_set` / `X_remove`（+ 有开关时 `X_set_enabled`）。每个 `X_set` 工具都是幂等 upsert，`model_set` 则**合并**进现有条目——局部更新会保留你没有提到的字段。`rest_api_set` 让一个条目指向一份 OpenAPI 文档，文档里的每个端点成为一个带类型的 `{name}__{endpoint}` 工具——REST API 就是 `tools.yaml` 里 `rest-api` section 中的一个普通条目。

**插件工具** — 内置插件以裸名注册；外部 MCP 服务器以 `{server}__{tool}` 出现：

| 服务器 | 工具 |
|--------|-------|
| `mcp-gateway` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`（有截断——其余用 `tool_search` 找） |
| `memdb` | `turn_search`, `turn_list`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_save`, `diary_save`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `file_list`, `cabinet_search`, `file_read`, `report_save`, `report_list`, `report_read` |
| `sharefile` | `share_file`, `sharefile_unshare` |
| `a2a` | `a2a_send_message`（异步——返回 task_id，结果稍后自动推送）、`a2a_cancel_task`、`a2a_list_agents`、`a2a_broadcast` |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| `job-coding` | `job-list`, `job-write`, `job-remove`, `job-run` + 每个已注册 job 一个工具（如 `job-translate`） |

**工具按需加载。** 第三方能力以 `tools.yaml` 里的标准 MCP 服务器接入（`mcp` + `rest-api` 两个 section——任何 stdio / SSE / Streamable HTTP 服务器都可以，无需 Slife SDK）。模型用 `tool_search` 找工具，再用 `func_tool_load(full_name)` 把工具注入——按工具而非按服务器，所以一个上千工具的大服务器只会把真正在用的那几个放到模型面前。加载决定模型*看得见*什么，不决定什么能跑：只要有执行实例，工具按名字就能调用，加载与否皆然。少数工具始终可用——harness 工具、系统元工具、固定注入的 `skill_use` / `system_health` / `attach_image`，以及你标了 `autoload: true` 的条目——注入列表由 `tool_load.threshold` 封顶（默认 100），淘汰时先淘汰最久未用的，从不淘汰 `autoload` 的。已启用的服务器在启动时被拉起；连不上的服务器其工具被标记 `error`，因此死连接永远不会被注入。

**Windows 下的命令执行。** `execute_shell` 在 Slife 检测到的 shell 中运行——PowerShell 或 cmd——所以为该 shell 写的命令按原样执行，非 ASCII 输出也能正确解码。`run_python_script` 让子进程以 UTF-8 运行，因此非 ASCII 输出不会让它崩溃。

### 记忆 — 始终开启

每轮对话永久记录在 SQLite（`~/.slife/<agent>.db`），并有四种搜索方式：

**记忆是核心功能——agent 绝不在记忆失效时静默运行。** 若记忆数据库损坏（缺列、损坏或磁盘错误），agent 会响亮地失败而非假装正常：无法恢复的会话在启动时报错中止；无法保存的轮次会冻结收件箱并显示红色横幅——不再处理新轮次，直到数据库修复并重启 agent。

| 模式 | 适用场景 |
|------|----------|
| `grep` | 正则表达式 — 也支持部分拼写：错误信息、文件路径、代码 |
| `fts5` | 主题/关键词搜索，带排序摘要 |
| `hybrid` | 语义召回 — 关键词与含义结合 |
| `time` | 按日期浏览 |

Embeddings 是 `slife.yaml` 中**一级顶层的 `embeddings` 配置段**（由 `memdb` + `memfiles` 共享），由内置 `embeddings_*` 工具管理；运行时索引状态由 `system_health` 上报。每个 provider 都是 **OpenAI 兼容端点**（`base_url` + `api_key`），而 `active_model` 指的是*provider*（例如 `"local_embed"` 或 `"siliconflow"`）而不是某个模型。**`local-embed` 服务**——由 Slife 作为插件为你启动，或你自己启动的那个实例——在 `http://127.0.0.1:17347/v1` 提供本地 GGUF/transformer 模型。**没有嵌入后端时关键词搜索照样工作。** 语义（hybrid）结果只在当前模型的索引完整构建后才返回——重建运行期间 hybrid 退回关键词搜索，索引进度完成时自动恢复。

每轮对话还记录两个时间戳——你的输入时间（`created_at`，敲下回车的那一刻）和 assistant 的完成时间（`completed_at`）——以灰色 `[HH:MM]` 标记显示。用户消息还带一条 **`[INFO: {"turn_id": N, …}]`** 脚注（turn id 加发生时间），agent 用它按 turn id 引用轮次（`turn_read` / `turn_summarize`），你在同一行里读到它。

每轮对话还记住**它来自哪个渠道**——`human`、`wechat`、subagent、心跳、A2A peer，或 `system`（Slife 自身）——因此会话恢复时每个气泡都带对应的前缀：`You>`、`Wechat>`、`Subagent(<name>)>`、`Heartbeat>`、`A2A(<agent>)`。进入的微信消息还会以 **`[Wechat:{...}]`** 前缀到达模型。

### 自主心跳

空闲时，agent 可以取一个周期性的自主窗口——**随附配置里是关闭的**（`agent.heartbeat_interval: 0`），因为它自己会消耗 token。把它设成一个秒数即可开启（不写这个键则为 30 分钟）。它作为一个正常 turn 运行（独立的一轮，存入记忆）；回复契约是：有值得说的话就输出真实内容，否则只输出一个 `.`。单独的 `.` 回复表示**沉默**——来自任何事件的 `.` 都不会渲染到聊天或会话恢复里——而真正的自主回复显示为 `⚡ 自主`。这是涌现自发性行为的前提。

### 定时任务

让 agent 按计划做事——"每晚 12 点写日记"、"每周五总结本周"——它会注册一个 cron 定时任务（`scheduled_task_set`）。任务名同时也是执行它的 worker 的名字，所以请用简短的 ASCII 标识符。任务触发时，agent 把工作派发给一个以任务名命名的 subagent worker（`run_schedule_now`）而非亲自执行，worker 完成后把结果作为**报告**存入文件柜（`report_save`）并通知你。每次触发都有记录（`scheduled_run_list`），所以你能看到跑了什么、产出了什么（`report_list` / `report_read`）。任务在创建时**必须有描述**——它就是 worker 的指令，因此空任务不可能存在。`run_schedule_now` 接受 `clone_context=True`，在任务依赖你正在讨论的内容时给 worker 一份当前对话的克隆。

任务**只在 Slife 运行时触发**。下次启动时，一次性扫描会结算上一会话留在 `scheduled_run_list` 里的记录：没跑完的记为**未完成（failed）**，Slife 关闭期间到点没做的记为**错过（missed）**。不做任何提示、也不等你的输入——未完成或错过的运行仍可用 `run_schedule_now` 补做（立即触发），或用 `scheduled_run_skip` 关闭。

### Jobs — 确定性、代码定义

对于**定义明确、可重复**的工作——翻译、摘要、抽取、分类、格式化——用 **Job** 运行一个代码定义的函数、只传它声明的参数，而不是把整个会话拖进一个 agent turn。Job 就是 `~/.slife/jobs/` 下的普通 `.py` 文件（一个公开函数 = 一个 job 工具；写法和内置的 `translate` / `summarize` 样例见 `job-coding` skill）。插件每次启动重载它们，并支持实时管理：

- `job-list` — 查看已注册的 job
- `job-write` / `job-remove` — 新增/改动（创建或替换一体；写入出错自动回滚）或删除 job；它的工具立即出现/消失，重启后依然生效
- `job-run` — 按名执行任意 job，或直接调用该 job 自己的工具

需要大模型的 job **一次性**调用它——一次狭窄、显式的 `llm.chat`，走 `job_coding_model`——一个由你独立于会话 active model 配置的模型，让 job 保持便宜、永不扰动 agent 的 prompt 缓存。任何对话历史、系统提示词、agent loop 都到不了 job。

job 还能驱动 `tools.yaml` 里配置的**任意外部 MCP server**——包括主 agent 尚未加载的工具——一句一次调用：`await mcp.call(server, tool, args)`。`mcp.call` 永不抛异常：网关不可达、server 掉线/被禁用、工具不存在，都返回一个清晰的 `Error: ...` 字符串供 job 分支判断。需要这两个句柄中的任意一个，job 自己导入：`from slife.plugins.job_coding import llm, mcp`。

### 图片与视觉

用 `@path` / `@url` 语法附加图片（带空格的路径可加引号），喂给支持视觉的模型：

```
看看这张截图 @D:\Downloads\error.png
```

支持视觉的模型以 base64 data URI 接收本地文件，HTTP(S) URL 原样透传；`attach_image` 工具让 agent 能在对话中途附加图片（本地来源上限 20 MB，防止误把超大文件 base64 进上下文）。终端里从不渲染任何内容——文件用系统默认程序打开，`share_file` 通过可插拔隧道 provider（ngrok / localhost.run / Cloudflare Quick Tunnel，由 `sharefile.yaml` 选择）把任意本地文件发布为公开 HTTPS 链接（隧道离线时 `share_file` 返回优雅错误）。

### 插件

九个内置插件各自作为独立进程运行在 agent 旁边。其中之一——**mcp-gateway**——是通往一切外部的门：第三方能力以 `tools.yaml` 里的标准 MCP 服务器形式进入 Slife。

| 插件 | 角色 |
|--------|------|
| **mcp-gateway** | MCP 网关——连接外部 MCP 服务器（stdio / SSE / Streamable HTTP）并持有这些连接。管理：`mcp_set`、`mcp_set_enabled`、`mcp_remove`、`mcp_list`、`mcp_list_tools`（有截断——其余用 `tool_search` 找）；工具经 `func_tool_load` 按需载入 |
| **memdb** | 对话记录数据库 + 混合搜索 |
| **wechat** | 双向微信消息 |
| **memfiles** | 笔记 / 日记 / 文件 / 报告文件柜（私有）。笔记、日记与报告是 `~/.slife/<agent>.files/` 下的纯 markdown，已建索引供 agent 搜索。所有保存工具都返回本地路径——绝不自动发布 |
| **sharefile** | 公开文件分享——`share_file` 把本地文件发布为公开 HTTPS URL（隧道从 `sharefile.yaml` 配置，可插拔） |
| **a2a** | 基于 MQTT 的 A2A 网格通道（仅在 broker 可达时启动） |
| **media** | 来自你所配置的 provider 的非聊天式 AI 生成（图片、视频、TTS、ASR）。工具：`generate_image`、`generate_video`、`text_to_speech`、`transcribe_audio` |
| **job-coding** | 确定性 jobs——`~/.slife/jobs/` 里的代码定义函数按声明的参数精确执行；一次性 LLM 调用走 `llm.chat`、用 `job_coding_model`。工具：`job-list`、`job-write`、`job-remove`、`job-run` + 每个 job 一个 `job-<函数名>` |
| **local-embed** | 供 `memdb` + `memfiles` 使用的本地 embedding 端点服务。也可独立运行——你已自建实例时 Slife 会用它，而不是再起一个 |

插件崩溃后会**自动重启**。**必需插件**（`plugins.required`——随附配置里是 `memdb` 与 `memfiles`）是核心：无法启动时**中止启动**而不是带病运行。其它一切从属依赖——外部 MCP 服务器、隧道、微信登录、媒体 provider、A2A broker、`local-embed`——都不阻塞启动、也不中止启动：这些依赖 fail-open、运行时自行恢复，并经由 `system_health` 上报。

### A2A — 智能体间网格

A2A 让彼此独立的智能体——同一台机器上或跨机器——互相发现、委派任务、推送结果。它说的是官方 **A2A-over-MQTT** profile，因此 Slife agent 能与任何其它实现互操作：

- **网格工具**（统一 `a2a_` 前缀）：`a2a_send_message`（异步——立即返回 task_id，结果稍后到达）、`a2a_cancel_task`、`a2a_list_agents`、`a2a_broadcast`（发后即忘的事件）。入站的 peer 流量统一以一个 `[A2A:…]` 信封到达模型（`from` 是发送方 peer——永远不是接收者自己）；TUI 显示 `A2A(<peer>)>`。`a2a` 插件只在 MQTT broker 可达时启动。
- **Subagent 是本地 worker，不是 A2A peer**：`spawn_subagent` / `subagent_send_task` / `subagent_get_task_result` / ……创建共享你的插件、一次处理一个任务的 worker（对忙碌 worker 的同步发送会自动转为异步入队）。异步结果自动推送到你的聊天（`mode="auto"`，默认）或只能轮询（`mode="poll"`）。Subagent 绝不清空你的收件箱——所有回复与管理都属于主 agent。

所有消息——人类输入、微信、MQTT、subagent 结果——都流经单一收件箱队列，逐轮处理。

<a id="semantic-memory-search--installation-guide" name="semantic-memory-search--installation-guide"></a>

## 语义记忆搜索 — 安装指南

语义（混合）记忆搜索——跨越 `memdb` 轮次与 `memfiles` 笔记按含义召回——需要一个本地 embedding
**后端**和**模型权重**，而一键安装器刻意不带这两样。关键词搜索（`grep` / `fts5` / `time`）不需要它们，
且每一环都是 fail-open：后端缺失时关键词核心照常可用。

**完整指南——各平台后端安装、权重、`local_embed.yaml`、验证、排障：**
**[local-embed/README.md](local-embed/README.md)**——`local-embed` 服务就是 slife 嵌入时打交道的那个端点，那份文件就是它的手册。

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
| `--headless` | 无 TUI——stdin/stdout 上的 worker 协议，subagent 进程就是用它和父进程对话 |
| `-h`、`--help` | 打印用法并退出 |
| `<配置路径>` | 位置参数 — 使用指定的配置文件（其父目录成为数据目录） |

### 健康与日志

* **`system_health`** 一次调用报告每个子系统的实时状态——先说问题和该怎么处理，然后每个健康组件一行。感觉任何东西不对劲时都让 agent 跑一下。
* **日志**在 `~/.slife/logs/`（每个会话一个文件，`event_name key=value` 行格式，DEBUG+；插件继承会话 id）。终端保留给 TUI——除了聊天，什么都不往终端打印。
* **被强杀的会话由下一个会话来报告。** 硬杀（`taskkill`、任务管理器"结束任务"、直接关窗口）不会执行任何 Python 代码，所以受害者自己写不下任何东西——于是每个会话在启动时留下一个标记文件（`logs/.session.<pid>.state`），只有正常退出才会删掉它。下次启动若发现标记还在而进程已不在，slife 会警告 `the last session … was killed from outside` 并给出它的日志路径。同一次强杀还会把那个终端留在 raw 模式——按键回显成乱码、`Ctrl+C` 完全失效；关掉那个终端窗口即可恢复。

## 许可证

MIT