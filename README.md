# Slife

> **Terminology.** The authoritative definitions of the project's terms —
> model-facing and developer-facing alike — live in **[Glossary.md](Glossary.md)**.
> This README uses those terms without restating them.

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers every turn forever, and orchestrates other agents.

```
You: "Find all TODO comments and create GitHub issues"
  → LLM calls search_content("TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked above."
```

One TUI window around an LLM tool loop: 65 native tools across 13 categories (including a reserved harness tool, `_sys_note`), six built-in plugin services plus the standalone `mcp-plugin` MCP gateway and the `local-embed` embedding service (both external plugins), always-on memory with hybrid search, vision image attachments (`@path`/`@url`), runtime model switching across three API backends, and an agent-to-agent mesh — everything presented to the LLM as uniform OpenAI-style function definitions.

Requires Python 3.13+. Runs on Windows (native & WSL), macOS, and Linux.

**Bilingual interface.** The TUI follows your OS language — Chinese on a Chinese system, English everywhere else. Detected at startup via [`sys-lang`](https://pypi.org/project/sys-lang/) (Windows `Get-Culture` / *nix locale); all in-TUI text — system messages, the approval prompt, the model picker, tool-call labels, the status bar — renders in the right language. What the LLM sees (system prompt, tool schemas) stays uniformly English; so do logs.

## Install

**Zero prerequisites, fully out-of-the-box.** The install script fetches the latest `main`, builds slife **from source** (workspace wheels — no PyPI, always the newest code), and installs it with uv into an isolated tool venv. It auto-installs what's missing — uv, Node.js (`npx`), bun, and Mosquitto — then seeds the three **git-tracked configs** and the bundled **skills** into their per-module folders, and builds the MCP tool catalog, so a first-time user gets the full tool set — local embeddings, external MCP servers, yt-dlp, browser-harness, the A2A mesh — with nothing to configure by hand. On WSL it uses Linux-native runtimes (Windows executables cannot receive custom env vars via WSL interop).

The **semantic embedding backend and model are deliberately not part of the install** — the backend is env-specific (CPU / CUDA / Metal) and the model download is ~2 GB, so it's a user-run step. Follow the **[Semantic Memory Search — Installation Guide](#semantic-memory-search--installation-guide)** below: install one backend, download the model weights, configure `HF_HUB_CACHE` / `BGE_M3_GGUF_PATH`, then verify the service is ready.

### Environment requirements

The installer is best-effort: it uses standard paths, tries several install routes per runtime, **warns and continues** when one is unavailable (only that runtime's features are affected), and never silently swaps in an old or alternate version. Pass `--core` (or set `SLIFE_CORE=1`) to skip the optional CLI tools for a light core-only install.

| Runtime | Where / how it's installed | Why it's needed |
|---|---|---|
| uv | official installer → `~/.local/bin`; Python 3.13 managed by uv | builds and runs slife |
| Node.js (`npx`) | package manager (apt / brew / dnf / pacman / winget) → cluster `module load nodejs` → official LTS tarball → `~/.local` (rootless fallback) | npx-based MCP servers: `file-search`, `serper`, `tavily-mcp`, `github`, `amap-maps`, `filesystem` |
| bun | `~/.bun/bin` | `nvidia-nim` MCP server |
| Mosquitto | package manager (winget / apt / brew / dnf / pacman) | A2A MQTT mesh — best-effort auto-install; A2A stays disabled until a broker runs |
| `unzip` (Linux) | package manager | bun installer dependency |

**Installed by default:** `yt-dlp` and `browser-harness` (both skipped by `--core`), Mosquitto (always attempted), the three configs (`slife.json5` → `~/.slife/`, `local_embed.json5` → `~/.local-embed/`, `mcp-plugin.json5` → `~/.mcp-plugin/`) seeded from bundled defaults, the bundled skills (`~/.slife/skills/`), and an MCP server catalog built at the end (`mcp-plugin build`).

If a runtime can't be installed, the installer **warns and continues** — slife itself still installs; only the features needing that runtime are unavailable. For example, on a Linux box older than **glibc 2.28 / libstdc++ 3.4.29**, the Node rootless tarball fallback won't run (the installer reports the missing `GLIBC_2.28` / `GLIBCXX_3.4.xx` symbols). The supported route is **not** an older Node — it's a Node built for your distro (e.g. `module load nodejs` on HPC clusters, or your distro's package). Install that, then re-run this installer — it detects an existing `npx` and skips its own Node install.

If you edit `mcp-plugin.json5` by hand, restore the MCP server catalog with `mcp-plugin build` (both `mcp-plugin` and `local-embed` are on PATH after install). Every optional step is **fail-open**: an error warns and continues, leaving a working core.

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
- **Configs** that already exist are left untouched; resetting one to the bundled default is **asked per file, only when its content differs from the bundled default** (identical files are skipped silently; default: no). Missing configs are seeded silently.
- **Skills** that already exist are left untouched; overwriting one is **asked per skill, only when its content differs from the bundled default** (identical files are skipped silently; default: no). Missing skills are seeded silently.

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

The repo also ships four standalone PyPI packages — install each independently:

| Package | Install | Purpose |
|---------|---------|---------|
| `slife` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh \| bash` | The agent (this README) |
| `credstore` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh \| bash` | Cross-platform credential storage |
| `cc-switch` | `curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh \| bash` | Generate `~/.claude/settings.json` |
| `mcp-plugin` | installed with slife, or `uv tool install mcp-plugin` | MCP gateway for external MCP servers |

Installing slife depends on [credstore](credstore/README.md) and
[mcp-plugin](mcp-plugin/README.md) — it does **not** install cc-switch. See the
[cc-switch](cc-switch/README.md), [credstore](credstore/README.md), and
[mcp-plugin](mcp-plugin/README.md) READMEs for details.

`slife`, `credstore`, and `cc-switch` each have a one-click installer
(macOS / Linux / WSL: `install.sh`, Windows: `install.ps1`) and uninstaller kept
in their package directories; `mcp-plugin` ships no installer of its own — it is
installed as a slife dependency or via `uv tool install mcp-plugin` from PyPI.

## Semantic Memory Search — Installation Guide

Semantic (hybrid) memory search — recall by meaning across `memdb` turns and `memfiles` notes — needs **two things** the one-click installer deliberately does not bring: a local embedding **backend** (a Python package, platform-specific) and the **model weights** (downloaded by you — the server never auto-downloads). Keyword search (`grep` / `fts5` / `time`) works without any of this. Setup is a **user-run** step; every piece is fail-open, so a missing backend leaves a working keyword-only core.

**How it fits together.** slife treats every embedding provider as an OpenAI-compatible endpoint (`base_url` + `api_key`). The `local-embed` external plugin — installed with slife, running in the same tool venv — loads **one** local model **once** and serves it at `http://127.0.0.1:17347/v1` (`POST /v1/embeddings`, `GET /v1/models`, `GET /health`). `memdb` and `memfiles` both call that endpoint, so a model is never loaded twice. Which model serves is decided by `local_embed.json5` (`active_model`); slife's `embeddings.active_model` (`"local-embed"` by default) falls back to the endpoint's active model.

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

**Transformer route (default config, ~2 GB).** The seeded active model is `BAAI/bge-m3`; download it into the HF cache and no config change is needed:

```bash
hf download BAAI/bge-m3                                    # → ~/.cache/huggingface/hub
HF_ENDPOINT=https://hf-mirror.com hf download BAAI/bge-m3  # mainland-China mirror
```

**GGUF route (small offline file, ~100 MB).** Use any quantized BGE-M3 GGUF you trust — these are community conversions with no single authoritative source (prefer a high-fidelity `Q8_0`). Get it from any source (HF single-file pull, browser, `wget`/`curl`), then place it at the default path and switch the active model:

```bash
hf download <owner>/<repo> <model>.gguf --local-dir ~/.local-embed/models   # HF single-file pull
mv ~/.local-embed/models/<model>.gguf ~/.local-embed/models/bge-m3-q4_k_m.gguf     # the expected default path
```

The GGUF entry is **inert until it is active** — see `active_model` in step 3.

### 3. Configure the HF cache & GGUF path

Everything — host, port, models, active model, backend — lives in **`local_embed.json5`**, seeded by the installer (path resolution: `$LOCAL_EMBED_FILE` > slife project root (dev) > `~/.local-embed/local_embed.json5`). Values support `${VAR}` / `${VAR:-default}` expansion, and **a shell env var wins over the config**. The seeded file already carries portable placeholders — usually you only set env vars or edit two lines:

```json5
{
  active_model: "BAAI/bge-m3",                       // "BAAI/bge-m3" (transformer) or "bge-m3" (gguf)
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
| `active_model` | Which entry serves: `"BAAI/bge-m3"` (transformer) or `"bge-m3"` (gguf). |

Changes apply on the next start of the local-embed service (restart slife).

**CLI alternative** — `local-embed` (on PATH after install) upserts a model config, makes it active, and pins the port (idempotent, leaves other models untouched):

```bash
local-embed set BAAI/bge-m3 --HF_HUB_CACHE ~/.cache/huggingface/hub
local-embed set-gguf bge-m3 --path ~/.local-embed/models/bge-m3-q4_k_m.gguf
```

### 4. Make the service ready — verify

Start slife. `local-embed` runs as an external plugin; its MCP handshake completes fast, and the **model load is deferred to post-handshake warm-up** — the first embed loads it (a few seconds for GGUF, up to a minute for the ~2 GB transformer). Verify from inside the chat or over HTTP:

- **In the chat** — ask the agent to run `system_health` (reports an `embeddings` component: online / active model / loaded), `embeddings_probe` (configured vs. live `/v1/models`, active model marked ★), and `memdb_semantic_status` / `memfiles_semantic_status` (report `semantic_ready`).
- **Over HTTP** (the service is standalone at the fixed port):

```bash
curl http://127.0.0.1:17347/health            # {status, backend, model, dimension, loaded}
curl http://127.0.0.1:17347/v1/models         # every configured model + the active flag
curl http://127.0.0.1:17347/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model": "bge-m3", "input": ["hello world"]}'   # returns a real vector
```

A healthy state: `/health` → `loaded: true`; `system_health` → `embeddings` component online, active model loaded; `memdb_semantic_status` → `semantic_ready: true`. When the service is unreachable (backend missing, weights missing, still loading), slife **degrades gracefully to keyword search** — `system_health` reports the reason, and once the index is fully built for the current model, hybrid results resume automatically.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Log: `backend_unavailable … reason=llama_cpp_not_installed` / `sentence_transformers_not_installed` | Run the step-1 install for your platform — the log prints the exact command. |
| Transformer route won't load with `HF_HUB_OFFLINE=1` | The repo isn't in the cache — run `hf download BAAI/bge-m3` and make sure `HF_HUB_CACHE` points at the cache that holds it. |
| GGUF route won't load | File missing at `gguf_path` — check `BGE_M3_GGUF_PATH` / `gguf_path`, and that `active_model` is `"bge-m3"` (the GGUF entry is inert while the transformer is active). |
| `system_health` shows the embeddings component with the active model not loaded yet | Normal during warm-up; the model loads on the first embed. Re-check after a few seconds. |
| First embed very slow | A transformer download/warm-up is deferred to the first embed; subsequent calls are fast. |

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

## Configuration

Secrets in the credential store, config in JSON5:

| Layer | Storage | Contents |
|-------|---------|----------|
| **Secrets** | credential store (credstore) | API keys — encrypted at OS level, plus an encrypted cryptfile backup |
| **Config** | `~/.slife/slife.json5` | `${VAR}` references + non-secret values |

```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → resolved from credstore at runtime
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

`${VAR:-default}` fallback syntax is supported (resolution order: shell env → credstore → literal default). Secrets can also be referenced as `keyring:service/key` URIs.

**sLife does not support credstore's cryptfile mode, but is fully compatible with environment-variable setup.** sLife's credential resolution is password-free and never prompts — it reads the credential store (credstore), then falls back to `os.environ`. (no system keyring available, e.g. Linux where the kernel keyring is blocked by seccomp/policy on an HPC login node), sLife does **not** read the encrypted backup; use one of two methods:

1. **Environment variables only (independent of credstore):** export the secrets in your shell:
   ```bash
   export DEEPSEEK_API_KEY="sk-…"
   ```
   sLife resolves `${VAR}` from `os.environ` (checked before credstore), so exported keys work normally.
2. **Keep managing in credstore cryptfile mode, but inject to env:** store credentials as usual (`credstore set-password`, `credstore set KEY`), then push them into the environment so sLife sees them:
   ```bash
   credstore inject DEEPSEEK_API_KEY BAILIAN_API_KEY   # prompts master pw in cryptfile-only mode
   # restart shell, or: eval "$(credstore inject DEEPSEEK_API_KEY)"
   ```
3. **Plaintext in the config file (tolerated, not recommended):** sLife accepts a literal `api_key` in `slife.json5`. It works, but the secret sits in plaintext on disk (`~/.slife/slife.json5`, chmod 0600) — prefer methods 1 or 2.

`credstore` itself works fully in cryptfile-only mode (`set-password`, `set`, `get -p`, `inject`, `status` — see [credstore/README.md](credstore/README.md)).

**Three first-class API backends:**

| `api` field | Backend | Providers |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama / MiniMax | Chat Completions |
| `anthropic-messages` | Claude / Bailian (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

**Per-model `compat` overrides** (configured in the model entry, or via `model_set`):

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
        compat: { thinking: "omit" },          // openai backend: send NO thinking field (gateway 400s on enabled)
      }],
    },
  },
},
```

`compat.thinking` on the OpenAI backend: `"omit"` sends no thinking field (for gateways that reject the `{"type": "enabled"}` shape but reason natively), `"disabled"` forces explicit off, `"enabled"` matches the default.

Switch at runtime: `model_list` → `model_switch(ref="bailian/qwen3.8-max")`.

**Secrets never reach the LLM.** User input, tool-call arguments, and every tool result pass through a pattern-based sanitizer before entering the context — API key shapes (`sk-*`, `ghp_*`, Bearer tokens, …) are auto-masked.

## Features

### Tools

All unified as OpenAI function definitions. The LLM sees no difference between native, plugin, and external MCP tools.

**65 native tools in 13 categories** — auto-discovered from `slife/tools/` (64 LLM-visible + 1 harness `_sys_note`; `attach_image` is dropped when the active model has no vision, and `install_python_package` is disabled by default in the shipped config):

| Category | Tools |
|----------|-------|
| System | `system_health` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` |
| Schedule | `scheduled_task_set`, `scheduled_task_remove`, `scheduled_task_list`, `scheduled_run_list`, `scheduled_run_skip`, `run_schedule_now` |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch`, `attach_image` (feed images to a vision model), `_sys_note` (context status, auto-invoked) |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| Meta | `list_native_tools`, `check_async`, `cancel_async`, `clear_context`, `set_max_iterations`, `notify_user` |
| embeddings | `embeddings_probe`, `embeddings_enable`, `embeddings_model_list`, `embeddings_model_set`, `embeddings_model_remove`, `embeddings_model_switch` |
| mcp | `mcp_tool_load` |

The A2A mesh tools (`a2a_*`, 8 of them) and all plugin tools are hosted in plugins, not the native tool set — see below.

Every tool additionally accepts three tool meta-parameters: `_timeout` (per-call override), `_async` (run in background, poll with `check_async`), and `_approve` (inline approval prompt in the chat — Y approve / N deny / Esc deny).

**Five managed categories** (Skills / CLI / REST API / Models / MCP) support `X_list` / `X_set` / `X_remove` (+ `X_set_enabled` where a toggle applies) — all `X_set` tools are idempotent upserts. `model_set` upserts **merge** into the existing entry (a partial update preserves `reasoning` / `input` / `compat`), and accepts a `compat` dict for per-model provider overrides.

**Plugin tools** — built-in plugin tools are first-class and registered under their bare names; external MCP servers appear as `{server}__{tool}`:

| Server | LLM-visible tools |
|--------|-------------------|
| `mcp` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`, `mcp_tool_search` |
| `memdb` | `turn_list`, `turn_search`, `turn_read`, `turn_summarize`, `turn_count`, `turn_token_usage`, `memdb_semantic_status` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_send_typing`, `wechat_check_messages`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `note_save`, `diary_write`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `list_files`, `cabinet_search`, `cabinet_read`, `memfiles_semantic_status` |
| `sharefile` | `share_file` |
| `a2a` | `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` |
| `media` | `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |

