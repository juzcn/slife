# Slife

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers every turn forever, and orchestrates other agents.

```
You: "Find all TODO comments and create GitHub issues"
  → LLM greps the codebase with execute_shell
  → LLM calls github__create_issue(...) for each hit
  → LLM: "Created 7 issues. All linked above."
```

One TUI window around an LLM tool loop: **61 builtin tools across 13 categories**, always-on memory with hybrid search, vision image attachments (`@path`/`@url`), runtime model switching across three API backends, and an agent-to-agent mesh. Nine built-in plugins extend it: memory, WeChat, a markdown file cabinet, public file sharing, the A2A mesh, image/video/speech generation, deterministic jobs, the MCP gateway, and local embeddings.

Requires Python 3.13+. Runs on Windows (native & WSL), macOS, and Linux.

**Bilingual interface.** The TUI follows your OS language — Chinese on a Chinese system, English everywhere else. Detected at startup from the OS itself (Windows `GetUserDefaultUILanguage` / *nix locale variables); all in-TUI text — system messages, the approval prompt, the model picker, tool-call labels, the status bar — renders in the right language. What the LLM sees (system prompt, tool schemas) stays uniformly English; so do logs.

> **Reader's map** — pick your lane:
>
> * **I just want to try it** → [Quick Start](#quick-start)
> * **I'm installing this properly** → [Install](#install)
> * **I need to wire up models / API keys** → [Configuration](#configuration)
> * **What can it actually do?** → [Features](#features)
> * **I want semantic (hybrid) memory search to work** → [Semantic Memory Search — Installation Guide](#semantic-memory-search--installation-guide)
> * **Day-to-day use** (keys, flags, health) → [Usage Reference](#usage-reference)
> * **I'm going to develop or debug Slife itself** → [Run from source](#development)
> * **I'm changing Slife's code** → [DESIGN.md](DESIGN.md) — subsystems, mechanisms, invariants

<a id="quick-start" name="quick-start"></a>
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

<a id="install" name="install"></a>
## Install

**Zero prerequisites, fully out-of-the-box.** The install script fetches the latest `main`, builds slife **from source** (workspace wheels — no PyPI, always the newest code), and installs it with uv into an isolated tool venv. It auto-installs what's missing — uv, Node.js (`npx`), bun, and Mosquitto — and puts the bundled configs, skills and sample jobs in place, so a first-time user gets the full tool set — local embeddings, external MCP servers, yt-dlp, browser-harness, the A2A mesh — with nothing to configure by hand. On WSL it uses Linux-native runtimes (Windows executables cannot receive custom env vars via WSL interop).

The **semantic embedding backend and model are deliberately not part of the install** — the backend is env-specific (CPU / CUDA / Metal) and the model download is ~2 GB, so it's a user-run step. Follow the **[Semantic Memory Search — Installation Guide](#semantic-memory-search--installation-guide)** below: install one backend, download the model weights, configure `HF_HUB_CACHE` / `BGE_M3_GGUF_PATH`, then verify the service is ready.

### Environment requirements

The installer is best-effort: it uses standard paths, tries several install routes per runtime, **warns and continues** when one is unavailable (only that runtime's features are affected), and never silently swaps in an old or alternate version. Pass `--core` (or set `SLIFE_CORE=1`) to skip the optional CLI tools for a light core-only install.

| Runtime | Where / how it's installed | Why it's needed |
|---|---|---|
| uv | official installer → `~/.local/bin`; Python 3.13 managed by uv | builds and runs slife |
| Node.js (`npx`) | package manager (apt / brew / dnf / pacman / winget) → cluster `module load nodejs` → official LTS tarball → `~/.local` (rootless fallback) | npx-based MCP servers: `file-search`, `serper`, `tavily-mcp`, `amap-maps`, `filesystem` |
| bun | `~/.bun/bin` | MCP servers distributed as bun packages; none of the bundled ones need it |
| Mosquitto | package manager (winget / apt / brew / dnf / pacman) | A2A MQTT mesh — best-effort auto-install; A2A stays disabled until a broker runs |
| `cloudflared` | winget / Homebrew → official release binary → `~/.local/bin` (rootless, Linux) | `sharefile`'s `cloudflare` tunnel provider — best-effort auto-install; that provider stays unavailable without it (skip with `SLIFE_SKIP_CLOUDFLARED=1`) |
| `ssh` | ships with the OS (Windows: the optional *OpenSSH Client* capability) | `sharefile`'s `localhost.run` tunnel provider — **detection only**; enabling the Windows capability needs administrator |
| `unzip` (Linux) | package manager | bun installer dependency |

**Installed by default:** `yt-dlp` and `browser-harness` (both skipped by `--core`), Mosquitto and `cloudflared` (attempted unless `--core`), the **four configs** (`slife.yaml`, `tools.yaml` and `sharefile.yaml` → `~/.slife/`, `local_embed.yaml` → `~/.local-embed/`) seeded from bundled defaults, and the bundled skills (`~/.slife/skills/`) plus sample jobs (`~/.slife/jobs/`).

If a runtime can't be installed, the installer **warns and continues** — slife itself still installs; only the features needing that runtime are unavailable. For example, on a Linux box older than **glibc 2.28 / libstdc++ 3.4.29**, the Node rootless tarball fallback won't run (the installer reports the missing `GLIBC_2.28` / `GLIBCXX_3.4.xx` symbols). The supported route is **not** an older Node — it's a Node built for your distro (e.g. `module load nodejs` on HPC clusters, or your distro's package). Install that, then re-run this installer — it detects an existing `npx` and skips its own Node install.

If you edit `tools.yaml` by hand, the changes apply at the next start. Every optional step is **fail-open**: an error warns and continues, leaving a working core.

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

### Tokenizer vocabulary (repair only)

Slife measures context size with `tiktoken` (the OpenAI BPE), whose 3.6 MB vocabulary downloads on first use. The installer already fetches it; this is how to repair it. That download has **no timeout**, so on a slow or proxy-throttled link it can hang instead of failing — fetching it up front keeps that out of the agent's first turn:

```bash
mkdir -p ~/.cache/tiktoken
curl -fL --retry 3 -o ~/.cache/tiktoken/fb374d419588a4632f3f557e76b4b70aebbca790 \
  https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken
```

The filename is the SHA-1 of that URL (tiktoken's cache key) and the file must be exactly **3613922** bytes. A file that is present but shorter is refused rather than used, and Slife reports it — delete it and it will be fetched again.

### Try without installing

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

<a id="development" name="development"></a>
### Run from source

Running from source, for development or debugging:

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync

uv run credstore set-password
uv run credstore set DEEPSEEK_API_KEY
uv run slife

# Tests
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

`uv sync` is **exact**: it prunes the checkout's `.venv` to the lock, and no embedding backend is in the lock (they are per-platform manual installs — see [Re-adding a backend (manual installs)](local-embed/README.md#re-adding-a-backend-manual-installs)). If you keep `llama-cpp-python` or `sentence-transformers` in a dev venv for real end-to-end runs, either pass `uv sync --inexact` or reinstall the backend after syncing.

From the source tree, data files stay in the project directory; every installed copy keeps using `~/.slife/`.

### Update

Re-run the install script to upgrade slife — it rebuilds from the latest `main` and preserves what you've customized.

One schema change needs a command. The turns database's table was renamed `diary` → `turn` (it collided with the file cabinet's own *diary*, the day journal), and nothing migrates automatically — a database from before the rename keeps its history in the old table, which the new code ignores. Carry it over once, from the source tree:

```bash
uv run python scripts/migrate_memdb_diary_to_turn.py --all
```

Use `--agent NAME` or `--db PATH` for a single database. The script backs the file up first, is safe to re-run, and refuses if both tables hold turns rather than guessing which is authoritative.

Everything else is preserved automatically:

- **Optional packages** (e.g. `sentence-transformers`, `llama-cpp-python`) are captured from the previous tool venv and re-added after the fresh install, diffed against the new base so nothing is duplicated.
- **Configs, skills, and sample jobs** already present are never touched, and the installer **never prompts**. Missing ones are seeded in place; identical ones pass silently; when a bundled default has changed, the new default is seeded into `~/.slife/` as a **versioned reference copy** — `<name>.<version>.<ext>` for configs and jobs (`slife.0.10.0.yaml`, `total_tokens.0.10.0.py`), `<name>.<version>/` for skills — each written copy announced as `seeded <file> → <folder>`, so you apply it with a single `cp` / `Copy-Item` when you want it. Reinstalls refresh the same-version copy; older versions remain for reference. A skill counts as different only when a file the **bundled default ships** differs, so your own additions to a skill never trigger a reference copy.

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

The uninstaller removes the `slife` and `credstore` tool commands (they share one venv), plus a standalone `local-embed` tool if one is installed — and their `~/.local/bin` wrappers. User data (`~/.slife/`, `~/.credstore/`, `~/.local-embed/` — config and model weights) is **not removed** — delete manually for a full reset.

### Related tools

Besides slife itself, the repo ships three standalone packages, each with its own one-click installer:

| Package | Install | Purpose |
|---------|---------|---------|
| `slife` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh \| bash` | The agent (this README) |
| `credstore` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh \| bash` | Cross-platform credential storage |
| `cc-switch` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh \| bash` | Generate `~/.claude/settings.json` |
| `local-embed` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/local-embed/install.sh \| bash` | Local embedding endpoint service |

Installing slife depends on [credstore](credstore/README.md) — it does **not** install cc-switch. See the [cc-switch](cc-switch/README.md), [credstore](credstore/README.md), and [local-embed](local-embed/README.md) READMEs for details.

Two of these also ship **inside** slife: the MCP gateway is an internal plugin, and `local-embed` is one too — Slife starts it for you, or uses the instance you started yourself rather than starting a second one.

<a id="configuration" name="configuration"></a>
## Configuration

**Secrets in the credential store, config in YAML:**

| Layer | Storage | Contents |
|-------|---------|----------|
| **Secrets** | credential store (credstore) | API keys — encrypted at OS level, plus an encrypted cryptfile backup |
| **Config** | `~/.slife/slife.yaml` | `${VAR}` references + non-secret values |
| **Tool config** | `~/.slife/tools.yaml` | Tool definitions by category — `builtin` / `plugin` / `mcp` / `rest-api` / `job` / `cli` / `skill` (see the plugin table below) |

### Secrets & API keys

Secrets never live in the config file. Store them with `credstore set <KEY>`; the config references them as `${VAR}` and Slife resolves them in this order: **shell environment → credential store → literal default** (`${VAR:-default}` fallbacks supported).

```yaml
env:
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}"   # → resolved from credstore at runtime
```

An `api_key` field (models, embeddings) additionally accepts a `keyring:service/key` URI. The `env:` section does not — its values are resolved by the shadowing rule above only.

**Slife never prompts and does not read credstore's cryptfile backup.** It reads the system keyring only, falling back to `os.environ` when the keyring has nothing. If no system keyring is available (e.g. Linux where the kernel keyring is blocked by seccomp/policy on an HPC login node), use one of three methods:

1. **Environment variables only** — export the secrets in your shell (`export DEEPSEEK_API_KEY="sk-…"`). `os.environ` is consulted before credstore, so exported keys win.
2. **Keep managing in credstore cryptfile mode, but inject to env** — store credentials as usual, then push them into the environment so Slife sees them: `credstore inject DEEPSEEK_API_KEY BAILIAN_API_KEY` (prompts for the master password in cryptfile-only mode), then restart the shell or `eval "$(credstore inject DEEPSEEK_API_KEY)"`.
3. **Plaintext in the config file** (tolerated, not recommended) — a literal `api_key` in `slife.yaml` works, but the secret sits in plaintext on disk (`~/.slife/slife.yaml`, chmod 0600).

`credstore` itself works fully in cryptfile-only mode (`set-password`, `set`, `get -p`, `inject`, `status` — see [credstore/README.md](credstore/README.md)).

### Model providers

Models are configured once in `slife.yaml`, then switched at runtime from the chat (no file editing):

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

The `active_model` is a `"provider/model"` ref — the model Slife chats with. `job_coding_model` is the LLM used by deterministic **jobs** — name a different (usually smaller/faster) model than `active_model`, so a nested one-shot job call never churns the agent loop's prompt cache (absent → the active model; per-call override: `llm.chat(model=...)`).

**Three first-class API backends:**

| `api` field | Backend | Providers |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama / MiniMax | Chat Completions |
| `anthropic-messages` | Claude / Bailian (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

**Per-model `compat` overrides** (in the model entry, or via `model_set`), for gateways that don't follow the standard thinking shape:

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
            thinkingFormat: "openai"   # anthropic backend: model always thinks, no thinking param
    scnet:
      api: "openai-completions"
      models:
        - model: "MiniMax-M3"
          name: "MiniMax M3"
          reasoning: true
          compat:
            thinking: "omit"           # openai backend: send NO thinking field (gateway 400s on enabled)
```

`compat.thinking` on the OpenAI backend: `"omit"` sends no thinking field (for gateways that reject the `{"type": "enabled"}` shape but reason natively), `"disabled"` forces explicit off, `"enabled"` matches the default.

**Runtime switching:** `model_list` → `model_switch(ref="bailian/qwen3.8-max")` in natural language, or the `Ctrl+S` inline picker as an emergency escape when the active model is down. `model_set` upserts (merges — a partial update keeps the model's `reasoning`/`input`/`compat` fields) and accepts a `compat` dict, so per-model overrides need no hand-editing.

**Secrets never reach the LLM.** User input, tool-call arguments, and every tool result pass through a pattern-based sanitizer before entering the context — API key shapes (`sk-*`, `ghp_*`, Bearer tokens, …) are auto-masked.

### Behind a fake-IP proxy

A local proxy in **fake-IP** mode (Clash / mihomo / sing-box TUN) answers every hostname with an address from a pool of its own — typically `198.18.0.0/15` for IPv4, `2001:2::/48` or a ULA range for IPv6 — and maps the address back to the name when the connection arrives. Slife works behind one out of the box: a resolved address is never treated as evidence about where a connection lands, because whether the resolver lies is **measured** rather than assumed.

One case is off by default. `file_save` refuses a URL that arrives as an **IP literal** from such a pool, because a non-public address is normally LAN or cloud-metadata infrastructure. Turn the exemption on if you are behind such a proxy and a URL keeps arriving as a pool address — a redirect `Location:` header, or a link the model copied out of a page:

```yaml
net:
  fake_ip_exempt: auto    # off (default) | auto | on
```

| Value | Effect |
|-------|--------|
| `off` | A non-public IP literal is refused, always. The default — and on purpose (see the cost below). |
| `auto` | Exempt the fake-IP pools while this machine's resolver is measured to lie, i.e. while a pool address really is the proxy's synthetic space. |
| `on` | Exempt them regardless, for a proxy in the path that the probe cannot see — a `fake-ip-filter` that lists the probe name, or a probe that ran before the proxy came up. |

The ranges live in `slife/net.py` (`FAKE_IP_NETS`): `198.18.0.0/15`, `2001:2::/48`, `fdfe:dcba:9876::/48` and `fc00::/18` — the pools Clash, mihomo and sing-box ship or document, IPv6 included. **LAN, link-local and cloud-metadata addresses are never exempted**: `169.254.169.254`, `fd00:ec2::254` and `fd20:ce::254` all stay refused, which is why the IPv6 side is those specific ranges and never `fd00::/8`.

**What the exemption opens.** A proxy's pool also holds the proxy's *own* addresses — on a mihomo machine `198.18.0.1` is the TUN interface and `198.18.0.2` Clash's DNS — so while the exemption is on, a fetch aimed at one of those reaches your machine and whatever is bound to `0.0.0.0` (a dev server, a Docker-published port, a controller misconfigured to listen on every interface). Nothing else opens. That one target is why the default is `off`.

**Reading pages through the proxy.** `mcp-server-fetch` performs no address check and reads pages normally behind such a proxy. `duckduckgo-mcp-server` refuses page content in its own SSRF check, and ships no way to exempt a range — only an all-or-nothing `--allow-private-urls` that would open the LAN along with it — so use DuckDuckGo to search and the `fetch` server to read.

<a id="features" name="features"></a>
## Features

### Tools

Every tool accepts three meta-parameters: `_timeout` (per-call override; it overrides the tool's own default, while `0` or negative means "use the default", never "no timeout"), `_async` (run in background, poll with `check_async`; a backgrounded call with no `_timeout` runs unbounded), and `_approve` (inline approval prompt — Y approve / N, Esc deny).

**61 builtin tools in 13 categories** below; the bundled config registers 60 of them (`install_python_package` ships disabled). Two reserved harness tools are driven by Slife itself rather than by the model — `_turn_prompt` (the per-turn prompt, once per turn) and `_check_new_input` (mid-turn message injection) — while `attach_image` is invoked for you on `@`-attachments *and* can be called by the model itself; it refuses at call time on a model without vision.

| Category | Tools |
|----------|-------|
| System | `system_health`, `system_tools_list`, `check_async`, `cancel_async`, `set_max_iterations`, `set_midturn_input` (mid-turn preemption on/off), `notify_user`, `wait_minutes` (end the turn and resume later as a new one), `profile_edit` (replace `USER.md`, the user's standing profile) |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` (disabled by default) |
| Schedule | `scheduled_task_set`, `scheduled_task_remove`, `scheduled_task_list`, `scheduled_run_list`, `scheduled_run_skip`, `run_schedule_now` |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_list_tools`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `remove_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_run_task_background`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image` (feed images to a vision model), `_turn_prompt` (per-turn prompt, driven by Slife) |
| Harness | `_check_new_input` (mid-turn input, driven by Slife) |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| embeddings | `embeddings_model_list`, `embeddings_model_set`, `embeddings_model_switch`, `embeddings_model_remove`, `embeddings_enable` |
| ToolSystem | `tool_search` (search every category), `func_tool_load` (load a tool — an mcp/rest-api one too), `_func_tool_unload` (unload by name) |

**Managed categories** (Skills / CLI / REST API / Models / MCP) support `X_list` / `X_set` / `X_remove` (+ `X_set_enabled` where a toggle applies). Every `X_set` tool is an idempotent upsert, and `model_set` merges into the existing entry — a partial update keeps the fields you did not mention. `rest_api_set` points one entry at an OpenAPI document, and every endpoint in it becomes a typed `{name}__{endpoint}` tool — a REST API is simply an entry in the `rest-api` section of `tools.yaml`.

**Plugin tools** — built-in plugins register under bare names (the job plugin uses hyphens); external MCP servers appear as `{server}__{tool}`:

| Server | Tools |
|--------|-------|
| `mcp-gateway` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools` (capped — `tool_search` finds the rest) |
| `memdb` | `turn_search`, `turn_list`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_edit`, `note_remove`, `diary_write`, `file_save`, `file_remove`, `note_list`, `diary_list`, `note_read`, `diary_read`, `file_list`, `cabinet_search`, `cabinet_summarize`, `file_read`, `report_save`, `report_list`, `report_read` |
| `sharefile` | `share_file`, `sharefile_unshare` |
| `a2a` | `a2a_send_message` (async — returns a task_id, the result pushes back later), `a2a_cancel_task`, `a2a_list_agents`, `a2a_broadcast` |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| `job-coding` | `job-list`, `job-write`, `job-remove`, `job-run` + one tool per registered job (e.g. `job-translate`) |

**Tools are loaded on demand.** Third-party capability enters as a standard MCP server in `tools.yaml` (`mcp` + `rest-api` sections — any stdio / SSE / Streamable HTTP server works, no Slife SDK required). The model finds a tool with `tool_search`, then injects it with `func_tool_load(full_name)` — per tool, not per server, so one large server puts only the tools actually in use in front of the model; loading an external tool also materializes its execution proxy, and a load that cannot do that rolls back and says why. Loading decides what the model *sees*, not what may run: a tool with an execution route is callable by name either way, and only an external proxy is unregistered again on unload (it holds a live client). A few are always available — the harness tools, the meta tools, the pinned `skill_use` / `system_health`, and anything marked `autoload: true` (per tool, or per **server** for an external one) — and the injected list is capped by `tool_load.threshold` (default 100), evicting least-recently-used tools first and never an `autoload` one. A name belongs to its owner: an entry cannot take a name another family already owns, so a job cannot shadow a builtin. Enabled servers are brought up at boot; one that is down has its tools marked `error`, so a dead transport is never injected.

**Windows execution.** `execute_shell` runs in the shell Slife detected — PowerShell or cmd — so a command written for that shell executes as written, and its non-ASCII output decodes correctly. `run_python_script` runs its child as UTF-8, so non-ASCII output can't crash it.

### Memory — always on

Every turn is permanently recorded in SQLite (`~/.slife/<agent>.db`) and searched three ways — with time as an axis any of them can be bounded by:

| Mode | Best for |
|------|----------|
| `grep` | A regex — partial spellings too: error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall — keyword and meaning together |

Browsing by date is `turn_list` (the same axis every mode can be bounded by, read on its own). A search refuses an unknown mode and an empty query rather than falling back silently — an empty query is what the list tools are for.

**Memory is a core feature — the agent never runs silently without it.** If the memory DB is broken (missing column, corruption, disk error), the agent fails loudly instead of pretending: a session that can't restore aborts at startup with the error; a turn that can't be saved freezes the inbox and shows a red banner — new turns stop until the DB is fixed and the agent is restarted.

Embeddings are a **first-class top-level `embeddings` section** in `slife.yaml`, shared by `memdb`, `memfiles` and the tool catalog; they are managed by the builtin `embeddings_*` tools, and runtime index status is surfaced by `system_health`. Each provider is an **OpenAI-compatible endpoint** (`base_url` + `api_key`), and `active_model` names the *provider* (e.g. `"local_embed"` or `"siliconflow"`) rather than one model. The **`local-embed` service** — a plugin Slife starts for you, or the instance you started yourself — serves local GGUF/transformer models at `http://127.0.0.1:17347/v1`. **Keyword search works without any embedding backend.** Semantic (hybrid) results are only served once the index is fully built for the current model — while a reindex runs, hybrid degrades to keyword-only and resumes automatically when indexing finishes.

Each turn records two timestamps — your input time (`created_at`, the Enter-press moment) and the assistant's completion time (`completed_at`) — shown as dim `[HH:MM]` markers. User messages also carry an **`[INFO: {"turn_id": N, …}]`** footnote (the turn id plus when it happened), which the agent uses to reference a turn by id (`turn_read` / `turn_summarize`) and which you read in the same line.

Every turn remembers **which channel it came from** — `human`, `wechat`, a subagent, the heartbeat, an A2A peer, or `system` (Slife itself) — so each bubble carries the matching prefix on session restore: `You>`, `Wechat>`, `Subagent(<name>)>`, `Heartbeat>`, `A2A(<peer>)>`. `system` turns — schedule and timer triggers — show no prefix and are hidden from the chat. An incoming WeChat message also reaches the model prefixed **`[Wechat:{...}]`**.

### Autonomous heartbeat

While idle, the agent can take a periodic autonomous window — **off in the shipped config** (`agent.heartbeat_interval: 0`), since it spends tokens on its own. Set it to a number of seconds to turn it on (30 minutes if you leave the key out). A beat is skipped while the inbox is busy or has queued work, so it never competes with real input. It runs as a normal turn (its own turn, saved to memory); the reply contract is real content if it has something worth saying, otherwise a single `.`. A bare `.` reply is **silence** — never rendered in the chat or session restore, from any event — while a real autonomous reply renders as `⚡ 自主`. This is the precondition for emergent self-initiated behavior.

### Scheduled tasks

Ask the agent to do something on a schedule — "write a diary entry every night at midnight", "summarize the week every Friday" — and it registers a cron-scheduled task (`scheduled_task_set`). The task name identifies the task — its runs, its report, the trigger — so it should be a short ASCII identifier; it is never a worker's name. When a task fires, the agent dispatches the work to a subagent worker taken from the pool (`run_schedule_now`) rather than doing it inline, and the worker saves the result as a **report** in the file cabinet (`report_save`) and notifies you when done — the completion reads `📅 Scheduled(<task>)>`, since which worker ran it is not something the task owns. Every fire is recorded (`scheduled_run_list`), so you can see what ran and what it produced (`report_list` / `report_read`). A task's **description is required** at creation — it is the worker's instruction, so an empty task cannot exist. The dispatched task carries your current conversation like any other delegation, so a task that depends on what you have been discussing can say so.

Tasks fire **only while Slife is running**. At the next start a one-shot sweep settles what a previous session left behind in `scheduled_run_list`: runs that never finished become **failed**, and fires that were due while Slife was closed are marked **missed**. Nothing is announced and nothing waits for your input — a failed or missed run can still be backfilled with `run_schedule_now` (firing immediately) or closed with `scheduled_run_skip`.

### Jobs — deterministic, code-defined

For work that is well-specified and repeatable — translate, summarize, extract, classify, format — a **Job** runs one code-defined function with exactly the arguments it declares, instead of dragging a whole conversation into an agent turn. Jobs are plain `.py` files in `~/.slife/jobs/` (one public function = one `job-<function>` tool; see the `job-coding` skill for the conventions and the bundled `translate` / `summarize` samples). The plugin reloads them at every start and manages them live:

- `job-list` — see the registered jobs
- `job-write` / `job-remove` — add/change (create or replace; a broken write rolls back) or delete a job; its tool appears/disappears immediately and survives restarts
- `job-run` — run any job by name, or call the job's own tool directly

A job that needs the LLM calls it **once** — a narrow, explicit `llm.chat` on `job_coding_model`, a model you configure independently of the conversation's active model so jobs stay cheap and never disturb the agent's prompt cache. No conversation history, system prompt, or agent loop ever reaches a job.

A job can also drive **any external MCP server** configured in `tools.yaml` — including tools the main agent has not loaded — with one call per statement: `await mcp.call(server, tool, args)`. `mcp.call` never raises: an unreachable gateway, a disconnected or disabled server, or an unknown tool returns a clear `Error: ...` string the job can branch on. A job that needs either handle imports it: `from slife.plugins.job_coding import llm, mcp`.

### Images & vision

Attach images with `@path` / `@url` syntax (quotes supported for paths with spaces) to feed them to a vision-capable model:

```
Check this screenshot @D:\Downloads\error.png
```

Vision-capable models receive local files as base64 data URIs and HTTP(S) URLs as-is; the `attach_image` tool lets the agent attach images mid-turn (local sources are capped at 20 MB so a stray path never base64s a huge file into the context). Nothing is ever rendered in the terminal — files open with the OS default app, and `share_file` publishes a local file as a public HTTPS link via a pluggable tunnel provider (ngrok / localhost.run / Cloudflare Quick Tunnel, chosen by `sharefile.yaml`; `share_file` returns a graceful error while the tunnel is offline). It refuses credential-looking paths (`.env`, `id_rsa`, `.npmrc`, `netrc`, …).

### Plugins

Nine built-in plugins run as separate processes beside the agent. One of them — **mcp-gateway** — is the door to everything external: third-party capability reaches Slife as a standard MCP server in `tools.yaml`.

| Plugin | Role |
|--------|------|
| **mcp-gateway** | The MCP gateway — connects external MCP servers (stdio / SSE / Streamable HTTP) and holds the live connections. Management: `mcp_list`, `mcp_list_tools`, `mcp_set`, `mcp_set_enabled`, `mcp_remove`; a tool loads on demand with `func_tool_load` |
| **memdb** | Turns database with hybrid search |
| **wechat** | Bidirectional WeChat messaging |
| **memfiles** | Notes / diary / files / reports cabinet (private). Notes, diary and reports are plain markdown in `~/.slife/<agent>.files/`, indexed so the agent can search them. `file_read` returns a file's **text** or refuses (`… is not text (<mime>, <n> bytes)`) and announces the encoding when it is not UTF-8; `file_save` takes a local path or an http(s) URL (SSRF-guarded on every redirect hop, 50 MB cap). All save tools return local paths — never auto-publish |
| **sharefile** | Public file sharing — `share_file` publishes a local file as a public HTTPS URL (pluggable tunnel from `sharefile.yaml`) |
| **a2a** | A2A mesh channel over MQTT (only starts when the broker is reachable) |
| **media** | Non-chat AI generation (image, video, TTS, ASR) from the providers you configure. Tools: `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |
| **job-coding** | Deterministic jobs — code-defined functions in `~/.slife/jobs/` run with exactly their declared args; one-shot LLM calls via `llm.chat` on `job_coding_model`. Tools: `job-list`, `job-write`, `job-remove`, `job-run` + one per job |
| **local-embed** | Local embedding endpoint service for `memdb` + `memfiles`. Also runnable standalone — Slife uses your instance rather than starting a second one |

A crashed plugin is **restarted automatically**. **Required plugins** (`plugins.required` — `memdb` and `memfiles` in the bundled config) are core: one that cannot start **aborts startup** instead of limping on. Everything subordinate — external MCP servers, the tunnel, WeChat login, media providers, the A2A broker, `local-embed` — never blocks startup and never aborts it: those dependencies fail open, recover on their own at runtime, and report through `system_health`.

### A2A — agent-to-agent mesh

A2A is how separate agents — on one machine or across several — discover each other, delegate tasks, and push results back. It speaks the official **A2A-over-MQTT** profile, so Slife agents interoperate with any other implementation of it:

- **Mesh tools** (one uniform `a2a_` prefix): `a2a_send_message` (async — returns a task_id immediately, the result arrives later), `a2a_cancel_task`, `a2a_list_agents`, `a2a_broadcast` (fire-and-forget event). Inbound peer traffic reaches the model in one `[A2A:…]` envelope (`from` names the sending peer — never the receiver); the TUI shows `A2A(<peer>)>`. The `a2a` plugin only starts when the MQTT broker is reachable. A peer's withdrawal reaches the model as a message and is left to its judgment, never turned into a harness preempt; a task still in flight when the process dies is recorded per agent and reported as **stale**, since the reply path died with the process.
- **Subagents are local workers, not A2A peers**: `spawn_subagent` / `subagent_send_task` / `subagent_get_task_result` / … create workers that share your plugins and run one task at a time (a sync send to a busy worker is auto-queued as async). Each task is sent with a copy of the conversation so far — the turns already settled, not the work in progress — and the worker knows where that copy ends and its task begins, so it does the task rather than continuing yours. The copy is a candidate, not a floor: before running, the worker selects what it needs from it, so a task can end up carrying none of it. Delegating twice to the same worker gives each task what was true when *it* was sent. Naming a worker is optional: `subagent_run_task_background` takes whichever worker is idle and spawns one (`worker-1`, `worker-2`, …) when none is, and reports the worker it used — with the pool already at `subagent.max_subagents` and every worker busy, it says so instead of queueing. Async results auto-push to your chat (`mode="auto"`, default) or stay pollable-only (`mode="poll"`). Subagents never drain your inbox — all replies and management belong to the main agent. A worker keeps the config its process started with — your active model, your profile (USER.md), and the tools.yaml wiring — while the shared tool catalog and the plugin servers stay live; a config change reaches a worker only when it is replaced, with `remove_subagent` or a restart.
- **One name is one agent.** The agent name *is* the mesh identity — the MQTT client id and the topic segment peers address — so a second instance with the same name on the same broker would take the first one's session over, and every task sent to that name would run in both. The mesh checks the name before joining, so the newcomer refuses and says whose the name is; `system_health` reports it as the reason the mesh is down. Give each instance its own `--agent` name, or different `a2a` `org`/`unit` values.

  Leaving is automatic: a clean shutdown publishes an *offline* card before disconnecting, and a crash gets the same card from the broker's last will — so the name frees itself. Only a broker that keeps retained messages across restarts (`persistence true`, off by default) can hold a card nobody is behind; if no other slife is running and the mesh still refuses, clear that card and retry on the next connect:

  ```bash
  mosquitto_pub -h localhost -t '$a2a/v1/discovery/default/default/slife' -r -n
  ```

  (adjust `default/default/slife` to your `a2a` `org`/`unit` and `--agent` name)

All messages — human, WeChat, MQTT, subagent results — flow through a single inbox queue and are processed one turn at a time.

<a id="semantic-memory-search--installation-guide" name="semantic-memory-search--installation-guide"></a>
## Semantic Memory Search — Installation Guide

Semantic (hybrid) memory search — recall by meaning across `memdb` turns, `memfiles` notes and the tool catalog — needs a local embedding **backend** and the **model weights**, which the one-click installer deliberately does not bring. Keyword search (`grep` / `fts5`) works without them, and every piece is fail-open: a missing backend leaves a working keyword-only core.

**Full guide — backend install per platform, weights, `local_embed.yaml`, verification, troubleshooting:**
**[local-embed/README.md](local-embed/README.md)** — the `local-embed` service is the endpoint slife embeds against, and that file is its manual.

<a id="usage-reference" name="usage-reference"></a>
## Usage Reference

### Keyboard shortcuts

Key caps (`Ctrl+C`, `Esc`, …) are universal; the action words after them localize with the interface language.

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Esc` | Cancel agent loop |
| `Ctrl+S` | Switch model (`↑`/`↓` to move, `Enter` to pick, `Esc` cancels) |
| `PageUp` / `PageDown` | Scroll the transcript |
| `Home` / `End` | Scroll to top / bottom |
| `Ctrl+Y` | Copy result (on a tool call) |
| `Enter` / `Space` | Expand/collapse (an assistant thinking block, or a tool call's detail) |
| `↑` / `↓` | Input history navigation (from the first/last line of the input) |
| `Shift+Enter` | Insert newline in input |
| `Y` / `N` / `Esc` | Approve / deny / dismiss (on an approval prompt) |

### CLI

| Flag | Description |
|------|-------------|
| `--agent <id>` | Agent identity — separate turns database, memory files and tool catalog, plus the A2A mesh name (default: `slife`). The mesh name must be unique on the broker: a second instance with the same name refuses to join instead of fighting for it |
| `--headless` | Run as a headless agent — the same agent with no terminal attached (see below) |
| `--lang <en\|zh>` | TUI language — force English / Chinese (default: auto-detect from OS locale) |
| `-h`, `--help` | Print the usage and exit |
| `<config-path>` | Positional — use a specific config file (its parent dir becomes the data dir) |

### Headless mode

`slife --headless --agent jack` runs **the same agent** with no terminal attached: the same turns database, the same plugins, the same heartbeat and schedules — but no keyboard and no chat view. Its input is whatever reaches it from outside: A2A messages from peers, results pushed back by its own workers, heartbeat and schedule triggers. `Ctrl+C` stops it.

```bash
slife --headless --agent jack                 # default config
slife --headless --agent jack myconf.yaml     # a specific config file
```

* **It is silent.** Nothing is written to the terminal except startup warnings and errors: every turn is in the turns database and the session log is in `~/.slife/logs/`. What the agent says to the world leaves over the mesh. The one exception is a memory database that breaks mid-session — it **exits** with the reason on stderr rather than sitting there alive and deaf.
* **Nobody can approve a tool call**, and it is told so in its system prompt — a call marked `_approve` runs without asking. Run a headless agent only with an agent identity whose reach you are comfortable leaving unattended.
* **A mesh that is not up is a warning, not a failure.** If the broker is unreachable, or the name is already held by another process, it starts anyway and warns on stderr; the plugin watchdog keeps retrying.
* **A restart resumes.** The exit-time context is restored on start, exactly as the TUI restores it, so a peer's task that spans several turns survives a restart.
* One name is one agent: a headless `jack` and a TUI `jack` cannot hold the same mesh identity at the same time. Give them different `--agent` names.

### Two agents on one machine

Isolation follows the **data directory**, not `--agent`. Two instances pointed at the same data dir share everything in it; two pointed at different data dirs share nothing.

**Same data dir.** `--agent` splits the per-agent pieces; everything else is common ground.

| Per agent | Shared |
|---|---|
| turns database `<agent>.db`, memory files `<agent>.files/`, tool catalog `<agent>.tools.db`, A2A inbound ledger `a2a_inbound_<agent>.yaml`, WeChat session `wechat_<agent>.yaml`, log and session-marker filenames | `slife.yaml`, `tools.yaml`, `sharefile.yaml`, `local_embed.yaml`, `skills/`, `jobs/`, the `logs/` directory |

Sharing the configs means sharing *state*, and that is this layout's price:

* **Both agents use the same models and the same tools.** `active_model`, the `models` list and every tool section live in the shared configs, so there is no way to give one agent a model or a tool set the other does not have.
* **A runtime change is live in one instance and pending in the other.** Adding a server with `mcp_set`, switching a model, editing a section — it takes effect at once where it was made, and reaches the other instance at its next start (its MCP gateway reads `tools.yaml` once, when it spawns). Until then the two disagree about which tools exist: you may be told about a tool the other agent cannot call yet. Nothing breaks, and a restart settles it.
* **Config writes can neither be lost nor interleaved.** Every read-modify-write of a config file is serialized across processes by a lock file, so concurrent edits queue instead of clobbering each other.

**Different data dirs.** Full isolation — separate configs, catalogs and indexes, skills, jobs, logs. Pass a config file and its parent directory becomes the data dir (the default `~/.slife` needs no flag, and a directory that has no config yet is an error, not a fresh start):

```bash
mkdir -p ~/agents/jack
cp ~/.slife/slife.yaml ~/agents/jack/slife.yaml   # its own config, edited independently
slife --agent jack ~/agents/jack/slife.yaml       # data dir = ~/agents/jack
```

The cost is duplication you maintain by hand: two configs to keep in sync, `skills/` and `jobs/` seeded per directory, and a catalog index embedded per agent. In exchange the two can differ in everything — models, tools, schedules.

**Either way**, external MCP servers are started once per instance (two processes for the same server: two browser sessions, two npm caches — free while a server is stateless, not free if it owns a database, a profile directory or an account session), the local-embed service is shared (a second child adopts the instance already holding its port rather than loading the model twice), and the A2A mesh name must still be unique per instance.

### Health & logs

* **`system_health`** reports live status for every subsystem in one call — a verdict line, then problems with what to do about them, then one line per healthy component. Ask the agent to run it any time something seems off.
* **Logs** live in `~/.slife/logs/` (one per session, `event_name key=value` lines, DEBUG+; plugins inherit the session id). The terminal belongs to the chat in the TUI and to nothing at all in headless mode — logs never print to it.
* **A session that was killed is reported by the next one.** A hard kill (`taskkill`, End Task, a closed window) runs no Python, so the victim writes nothing itself — instead every session leaves a marker file (`logs/.session.<agent>.<pid>.state`) that only a clean exit removes. Finding one of its own whose process is gone, slife warns `the last session … was killed from outside` and names its log. The marker carries the agent name, so each instance reports only its own kills — two agents sharing one data dir never report each other's. The same kill leaves that terminal in raw mode — keystrokes echo as garbage and `Ctrl+C` does nothing; close that window to recover.

## License

MIT
