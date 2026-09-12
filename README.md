# Slife

> **Tool tiers, in one line.** Slife presents three kinds of tools to the LLM,
> indistinguishable at the call site: **native** tools (shipped in
> `slife/tools/`, auto-discovered), **built-in plugin** tools (first-class,
> bare names — e.g. `turn_search`, `mcp_set`), and **external MCP server**
> tools (`{server}__{tool}`, loaded on demand).

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers every turn forever, and orchestrates other agents.

```
You: "Find all TODO comments and create GitHub issues"
  → LLM greps the codebase with execute_shell
  → LLM calls github__create_issue(...) for each hit
  → LLM: "Created 7 issues. All linked above."
```

One TUI window around an LLM tool loop: **57 native tools by default** across 12 categories (including the reserved harness tool `_turn_prompt`, auto-invoked each turn), **eight internal plugin services** (memdb, wechat, memfiles, sharefile, a2a, media, job-coding, and the MCP gateway `mcp-gateway`), the **`local-embed`** embedding daemon (started manually), always-on memory with hybrid search, vision image attachments (`@path`/`@url`), runtime model switching across three API backends, and an agent-to-agent mesh — everything presented to the LLM as uniform OpenAI-style function definitions.

Requires Python 3.13+. Runs on Windows (native & WSL), macOS, and Linux.