Built-in plugin tools are registered under their bare names (e.g. `turn_search`, `wechat_login`, `mcp_set`); each schema carries a `[<server>] ` description prefix. External MCP servers appear as `{server}__{tool}` (e.g. `filesystem__read_file`) and are **loaded on demand**: the LLM discovers them with `mcp_tool_search` — a hybrid keyword/semantic search over the gateway's persistent tool catalog — and loads a chosen one with `mcp_tool_load`. A server with `auto_load: true` keeps the older wholesale registration; enable/disable is server-granular (`mcp_set_enabled`), and `mcp-plugin build` rebuilds the catalog and its search index from live connections.

**Windows execution.** `execute_shell` runs in the detected shell — PowerShell or cmd (the same value the system prompt reports, so the LLM's syntax actually executes) — and its output is decoded with the system code page (GBK/cp936 on Chinese Windows). `run_python_script` forces the child Python to UTF-8 (`-X utf8`) so non-ASCII output can't crash the child.

### Memory — Always On

Every turn is permanently recorded in SQLite (`~/.slife/<agent>.db`). Hybrid search across four modes:

**Memory is a core feature — the agent never runs silently without it.** If the memory DB is broken (missing column, corruption, disk error), the agent fails loudly instead of pretending: a session that can't restore aborts at startup with the error; a turn that can't be saved freezes the inbox and shows a red banner — new turns stop until the DB is fixed and the agent is restarted.

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vector → RRF merge) |
| `time` | Browse by date |

Embeddings are a **first-class top-level `embeddings` section** in `slife.json5` (shared by `memdb` + `memfiles`), managed by the native tools `embeddings_model_list`, `embeddings_model_set`, `embeddings_model_switch`, `embeddings_model_remove`, `embeddings_probe`, and `embeddings_enable` (category `embeddings`). Each provider is an **OpenAI-compatible endpoint** (`base_url` + `api_key`); `active_model` (`"provider/model"` or bare `"provider"`) is configuration-authoritative. The **`local-embed` external plugin** (or a standalone local-embed server) serves a local GGUF/transformer model at `http://127.0.0.1:17347/v1`, loaded **once** and shared by `memdb` and `memfiles` — no double load. The actual model is pinned from the endpoint's `GET /v1/models` when the config names no model. Keyword search works without any embedding backend. Semantic (hybrid) results are only served once the index is fully built for the current model — while a full reindex runs (new/changed model, restart mid-index), hybrid degrades to keyword-only and resumes automatically when indexing finishes.

Each turn records two timestamps — the user's input time (`created_at`, the Enter-press moment) and the assistant's completion time (`completed_at`) — shown as dim `[HH:MM]` markers in the chat. User messages carry a compact **`[INFO: {"turn_id": N, "begin": …, "end": …}]`** footnote (the turn id plus when the turn happened) so the LLM can reference turns by id (`turn_read` / `turn_summarize`) — and the human reads the same line in the TUI.