**Bilingual interface.** The TUI follows your OS language — Chinese on a Chinese system, English everywhere else. Detected at startup via [`sys-lang`](https://pypi.org/project/sys-lang/) (Windows `Get-Culture` / *nix locale); all in-TUI text — system messages, the approval prompt, the model picker, tool-call labels, the status bar — renders in the right language. What the LLM sees (system prompt, tool schemas) stays uniformly English; so do logs.

> **Reader's map** — pick your lane:
>
> * **I just want to try it** → [Quick Start](#quick-start)
> * **I'm installing this properly** → [Install](#install)
> * **I need to wire up models / API keys** → [Configuration](#configuration)
> * **What can it actually do?** → [Features](#features)
> * **I want semantic (hybrid) memory search to work** → [Semantic Memory Search — Installation Guide](#semantic-memory-search--installation-guide)
> * **Day-to-day use** (keys, flags, health) → [Usage Reference](#usage-reference)
> * **I'm going to develop or debug Slife itself** → [Development](#development) — developer docs live in [DESIGN.md](DESIGN.md)

## Quick Start

```bash
credstore set-password              # first time — encrypted backup
credstore set DEEPSEEK_API_KEY      # store API key (masked input)
slife
```

To share the same API key across multiple providers:

```bash
credstore copy DEEPSEEK_API_KEY BAILIAN_API_KEY
```

That's it. With no extra configuration you get the core loop: chat, tool calls, always-on memory, subagents, schedules. Everything else — semantic search, WeChat, media generation, external MCP servers, the A2A mesh — is opt-in from [Configuration](#configuration) and [Features](#features) below.

## Install

**Zero prerequisites, fully out-of-the-box.** The install script fetches the latest `main`, builds slife **from source** (workspace wheels — no PyPI, always the newest code), and installs it with uv into an isolated tool venv. It auto-installs what's missing — uv, Node.js (`npx`), bun, and Mosquitto — then seeds the four **git-tracked configs** and the bundled **skills** into their per-module folders, and builds the MCP tool catalog, so a first-time user gets the full tool set — local embeddings, external MCP servers, yt-dlp, browser-harness, the A2A mesh — with nothing to configure by hand. On WSL it uses Linux-native runtimes (Windows executables cannot receive custom env vars via WSL interop).

The **semantic embedding backend and model are deliberately not part of the install** — the backend is env-specific (CPU / CUDA / Metal) and the model download is ~2 GB, so it's a user-run step. Follow the **[Semantic Memory Search — Installation Guide](#semantic-memory-search--installation-guide)** below: install one backend, download the model weights, configure `HF_HUB_CACHE` / `BGE_M3_GGUF_PATH`, then verify the service is ready.

### Environment requirements

The installer is best-effort: it uses standard paths, tries several install routes per runtime, **warns and continues** when one is unavailable (only that runtime's features are affected), and never silently swaps in an old or alternate version. Pass `--core` (or set `SLIFE_CORE=1`) to skip the optional CLI tools for a light core-only install.

| Runtime | Where / how it's installed | Why it's needed |
|---|---|---|
| uv | official installer → `~/.local/bin`; Python 3.13 managed by uv | builds and runs slife |
| Node.js (`npx`) | package manager (apt / brew / dnf / pacman / winget) → cluster `module load nodejs` → official LTS tarball → `~/.local` (rootless fallback) | npx-based MCP servers: `file-search`, `serper`, `tavily-mcp`, `github`, `amap-maps`, `filesystem` |
| bun | `~/.bun/bin` | `nvidia-nim` MCP server |
| Mosquitto | package manager (winget / apt / brew / dnf / pacman) | A2A MQTT mesh — best-effort auto-install; A2A stays disabled until a broker runs |
| `cloudflared` | winget / Homebrew → official release binary → `~/.local/bin` (rootless, Linux) | `sharefile`'s `cloudflare` tunnel provider — best-effort auto-install; that provider stays unavailable without it (skip with `SLIFE_SKIP_CLOUDFLARED=1`) |
| `ssh` | ships with the OS (Windows: the optional *OpenSSH Client* capability) | `sharefile`'s `localhost.run` tunnel provider — **detection only**; enabling the Windows capability needs administrator |
| `unzip` (Linux) | package manager | bun installer dependency |

**Installed by default:** `yt-dlp` and `browser-harness` (both skipped by `--core`), Mosquitto and `cloudflared` (always attempted), the **four configs** (`slife.json5`, `mcp-plugin.json5` and `sharefile.json5` → `~/.slife/`, `local_embed.json5` → `~/.local-embed/`) seeded from bundled defaults, and the bundled skills (`~/.slife/skills/`) plus sample jobs (`~/.slife/jobs/`).

If a runtime can't be installed, the installer **warns and continues** — slife itself still installs; only the features needing that runtime are unavailable. For example, on a Linux box older than **glibc 2.28 / libstdc++ 3.4.29**, the Node rootless tarball fallback won't run (the installer reports the missing `GLIBC_2.28` / `GLIBCXX_3.4.xx` symbols). The supported route is **not** an older Node — it's a Node built for your distro (e.g. `module load nodejs` on HPC clusters, or your distro's package). Install that, then re-run this installer — it detects an existing `npx` and skips its own Node install.

If you edit `mcp-plugin.json5` by hand, the changes apply at the next wrapper start — the tool catalog is rebuilt live from connections, so no offline rebuild step exists (`local-embed` is on PATH after install). Every optional step is **fail-open**: an error warns and continues, leaving a working core.

### macOS / Linux / WSL

```bash
# Global
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
# China mainland
curl -fsSL https://gitee.com/juzcn/slife/raw/main/install.sh | bash
```

### Windows PowerShell

```powershell
# Global
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
# China mainland
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/install.ps1 | iex"
```

### Try without installing

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

### Update

Re-run the install script to upgrade slife — it rebuilds from the latest `main` and preserves what you've customized:

- **Optional packages** (e.g. `sentence-transformers`, `llama-cpp-python`) are captured from the previous tool venv and re-added after the fresh install, diffed against the new base so nothing is duplicated.
- **Configs, skills, and sample jobs** already present are never touched, and the installer **never prompts**. Missing ones are seeded in place; identical ones pass silently; when a bundled default has changed, the new default is seeded into `~/.slife/` as a **versioned reference copy** — `<name>.<version>.<ext>` for configs and jobs (`slife.0.9.7.json5`, `total_tokens.0.9.7.py`), `<name>.<version>/` for skills — each written copy announced as `seeded <file> → <folder>` (apply one with a single `cp` / `Copy-Item`). Reinstalls refresh the same-version copy; older versions remain for reference. A skill counts as different when **any** file inside the folder differs.

### Uninstall

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/uninstall.sh | bash
# China mainland
curl -fsSL https://gitee.com/juzcn/slife/raw/main/uninstall.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"
# China mainland
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/uninstall.ps1 | iex"
```

The uninstaller removes the `slife` and `credstore` tool commands (they share one venv) plus their `~/.local/bin` wrappers. User data (`~/.slife/`, `~/.credstore/`) is **not removed** — delete manually for a full reset.

### Related tools

The repo also ships three standalone packages — install each independently (the MCP gateway ships **inside** slife as an internal plugin; `local-embed` is a standalone daemon you start manually, like Mosquitto — slife never spawns it):

| Package | Install | Purpose |
|---------|---------|---------|
| `slife` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh \| bash` | The agent (this README) |
| `credstore` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh \| bash` | Cross-platform credential storage |
| `cc-switch` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh \| bash` | Generate `~/.claude/settings.json` |
| `local-embed` | installed with slife (standalone daemon) | Local embedding endpoint service |

Installing slife depends on [credstore](credstore/README.md) — it does **not** install cc-switch. See the [cc-switch](cc-switch/README.md), [credstore](credstore/README.md), and [local-embed](local-embed/README.md) READMEs for details. `slife`, `credstore`, and `cc-switch` each have a one-click installer (macOS / Linux / WSL: `install.sh`, Windows: `install.ps1`) and uninstaller kept in their package directories; `local-embed` ships as a slife dependency exposing the `local-embed` CLI.

## Configuration

**Secrets in the credential store, config in JSON5:**

| Layer | Storage | Contents |
|-------|---------|----------|
| **Secrets** | credential store (credstore) | API keys — encrypted at OS level, plus an encrypted cryptfile backup |
| **Config** | `~/.slife/slife.json5` | `${VAR}` references + non-secret values |

### Secrets & API keys

Secrets never live in the config file. Store them with `credstore set <KEY>`; the config references them as `${VAR}` and Slife resolves them at runtime — resolution order is **shell env → credstore → literal default** (`${VAR:-default}` fallbacks supported; secrets can also be referenced as `keyring:service/key` URIs).

```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → resolved from credstore at runtime
}
```

**Slife never prompts and does not read credstore's cryptfile backup.** It reads the system keyring only, then falls back to `os.environ`. If no system keyring is available (e.g. Linux where the kernel keyring is blocked by seccomp/policy on an HPC login node), use one of three methods:

1. **Environment variables only** — export the secrets in your shell (`export DEEPSEEK_API_KEY="sk-…"`); `os.environ` is checked before credstore, so exported keys work normally.
2. **Keep managing in credstore cryptfile mode, but inject to env** — store credentials as usual, then push them into the environment so Slife sees them: `credstore inject DEEPSEEK_API_KEY BAILIAN_API_KEY` (prompts for the master password in cryptfile-only mode), then restart the shell or `eval "$(credstore inject DEEPSEEK_API_KEY)"`.
3. **Plaintext in the config file** (tolerated, not recommended) — a literal `api_key` in `slife.json5` works, but the secret sits in plaintext on disk (`~/.slife/slife.json5`, chmod 0600).

`credstore` itself works fully in cryptfile-only mode (`set-password`, `set`, `get -p`, `inject`, `status` — see [credstore/README.md](credstore/README.md)).

### Model providers

Models are configured once in `slife.json5`, then switched at runtime from the chat (no file editing):

```json5
models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      api: "openai-completions",
      models: [{ model: "deepseek-flash", name: "DeepSeek Flash", reasoning: true }],
    },
  },
},
active_model: "deepseek/deepseek-flash",
job_coding_model: "bailian_personal/qwen3.6-flash",
```

The `active_model` is a `"provider/model"` ref — the model Slife chats with. `job_coding_model` is the LLM used by deterministic **jobs** — name a different (usually smaller/faster) model than `active_model`, so a nested one-shot job call never churns the agent loop's prompt cache (absent → the active model; per-call override: `llm.chat(model=...)`).

**Three first-class API backends:**

| `api` field | Backend | Providers |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama / MiniMax | Chat Completions |
| `anthropic-messages` | Claude / Bailian (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

**Per-model `compat` overrides** (in the model entry, or via `model_set`), for gateways that don't follow the standard thinking shape:

```json5
models: {
  providers: {
    bailian: {
      api: "anthropic-messages",
      models: [{
        model: "qwen3.8-max", name: "Qwen3.8 Max",
        reasoning: true,
        compat: { thinkingFormat: "openai" },  // anthropic backend: model always thinks, no thinking param
      }],
    },
    scnet: {
      api: "openai-completions",
      models: [{
        model: "MiniMax-M3", name: "MiniMax M3",
        reasoning: true,
        compat: { thinking: "omit" },         // openai backend: send NO thinking field (gateway 400s on enabled)
      }],
    },
  },
},
```

`compat.thinking` on the OpenAI backend: `"omit"` sends no thinking field (for gateways that reject the `{"type": "enabled"}` shape but reason natively), `"disabled"` forces explicit off, `"enabled"` matches the default.

**Runtime switching:** `model_list` → `model_switch(ref="bailian/qwen3.8-max")` in natural language, or the `Ctrl+S` inline picker as an emergency escape when the active model is down. `model_set` upserts (merges — a partial update keeps the model's `reasoning`/`input`/`compat` fields) and accepts a `compat` dict, so per-model overrides need no hand-editing.

**Secrets never reach the LLM.** User input, tool-call arguments, and every tool result pass through a pattern-based sanitizer before entering the context — API key shapes (`sk-*`, `ghp_*`, Bearer tokens, …) are auto-masked.

## Features

### Tools

All unified as OpenAI function definitions — the LLM sees no difference between native, built-in plugin, and external MCP tools. Every tool additionally accepts three meta-parameters: `_timeout` (per-call override), `_async` (run in background, poll with `check_async`), and `_approve` (inline approval prompt — Y approve / N, Esc deny).

**57 native tools in 12 categories** (58 classes auto-discovered from `slife/tools/`; `install_python_package` ships disabled in the bundled config). The reserved harness tool `_turn_prompt` is auto-invoked once per turn, `attach_image` is auto-invoked on `@`-attachments — the model reads their results but is told not to call them. `attach_image` refuses at call time on a vision-less model (it is never hidden).

| Category | Tools |
|----------|-------|
| System | `system_health`, `list_native_tools`, `check_async`, `cancel_async`, `clear_context`, `set_max_iterations`, `notify_user`, `wait_minutes` (pause the turn and resume automatically), `add_user_pref` (record a preference in `USER.md`) |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` (disabled by default) |
| Schedule | `scheduled_task_set`, `scheduled_task_remove`, `scheduled_task_list`, `scheduled_run_list`, `scheduled_run_skip`, `run_schedule_now` |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image` (feed images to a vision model), `_turn_prompt` (per-turn prompt, auto-invoked) |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| embeddings | `embeddings_model_list`, `embeddings_model_set`, `embeddings_model_switch`, `embeddings_model_remove`, `embeddings_enable` |
| mcp | `mcp_tool_load` |

**Managed categories** (Skills / CLI / REST API / Models / MCP) support `X_list` / `X_set` / `X_remove` (+ `X_set_enabled` where a toggle applies) — all `X_set` tools are idempotent upserts; `model_set` **merges** into the existing entry, so a field-focused change can't silently strip a model's `reasoning`/`input`/`compat`.

**Plugin tools** — built-in plugins register under bare names (`[<server>]` description prefix); external MCP servers appear as `{server}__{tool}`:

| Server | Tools |
|--------|-------|
| `mcp-gateway` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`, `mcp_tool_search` |
| `memdb` | `turn_list`, `turn_search`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_save`, `diary_write`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `list_files`, `cabinet_search`, `cabinet_read`, `report_save`, `report_list`, `report_read` |
| `sharefile` | `share_file`, `sharefile_unshare` |
| `a2a` | `a2a_send_task`, `a2a_send_task_async`, `a2a_send_message`, `a2a_send_message_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| `job-coding` | `job-list`, `job-write`, `job-remove`, `job-run` + one tool per registered job (e.g. `translate`) |

**External MCP servers are loaded on demand.** Third-party capability enters only as a standard MCP server in `mcp-plugin.json5` (any stdio / SSE / Streamable HTTP server works — no Slife SDK required). The LLM discovers a tool with `mcp_tool_search` (a hybrid keyword/semantic search over the gateway's live tool catalog) and loads it with `mcp_tool_load(full_name)` — a server with `auto_load: true` keeps the older wholesale registration. Enable/disable is server-granular (`mcp_set_enabled`). The catalog is rebuilt live from connections on every (re)connect, so it always reflects exactly what the runtime can use — no offline rebuild step exists.

**Windows execution.** `execute_shell` runs in the detected shell — PowerShell or cmd (the same value the system prompt reports, so the LLM's syntax actually executes) — and its output is decoded with the system code page (GBK/cp936 on Chinese Windows). `run_python_script` forces the child Python to UTF-8 (`-X utf8`) so non-ASCII output can't crash the child.

### Memory — always on

Every turn is permanently recorded in SQLite (`~/.slife/<agent>.db`) and searched four ways:

**Memory is a core feature — the agent never runs silently without it.** If the memory DB is broken (missing column, corruption, disk error), the agent fails loudly instead of pretending: a session that can't restore aborts at startup with the error; a turn that can't be saved freezes the inbox and shows a red banner — new turns stop until the DB is fixed and the agent is restarted.

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vector → RRF merge) |
| `time` | Browse by date |

Embeddings are a **first-class top-level `embeddings` section** in `slife.json5` (shared by `memdb` + `memfiles`), managed by the native `embeddings_*` tools; runtime index status is surfaced by `system_health`. Each provider is an **OpenAI-compatible endpoint** (`base_url` + `api_key`); `active_model` ("provider" — e.g. `"local_embed"` or `"siliconflow"`) is configuration-authoritative. The **`local-embed` daemon** (started manually, like Mosquitto — not a slife plugin) serves local GGUF/transformer models at `http://127.0.0.1:17347/v1`, each loaded **once** and shared by `memdb` and `memfiles` — no double load — with no "active model" of its own (the client requests the model it wants, so a model is never loaded twice). **Keyword search works without any embedding backend.** Semantic (hybrid) results are only served once the index is fully built for the current model — while a reindex runs, hybrid degrades to keyword-only and resumes automatically when indexing finishes.

Each turn records two timestamps — your input time (`created_at`, the Enter-press moment) and the assistant's completion time (`completed_at`) — shown as dim `[HH:MM]` markers. User messages carry a compact **`[INFO: {"turn_id": N, "begin": …, "end": …}]`** footnote (the turn id plus when it happened) so the agent can reference turns by id (`turn_read` / `turn_summarize`) — and you read the same line in the TUI.

Every turn also preserves its **source channel** — `human`, `wechat`, a subagent, the heartbeat, an A2A peer, or `system` (Slife itself) — so session restore renders each bubble with the right prefix: `You>`, `Wechat>`, `Subagent(<name>)>`, `Heartbeat>`, `A2A(<agent>)`. An incoming WeChat message additionally reaches the model prefixed **`[Wechat:{...}]`** — the JSON carries `peer_wechat_id` / `context_token`, what `wechat_send_message` needs to reply.

### Autonomous heartbeat

While idle, the agent gets a periodic autonomous window (every `agent.heartbeat_interval` seconds; the code default is 60, the shipped template sets 1800). It runs as a normal turn (own turn, saved to memory); the reply contract is real content if it has something worth saying, otherwise a single `.`. A bare `.` reply is **silence** — never rendered in the chat or session restore, from any event (heartbeat, A2A async-completion notification, etc.); the `[Heartbeat]` trigger is filtered, and a real autonomous reply renders as `⚡ 自主`. This is the precondition for emergent self-initiated behavior.

### Scheduled tasks

Ask the agent to do something on a schedule — "write a diary entry every night at midnight", "summarize the week every Friday" — and it registers a cron-scheduled task (`scheduled_task_set`). The task name is also its worker's name, so it should be a short ASCII identifier. When a task fires, the agent dispatches the work to a subagent worker named after the task (`run_schedule_now`) rather than doing it inline, and the worker saves the result as a **report** in the file cabinet (`report_save`) and notifies you when done. Every fire is recorded (`scheduled_run_list`), so you can see what ran and what it produced (`report_list` / `report_read`). A task's **description is required** at creation — it is the worker's instruction, so an empty task cannot exist. `run_schedule_now` accepts `clone_context=True` to give the worker a clone of the current conversation when the task depends on what you've been discussing.

Tasks fire **only while Slife is running**. At the next start a one-shot sweep settles what a previous session left behind in `scheduled_run_list`: runs that never finished become **failed**, and fires that were due while Slife was closed are marked **missed**. Nothing is announced and nothing waits for your input — a failed or missed run can still be backfilled with `run_schedule_now` (firing immediately) or closed with `scheduled_run_skip`.

### Jobs — deterministic, code-defined

For work that is well-specified and repeatable — translate, summarize, extract, classify, format — a **Job** runs one code-defined function with exactly the arguments it declares, instead of dragging a whole conversation into an agent turn. Jobs are plain `.py` files in `~/.slife/jobs/` (one public function = one job tool; see the `job-coding` skill for the conventions and the bundled `translate` / `summarize` samples). The plugin reloads them at every start and manages them live:

- `job-list` — see the registered jobs
- `job-write` / `job-remove` — add/change (create or replace; a broken write rolls back) or delete a job; its tool appears/disappears immediately and survives restarts
- `job-run` — run any job by name, or call the job's own tool directly

A job that needs the LLM calls it **once** via the `llm` handle it imports itself (`from slife.plugins.job_coding import llm` — nothing is auto-injected): a narrow, explicit `llm.chat` on `job_coding_model`, a model configured independently of the conversation's active model so jobs stay cheap and never disturb the agent's prompt cache. No conversation history, system prompt, or agent loop ever reaches a job.

A job can also drive **any external MCP server** configured in `mcp-plugin.json5` through the `mcp` handle (`from slife.plugins.job_coding import mcp`) — bare MCP, one tool call per statement: `await mcp.call(server, tool, args)`. The call rides the mcp-gateway's persistent connections, so no external server is ever spawned a second time, and it reaches **tools the main agent hasn't loaded**. `mcp.call` never raises: an unreachable gateway, a disconnected or disabled server, or an unknown tool returns a clear `Error: ...` string the job can branch on.

### Images & vision

Attach images with `@path` / `@url` syntax (quotes supported for paths with spaces) to feed them to a vision-capable model:

```
Check this screenshot @D:\Downloads\error.png
```

Vision-capable models receive local files as base64 data URIs and HTTP(S) URLs as-is; the `attach_image` tool lets the agent attach images mid-turn (local sources are capped at 20 MB so a stray path never base64s a huge file into the context). Nothing is ever rendered in the terminal — files open with the OS default app, and `share_file` publishes any local file as a public HTTPS link via a pluggable tunnel provider (ngrok / localhost.run / Cloudflare Quick Tunnel, chosen by `sharefile.json5`; `share_file` returns a graceful error while the tunnel is offline).

### Plugins

Eight internal plugins run as independent child processes, each declared by one row in the central plugin spec and driven by the same uniform lifecycle (spawn → MCP-handshake readiness → watchdog → health). One of them — **mcp-gateway** — is the gateway to external MCP servers: third-party capability enters only as a standard MCP server in `mcp-plugin.json5`, never as a Python plugin.

| Plugin | Role |
|--------|------|
| **mcp-gateway** | The MCP gateway — proxies external MCP servers (stdio / SSE / Streamable HTTP) and keeps a live in-memory tool catalog. Management: `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`, `mcp_tool_search`; tools load on demand via `mcp_tool_load` |
| **memdb** | Turns database with hybrid search |
| **wechat** | Bidirectional WeChat messaging |
| **memfiles** | Notes / diary / files / reports cabinet (private). Notes, diary & reports dual-written to markdown + a SQLite hybrid index. All save tools return local paths — never auto-publish |
| **sharefile** | Public file sharing — `share_file` publishes a local file as a public HTTPS URL (`/share` route on the same port; pluggable tunnel from `sharefile.json5`) |
| **a2a** | A2A mesh channel over MQTT (only starts when the broker is reachable) |
| **media** | Non-chat AI generation (image, video, TTS, ASR) from any provider — owns the `media:` config section and a provider-agnostic adapter layer. Tools: `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| **job-coding** | Deterministic jobs as MCP tools — code-defined functions in `~/.slife/jobs/` run with exactly their declared args; one-shot LLM calls via `llm.chat` on `job_coding_model`. Tools: `job-list`, `job-write`, `job-remove`, `job-run` + one per job |

All internal plugins run with a **watchdog** that auto-restarts them on crash (exponential backoff 1s→30s, max 5 consecutive failures) and recovers the restart counter only after a plugin stays up ~60 s. Readiness follows the MCP standard (the `initialize` handshake completes only after the plugin's own init succeeded); **required plugins** (`plugins.required` — `memdb` and `memfiles` in the bundled config) are core: failing to become ready **aborts startup** instead of limping on. External/subordinate dependencies — external MCP servers, the tunnel, WeChat login, media providers, the A2A broker, the `local-embed` daemon — never gate readiness; they are uncontrollable, self-heal at runtime, and are surfaced separately via status tools in `system_health`.

### A2A — agent-to-agent mesh

The A2A protocol runs over a pluggable transport **binding** (currently MQTT) so multiple agents — on the same machine or different ones — discover each other, send tasks and messages, and share results:

- **Mesh tools** (one uniform `a2a_` prefix): `a2a_send_task`, `a2a_send_task_async`, `a2a_send_message`, `a2a_send_message_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast`. Inbound peer messages/tasks reach the model as `[A2A:{"agent_name": …, "task_id": …}]` (task id only for tasks — a stateless message carries just the peer) and auto-pushed async results as `[A2A-PUSH:…]`; the TUI shows `A2A(<peer>)>`. The `a2a` plugin only starts when the MQTT broker is reachable.
- **Subagents are local workers, not A2A peers**: `spawn_subagent` / `subagent_send_task` / `subagent_get_task_result` / … create child-process workers that share your plugins and run one task at a time (a sync send to a busy worker is auto-queued as async). Async results auto-push to your chat (`mode="auto"`, default) or stay pollable-only (`mode="poll"`). Subagents never drain your inbox — all replies and management belong to the main agent.

All messages — human, WeChat, MQTT, subagent results — flow through a single inbox queue and are processed one turn at a time.

## Semantic Memory Search — Installation Guide

Semantic (hybrid) memory search — recall by meaning across `memdb` turns and `memfiles` notes — needs **two things** the one-click installer deliberately does not bring: a local embedding **backend** (a Python package, platform-specific) and the **model weights** (downloaded by you — the server never auto-downloads). Keyword search (`grep` / `fts5` / `time`) works without any of this. Setup is a **user-run** step; every piece is fail-open, so a missing backend leaves a working keyword-only core.

**How it fits together.** Slife treats every embedding provider as an OpenAI-compatible endpoint (`base_url` + `api_key`). The `local-embed` daemon — a standalone service you start manually (like Mosquitto), exposing the `local-embed` CLI — loads each local model **once** and serves it at `http://127.0.0.1:17347/v1` (`POST /v1/embeddings`, `GET /v1/models`, `GET /health`). `memdb` and `memfiles` both call that endpoint, so a model is never loaded twice. local-embed has **no "active model"** — every request names the model it wants; slife's `embeddings.active_model` (e.g. `"local_embed"` for the local daemon, `"siliconflow"` for a cloud provider — the seeded config ships `"siliconflow"`) chooses which provider to embed against.

### 1. Install the backend dependency

Install the backend **into the slife tool venv** — the same interpreter `local-embed` runs under. Reference the venv by its **root directory**, `"$(uv tool dir)/slife"` — this works on **macOS, Linux and Windows alike** (uv locates the interpreter inside the venv itself, so you never need to know whether it's `bin/` or `Scripts/`):

| Backend | Command |
|---------|---------|
| **Transformer** — `sentence-transformers`, simplest, works everywhere | `uv pip install --python "$(uv tool dir)/slife" sentence-transformers` |
| **GGUF · CPU (Linux / WSL / macOS)** | `uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34` |
| **GGUF · NVIDIA CUDA** | `CMAKE_ARGS="-DGGML_CUDA=on" uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34` |
| **GGUF · macOS Metal** | `CMAKE_ARGS="-DGGML_METAL=on" uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34` |
| **GGUF · Windows CPU** | `uv pip install --python "$(uv tool dir)/slife" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.34` |

- llama-cpp-python ships **no PyPI wheel** (only the sdist), so the Linux / WSL / macOS rows **compile from source** — the standard build — and need a **C compiler + CMake ≥ 3.21** (macOS: Xcode CLT clang; Linux: `build-essential` + `cmake`). The GPU rows pass `CMAKE_ARGS` to select the backend. **Windows has no default C toolchain**, so it uses the upstream prebuilt CPU wheel — the one workaround.
- The two backends can coexist — install both in **one** `uv pip install` (e.g. `sentence-transformers` plus the `llama-cpp-python` CPU row in a single command). Installing twice replaces the first install.
- If the backend is missing, `local-embed` logs the exact install command for your platform instead of failing silently.

### 2. Download the model weights

Offline by default — `HF_HUB_OFFLINE=1`, **no auto-download**. Make the weights available yourself via one of two routes. The `hf` CLI is not shipped by the backends — install it once: `uv tool install "huggingface-hub[cli]"` (or prefix any `hf` command with `uvx --from huggingface-hub`).

**Transformer route (default config, ~2 GB).** The seeded model is `BAAI/bge-m3`; download it into the HF cache and no config change is needed:

```bash
hf download BAAI/bge-m3                                    # → ~/.cache/huggingface/hub
HF_ENDPOINT=https://hf-mirror.com hf download BAAI/bge-m3  # mainland-China mirror
```

**GGUF route (small offline file, ~100 MB).** Use any quantized BGE-M3 GGUF you trust — these are community conversions with no single authoritative source (prefer a high-fidelity `Q8_0`). Get it from any source (HF single-file pull, browser, `wget`/`curl`), then place it at the default path and point the client at the `bge-m3` model:

```bash
hf download <owner>/<repo> <model>.gguf --local-dir ~/.local-embed/models   # HF single-file pull
mv ~/.local-embed/models/<model>.gguf ~/.local-embed/models/bge-m3-q4_k_m.gguf     # the expected default path
```

Every model in the `models` map is served as a **peer** — there is no `active_model`; the client names the model on every request (a stale `active_model` key in an existing config is ignored).

### 3. Configure the HF cache & GGUF path

Everything — host, port, models, backend — lives in **`local_embed.json5`**, seeded by the installer (path resolution: `$LOCAL_EMBED_FILE` > slife project root (dev) > `~/.local-embed/local_embed.json5`). Values support `${VAR}` / `${VAR:-default}` expansion, and **a shell env var wins over the config**. The seeded file already carries portable placeholders — usually you only set env vars or edit two lines:

```json5
{
  env: {
    HF_HUB_CACHE: "${HF_HUB_CACHE:-~/.cache/huggingface/hub}",   // where transformer repos resolve
    HF_HUB_OFFLINE: "${HF_HUB_OFFLINE:-1}",          // 1 = never auto-download; 0 = allow on-demand
  },
  models: {
    "BAAI/bge-m3": { backend: "transformer", model: "BAAI/bge-m3" },
    "bge-m3": { backend: "gguf", gguf_path: "${BGE_M3_GGUF_PATH:-~/.local-embed/models/bge-m3-q4_k_m.gguf}" },
  },
  port: 17347,
}
```

| Setting | Meaning |
|---------|---------|
| `env.HF_HUB_CACHE` / `HF_HUB_CACHE` | Where the transformer route resolves HF repo ids. Default `~/.cache/huggingface/hub`. If your model was downloaded into a different cache, point this at it — otherwise the repo is silently re-fetched. |
| `env.HF_HUB_OFFLINE` / `HF_HUB_OFFLINE` | `"1"` (default) — offline; the model must already be in the cache / on disk. `"0"` — allow the model loader to reach the network (no managed download / mirror fallback). |
| `models."bge-m3".gguf_path` / `BGE_M3_GGUF_PATH` | The `.gguf` file for the GGUF route. `~` is expanded; `BGE_M3_GGUF_PATH` in the shell overrides the config default. |

Requests name the model they want (slife's provider `model` id — `"BAAI/bge-m3"` for the transformer route, `"bge-m3"` for the GGUF route). Changes apply on the next start of the local-embed service (restart slife).

**CLI alternative** — `local-embed` (on PATH after install) upserts a model config and pins the port (idempotent, leaves other models untouched):

```bash
local-embed set BAAI/bge-m3 --HF_HUB_CACHE ~/.cache/huggingface/hub
local-embed set-gguf bge-m3 --path ~/.local-embed/models/bge-m3-q4_k_m.gguf
```

### 4. Make the service ready — verify

Start the `local-embed` daemon (`local-embed` on PATH — start it manually, like Mosquitto), then start slife. The **model load is deferred** — the first embed loads it (a few seconds for GGUF, up to a minute for the ~2 GB transformer). Verify from inside the chat or over HTTP:

- **In the chat** — ask the agent to run `system_health` (the `memdb`/`memfiles` components report the semantic gate: `semantic_ready`, model, pending embeddings).
- **Over HTTP** (the service is standalone at the fixed port):

```bash
curl http://127.0.0.1:17347/health            # {status, backend, model, dimension, loaded}
curl http://127.0.0.1:17347/v1/models         # every configured model + the active flag
curl http://127.0.0.1:17347/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model": "bge-m3", "input": ["hello world"]}'   # returns a real vector
```

A healthy state: `/health` → `status: ok`; `system_health` → `local_embed` component probes the active embedding endpoint (reachable + model list = `ok` — whether that endpoint is the local daemon or a cloud provider like SiliconFlow), and the `memdb`/`memfiles` components show `semantic_ready`. When the service is unreachable (backend missing, weights missing, still loading), slife **degrades gracefully to keyword search** — `system_health` reports the reason, and once the index is fully built for the current model, hybrid results resume automatically.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Log: `backend_unavailable … reason=llama_cpp_not_installed` / `sentence_transformers_not_installed` | Run the step-1 install for your platform — the log prints the exact command. |
| Transformer route won't load with `HF_HUB_OFFLINE=1` | The repo isn't in the cache — run `hf download BAAI/bge-m3` and make sure `HF_HUB_CACHE` points at the cache that holds it. |
| GGUF route won't load | File missing at `gguf_path` — check `BGE_M3_GGUF_PATH` / `gguf_path`, and that the client requests `"bge-m3"` (all configured models are peers — nothing is gated behind an `active_model`). |
| `system_health` shows `local_embed` as `unavailable` ("Active embedding endpoint unreachable") | The active embedding endpoint didn't answer `GET /v1/models` — start the `local-embed` daemon (or fix the cloud provider's key: `api_key` resolves `${VAR}` → env → credstore). The component only ever probes the **active** provider. |
| First embed very slow | A transformer download/warm-up is deferred to the first embed; subsequent calls are fast. |

### Optional extras (manual installs)

The embedding backends above are that same "optional extra" — for direct manual installs (uvx / git checkout) or to re-add a backend into the slife venv:

| Extra | Enables |
|-------|---------|
| `local-embed[gguf]` | Local GGUF embeddings via llama-cpp-python (offline, ~300 MB) |
| `local-embed[transformer]` | HuggingFace transformer embeddings via sentence-transformers (~2 GB) |
| `slife[gguf]` / `slife[transformer]` / `slife[embeddings]` | Legacy in-process embeddings (not used by default) |

```bash
uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34    # slife[gguf]  (Linux/macOS builds from source)
uv pip install --python "$(uv tool dir)/slife" sentence-transformers        # slife[transformer]
# Windows — pre-built wheels (no C++ compiler needed). See the step-1 table for wheel selection.
```

## Usage Reference

### Keyboard shortcuts

Key caps (`Ctrl+C`, `Esc`, …) are universal; the action words after them localize with the interface language.

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Esc` | Cancel agent loop |
| `Ctrl+S` | Switch model (inline picker — type a number, Esc cancels) |
| `Home` / `End` | Scroll to top / bottom |
| `Ctrl+Y` | Copy result (on a tool call) |
| `Enter` / `Space` | Toggle thinking block (on an assistant message) |
| `↑` / `↓` | Input history navigation |
| `Shift+Enter` | Insert newline in input |

### CLI

| Flag | Description |
|------|-------------|
| `--agent <id>` | Agent identity — separate turns database + A2A mesh name (default: `slife`) |
| `--lang <en\|zh>` | TUI language — force English / Chinese (default: auto-detect from OS locale) |
| `<config-path>` | Positional — use a specific config file (its parent dir becomes the data dir) |

### Health & logs

* **`system_health`** (or the standalone `check_memdb`, `check_wechat`, `check_memfiles`, `check_sharefile`, `check_local_embed`, `check_mcp_gateway [server]`, `check_a2a`, `check_media`, `check_job_coding`, `check_watchdog`) reports live status for every subsystem — ask the agent to run it any time something seems off.
* **Logs** live in `~/.slife/logs/` (one per session, `event_name key=value` lines, DEBUG+; plugins inherit the session id). The terminal is reserved for the TUI — nothing prints to it but the chat.

## Development

Slife is one codebase, a few docs, split by audience:

* **[DESIGN.md](DESIGN.md)** — architecture & implementation for people working on the code: the agent loop, context engineering, tool system, plugin architecture, MCP gateway, memory, A2A.
* **[PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md)** — the authoritative spec of the plugin system (central `PluginSpec` table, the registry, the uniform lifecycle) for anyone writing a plugin.
* **[CONTEXT_HARNESSING.md](CONTEXT_HARNESSING.md)** — how Slife curates the model context each turn: channels, markers, the `_turn_prompt` harness tool-pair.

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync --all-extras

uv run credstore set-password
uv run credstore set DEEPSEEK_API_KEY
uv run slife

# Tests
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

Dev mode auto-detects when you run from the source tree: data files stay in the project directory. Production installs (uv tool / pipx / pip) always use `~/.slife/` — even when launched from inside a checkout or from the home directory. CI runs the test suite on Ubuntu, macOS, and Windows with Python 3.13 (tests run against the built wheels).

## License

MIT