Every turn also preserves its **source channel** — `human`, `wechat`, a subagent, the heartbeat, an A2A peer, or `system` (Slife itself) — so session restore renders each bubble with the right prefix: `You>`, `Wechat>`, `Subagent(<name>)>`, `Heartbeat>`, `A2A(<agent>)`. The A2A **peer's agent name** travels with the turn and survives a restart; `system` turns (e.g. a scheduled-task trigger) are stored but hidden from the chat.

### Autonomous Heartbeat

While idle, the agent gets a periodic autonomous window (every `agent.heartbeat_interval` seconds; the code default is 60, the shipped template sets 600) to think or act on its own. It runs as a normal turn (own turn, saved to memory); the reply contract is real content if it has something worth saying, otherwise a single `.`. A bare `.` reply is **silence** — never rendered in the chat or session restore, from any event (heartbeat, A2A async-completion notification, etc.); the `[Heartbeat]` trigger is filtered, and a real autonomous reply renders as `⚡ 自主`. A precondition for emergent self-initiated behavior.

### Scheduled Tasks

Ask the agent to do something on a schedule — "write a diary entry every night at midnight", "summarize the week every Friday" — and it registers a cron-scheduled task (`scheduled_task_set`). The task name is also its worker's name, so it should be a short ASCII identifier. When a task fires, the agent dispatches the work to a subagent worker named after the task (`run_schedule_now`) rather than doing it inline, and the worker saves the result as a **report** in the file cabinet (`report_save`) and notifies you when done. Every fire is recorded (`scheduled_run_list`), so you can see what ran and what it produced (`report_list` / `report_read`).

Tasks fire **only while Slife is running**. At the next start a one-shot sweep settles what a previous session left behind in `scheduled_run_list`: runs that never finished become **failed**, and fires that were due while Slife was closed are marked **missed**. Nothing is announced and nothing waits for your input — a failed or missed run can still be backfilled with `run_schedule_now` (it fires immediately) or closed with `scheduled_run_skip`.

### Image & Vision

Attach images with `@path` / `@url` syntax (quotes supported for paths with spaces) to feed them to a vision-capable model:

```
Check this screenshot @D:\Downloads\error.png
```

Vision-capable models receive local files as base64 data URIs and HTTP(S) URLs as-is; the `attach_image` tool lets the agent attach images mid-turn. Nothing is ever rendered in the terminal — files open with the OS default app, and `share_file` publishes any local file as a public HTTPS link via the ngrok tunnel (returns a graceful error while the tunnel is offline).

### Plugins

Six built-in plugins as independent child processes, plus the standalone
`mcp-plugin` MCP gateway:

| Plugin | Role |
|--------|------|
| **slife-mcp** | Gateway for external MCP servers (stdio / SSE / Streamable HTTP) — the standalone `mcp-plugin` package, registered via `plugins.external`. Keeps a persistent tool catalog (`mcp-plugin.db`) searched by `mcp_tool_search`; external tools load on demand via `mcp_tool_load` (per-server `auto_load` restores wholesale registration) |
| **local-embed** | OpenAI-compatible embedding endpoint (`/v1/embeddings`) from one local GGUF/transformer model, loaded once and shared by memdb, memfiles, and the mcp tool catalog — registered via `plugins.external` |
| **slife-memdb** | Turns database with hybrid search |
| **slife-wechat** | Bidirectional WeChat messaging |
| **slife-memfiles** | Notes / diary / files cabinet (private). Notes & diary dual-written to markdown + a SQLite hybrid index. All save tools return local paths — never auto-publish |
| **slife-sharefile** | Public file sharing — sole tool `share_file` publishes a local file as a public HTTPS URL (`/share` route on the same port; ngrok tunnel owned by the plugin) |
| **slife-a2a** | A2A mesh channel over MQTT (only starts when the broker is reachable) |
| **slife-media** | Non-chat AI generation (image, video, TTS, ASR) from any provider — owns the `media:` config section and a provider-agnostic adapter layer (`dashscope-aigc`, `openai-images`). Tools: `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` |

External MCP servers configured in `mcp-plugin.json5` → `servers` — any stdio, SSE, or Streamable HTTP MCP server works, no Slife SDK required. For `url`-configured servers, SSE is auto-detected and Streamable HTTP is the fallback; a Streamable response may arrive as a single JSON body or an SSE stream (both handled). They are **loaded on demand** by default (discover with `mcp_tool_search`, load with `mcp_tool_load`); set `auto_load: true` on a server to bulk-register its tools on connect.

All plugins — built-in and auto-discovered third-party alike — run with a **watchdog** that auto-restarts them on crash (exponential backoff 1s→30s, max 5 restarts). The MCP wrapper watchdog also reconnects external servers after restart. Runtime health — each plugin's internal `__check` tool reports its application-level state and is aggregated into `system_health`; the watchdog is purely process-level.

Readiness follows the MCP standard: a plugin is ready when its `initialize` handshake completes — the server only answers it after its own initialization (FastMCP lifespan) succeeded, during which the plugin establishes its own serving capacity (memdb and memfiles require their store; the other plugins have no local requirement, so serving is readiness). There is no `__ready` probe tool. The lifespan stays **handshake-fast**: heavyweight startup (e.g. the embedding-model load) is deferred until after the first `tools/list` via `warm_after_handshake`, never run inside it. External/subordinate dependencies — external MCP servers, the ngrok tunnel, WeChat login, media providers, the A2A broker, embedding backends — never gate readiness: they are uncontrollable, self-heal at runtime, and are surfaced separately via status tools. Plugins named in `plugins.required` (`memdb`, `memfiles` by default) are core: failing to become ready **aborts startup** with an error instead of limping on. The service opens for user input only once every plugin spawn has converged (ready / skipped / failed — a lifespan that fails its requirement is reported as a failed start and retried by the watchdog), so input can never race ahead of plugin startup.

### A2A — Agent-to-Agent (mesh)

The A2A protocol (JSON-RPC operations and Message/Task/AgentCard data shapes mirroring the official a2a-python reference interface) runs over a pluggable transport **binding** — currently MQTT. The **`a2a` plugin** hosts the LLM-visible tools and the `A2AClient`, and only starts when the broker is reachable:
- **Mesh tools** (one uniform `a2a_` prefix): `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast`.
- **Local workers are NOT A2A**: `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task`. A worker runs one task at a time; a sync send to a busy worker is auto-queued as async (task_id returned) and reported.

A2A's only implemented transport binding is MQTT — setting `transport` to any other value disables A2A with a warning instead of crashing startup. All messages — human, WeChat, MQTT, subagent results — flow through a single inbox queue and are processed one turn at a time.

## Keyboard Shortcuts

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

## CLI

| Flag | Description |
|------|-------------|
| `--agent <id>` | Agent identity — separate turns database + A2A mesh name (default: `slife`) |
| `--lang <en\|zh>` | TUI language — force English / Chinese (default: auto-detect from OS locale) |
| `<config-path>` | Positional — use a specific config file (its parent dir becomes the data dir) |

## Optional Extras

These embedding backends are **not installed by the one-click installer** — they are the optional `semantic memory search` setup (see the [Semantic Memory Search — Installation Guide](#semantic-memory-search--installation-guide) — pick one backend + the model). The table below is for manual installs (uvx / git checkout) or to re-add a backend:

| Extra | Enables |
|-------|---------|
| `local-embed[gguf]` | Local GGUF embeddings via llama-cpp-python (offline, ~300 MB) |
| `local-embed[transformer]` | HuggingFace transformer embeddings via sentence-transformers (~2 GB) |
| `slife[gguf]` / `slife[transformer]` / `slife[embeddings]` | Legacy in-process embeddings (not used by default) |

**Linux / macOS** — builds from source (extra packages via `uv pip install`):

```bash
uv pip install --python "$(uv tool dir)/slife" llama-cpp-python==0.3.34    # slife[gguf]
uv pip install --python "$(uv tool dir)/slife" sentence-transformers        # slife[transformer]
```

**Windows** — pre-built wheels (no C++ compiler needed); uv is configured to use the llama-cpp-python CPU wheel index. See [install docs](https://github.com/juzcn/slife#optional-extras) for wheel selection and first-use instructions.

## Development

For the canonical terminology, see [Glossary.md](Glossary.md); for the design
and architecture, see [DESIGN.md](DESIGN.md).

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

## Architecture

See **[DESIGN.md](DESIGN.md)** — design principles, agent loop, tool system, plugin contract, MCP gateway, memory database, A2A mesh, credential security model, and full project structure.

## License

MIT
