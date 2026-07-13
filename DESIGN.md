# slife Design

## Minimum Harness

The harness does only what the LLM physically cannot do:

1. **Execute tools** — the LLM requests function calls; the harness runs them and returns results.
2. **Maintain conversation state** — the harness holds the message list and feeds it back each turn.
3. **Stream responses** — the harness delivers tokens to the UI as they arrive.

Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job. The harness does not route, validate, retry, or second-guess.

## Lean System Prompt

**The system prompt contains only project-specific information not in the LLM's training data.**

The LLM already knows: function calling, how to read tool schemas, how to format tool calls, shell command syntax, error handling strategies, and what "assistant" means. Teaching any of this is noise.

What the LLM *cannot* know:

- The `list_skills` / `use_skill` flow — a slife-specific convention
- That `slife.json5` has an `env:` section for setting API keys and env vars
- That the pre-configured MCP servers (filesystem, fetch, duckduckgo-search)
	  need no auth — the LLM can use them immediately without asking for keys
- That external MCP servers are managed via `mcp_add_server`
- That MCP servers can be eager (tools auto-loaded) or lazy (tools browsed then loaded on demand via `mcp_set_disclosure`)
- That some MCP servers need user-provided configuration arguments and must not be called with empty args
- That `anyapi-mcp-server` is the recommended framework for connecting REST APIs
- That a commented github/anyapi-mcp-server template is in slife.json5 for when the user has a token
- That after successfully installing and using a new CLI, it should be registered via `cli_add_tool`
- That `config_env_set` can write placeholders when a value isn't available yet
The system prompt at `slife/agent/templates/system_prompt.j2` encodes these
facts in four short sections — Platform, Configuration, and Tools (Skills /
CLI / MCP / REST APIs).  That file is the authoritative source; the list
above documents the rationale behind each entry.

Every line is a **when-to-use** rule.  Tool capabilities live in schemas;
the prompt only tells the model which tool to reach for in each situation.
Tools whose use is obvious from their schema alone (e.g. `execute_shell`)
need no prompt entry.

### Design Principles

1. **Project-specific only.** If the LLM can infer it from tool schemas or training data, it doesn't belong here.

2. **Tool schemas over prompts.** Usage instructions live in function `description` and `parameters` — the prompt never repeats what a schema already says. `config_env_set`'s schema describes its parameters; the prompt only says *when* to use it.

3. **Don't block on missing values.** When a tool or server needs an API key the user doesn't have yet, set a placeholder and move on. Never make the user provide a key before installation can proceed. This is a behavioral rule the LLM wouldn't discover from schemas alone.

4. **Minimal is correct.** Every line must carry a fact the model has no other way to discover. If a line can be removed without losing project-specific knowledge, remove it.

5. **Not a job description.** No personality, no tone, no "you are a helpful assistant." The prompt is a lookup table for slife-specific conventions.

## Tool Schemas Over Prompts

Anything expressible in the function schema (`name`, `description`, `parameters`) stays in the function schema. The system prompt does not describe tools.

### Schema vs Prompt: A Clear Boundary

| Layer | Responsibility | Example |
|---|---|---|
| **Schema** (`description`, `parameters`) | **What the tool does** — its capability, inputs, outputs, side effects | "Write an environment variable to slife.json5 and inject it into os.environ immediately. If value is omitted, writes a <YOUR_KEY> placeholder." |
| **System prompt** | **When to use it** — the scenario, workflow rule, or project convention | "Call config_env_get before asking for an API key — it may already be set. When the user doesn't have a key yet, persist a placeholder with config_env_set." |

**Rules:**

1. **Schema never says "Always call this before…" or "Use when…"** — those are scenario rules. They belong in the system prompt.
2. **Prompt never repeats parameter details** — if the schema says `key` expects `UPPER_SNAKE_CASE`, the prompt doesn't say it again.
3. **Schema is self-contained** — a new model reading only the function definition should understand the tool's capability without guessing.
4. **Prompt is the workflow map** — it tells the model which tool to reach for in which situation, but not how the tool works.

**Before/after example — `config_env_set`:**

| | Before (mixed) | After (separated) |
|---|---|---|
| Schema description | "Persist an environment variable… Use when storing API keys, tokens, or other config the system needs at runtime." | "Persist an environment variable to slife.json5 and inject into os.environ immediately. Omit value to write a placeholder." |
| System prompt | (nothing about this scenario) | "Set missing API keys via config_env_set with a placeholder." |

The schema now describes pure capability. The prompt now tells the model *when* this tool is the right answer.

## Progressive Disclosure

Not all tools need to be in every LLM request. slife uses a two-level pattern for external capabilities: a lightweight summary tool always available, and a load tool that brings in the full capability on demand.

### Summary

| Category | Pattern | Summary Tool | Load Tool |
|---|---|---|---|
| **Skills** | Two-level, per-skill | `list_skills` | `use_skill` |
| **MCP** (incl. REST APIs) | Two-level, per-server | `mcp_list_tools` / `mcp_list_servers` | `mcp_set_disclosure("eager")` |
| **Native** | Always loaded | — | — |
| **CLI** | Metadata-only, no schema cost | — | — |

**Skills** and **MCP/REST** implement progressive disclosure because their content or tool count can be large — skills have long markdown bodies, MCP servers can expose dozens of tools. The summary tool returns a lightweight list; the load tool brings in the full capability.

**Native tools** are always registered — they're few (~10), each is essential, and their schemas are small.

**CLI tools** don't consume function definition slots — they're metadata entries in `slife.json5` that the LLM discovers via `cli_list_tools` (a text response). The actual execution goes through `execute_shell`.

### Skills

Some capabilities require domain knowledge too long for a system prompt. Skills load that knowledge on demand via `list_skills` / `use_skill`, keeping context lean until the knowledge is needed.

Skills are discovered by scanning directories under `skills/` for `SKILL.md` files with YAML frontmatter (`name`, `description`). The shared `_iter_skills()` helper in `slife/tools/skill.py` handles directory scanning and frontmatter parsing once, used by both `get_skills_summary` and `_read_skill`.

### MCP & REST APIs

MCP servers default to eager mode: all tools are discovered and registered at startup. Servers with many tools can be configured as lazy — they connect but don't register tools until the LLM explicitly requests them:

```json5
{ disclosure: "lazy" }   // connect but don't auto-register tools
```

The LLM workflow for lazy servers:
1. `mcp_list_servers` — see server is connected with `active: false`
2. `mcp_list_tools({server: "name"})` — browse available tools before deciding
3. `mcp_set_disclosure({name, disclosure: "eager"})` — load tools immediately

All disclosure changes take effect immediately:
- `eager` → registers tools, they appear in the next LLM request
- `lazy` → unregisters tools, saving context. Server process stays connected — switch back to eager to reload.
- `mcp_remove_server` → stops the server process, unregisters tools, persists removal. To recover, re-add the server.

REST APIs (via anyapi-mcp-server) are regular MCP servers — they use the same lazy/eager mechanism with no special handling.

## Negative Space

- **Not a framework** — no agent composition, pipelines, or orchestration
- **Not a safety system** — no guardrails, approval gates, or sandboxing beyond the OS
- **Not a multi-agent system** — single conversation, single model, single loop
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in control.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py│
├──────────────────────────────────────────────────────┤
│  Agent Service                                       │
│  slife/agent/service.py — wires client + tools + loop │
│  Manages MCP lifecycle: connect → register → discover │
├──────────────────────────────────────────────────────┤
│  Agent Loop                                          │
│  slife/agent/loop.py — streaming function-calling    │
│  Emits: thinking chunks, text chunks, tool events     │
├──────────┴──────────────┴──────────────┴─────────────────────┴──────────────────┤
│  Native Tools (auto-discovered from slife/tools/*)                               │
│  shell.py  run_python_script.py  os_info.py  config_env.py  cli.py  skill.py    │
│                                                                                  │
│  Skills          MCP Tools          RESTful API Tools                            │
│  skills/ dir     slife/mcp/         (via MCP + anyapi-mcp-server)                │
│  SKILL.md files  client.py         OpenAPI spec → MCP tools at runtime           │
│                  process.py                                                      │
│                  tool_adapter.py                                                 │
├──────────────────────────────────────────────────────────────────────────────────┤
│  LLM Client (AsyncOpenAI)                            │
│  slife/agent/llm_client.py — streaming + thinking    │
├──────────────────────────────────────────────────────┤
│  Config (JSON5)                                      │
│  slife/config.py — env resolution, model parsing     │
└──────────────────────────────────────────────────────┘

                    slife agent ──── stdio/HTTP ──── slife-mcp
                    (slife/)                          (slife_mcp/)
                                                     │
                                                     ├── filesystem MCP (npx)
                                                     ├── serper MCP (npx)
                                                     ├── anyapi-mcp-server (npx)
                                                     │     └── GitHub REST API
                                                     └── ... (any MCP server)
```

## Agent Loop

Single function-calling loop. All tools (native functions, skills, MCP, RESTful API, CLI) are registered as OpenAI functions in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → Conversation.add_tool_result() → loop
    → no tool calls? → response text → return
```

- No hardcoded strategy, no preset workflows
- Tools are capabilities, the LLM is the decision maker
- Streaming output via `AgentEventHandler` protocol callbacks
- MCP tools and native tools are equal — the LLM sees no difference
- Iteration limit (`max_iterations`) prevents infinite loops

## Tool System

`Tool` ABC (`slife/tools/base.py`): `name` / `description` / `parameters` (JSON Schema) / `async execute(**kwargs) -> str`

Validation happens at class definition time via `__init_subclass__` — every `Tool` subclass must define non-empty `name`, `description`, and `parameters`.

Tool loading (`slife/tools/factory.py`): all modules in `slife.tools.*` are imported via `pkgutil.iter_modules`, then `Tool.__subclasses__()` discovers every valid subclass automatically. No manual registry — new tools are picked up as soon as their module exists in the package. The `slife.json5` `tools` array is optional: use it only to override defaults (`{name: "execute_shell", timeout: 60}`) or disable a tool (`{name: "execute_shell", enabled: false}`). Each entry matches against `Tool.name` — every tool has a unique name, so overrides are always per-tool.

slife supports five categories of tools, all unified under the `Tool` ABC and registered in a single `ToolRegistry`. The LLM sees no difference between them — all are OpenAI function definitions.

### 1. Native Function Tools

Built-in tools implemented directly in Python, auto-discovered from `slife/tools/*.py`. Config overrides match by `Tool.name`:

| Tool | Implementation |
|---|---|
| `execute_shell` | `asyncio.create_subprocess_shell`, configurable timeout |
| `run_python_script` | Platform-correct Python invocation with JSON args |
| `get_os_info` | Return current OS name (Windows/Linux/macOS) |
| `config_env_set` | Write env vars to `slife.json5` + inject immediately |
| `config_env_get` | Read env vars from `slife.json5` |
| `config_env_remove` | Remove env vars from `slife.json5` + os.environ |
| `cli_add_tool` | Persist a CLI to `slife.json5` for future discovery |
| `cli_check_installed` | Check if CLIs are already registered in `slife.json5` |
| `cli_remove_tool` | Delete a CLI registration from `slife.json5` |
| `cli_list_tools` | List all registered CLI tools with descriptions |

### 2. Skills

On-demand documentation plugins using progressive disclosure (see Progressive Disclosure section above). Four tools in `slife/tools/skill.py`:

| Tool | Implementation |
|---|---|
| `list_skills` | Discover available SKILL.md files under `skills_dir` |
| `use_skill` | Load a skill's full markdown body into context |
| `add_skill` | Install a skill from files or a zip/tar.gz archive |
| `remove_skill` | Remove an installed skill |

Skills are discovered by scanning directories under `skills/` for `SKILL.md` files with YAML frontmatter (`name`, `description`). The shared `_iter_skills()` helper in `slife/tools/skill.py` handles directory scanning and frontmatter parsing once, used by all four skill tools.

### 3. MCP Tools

External MCP servers connected through [slife-mcp](https://pypi.org/project/slife-mcp/) — an independent MCP proxy service. Each external server's tools are adapted to the `Tool` ABC via `MCPProxyTool` and registered with a `{server}__` prefix (e.g. `filesystem__read_file`, `serper__search`).

MCP tools are not configured via `tools[]` — they are discovered dynamically when slife-mcp connects to configured servers. Supports progressive disclosure via `disclosure: "lazy"` (see Progressive Disclosure section above). See the MCP Integration section below for architecture details.

### 4. RESTful API Tools (anyapi-mcp-server)

REST APIs are converted to MCP tools via [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server), which generates tools from OpenAPI specifications at runtime. These are regular MCP servers from slife's perspective — configured in `mcp.servers` with `command: "npx"` and `args` specifying the `--spec` URL, `--base-url`, and any required headers.

Example — exposing the GitHub REST API as callable tools:
```json5
github: {
  command: "npx",
  args: [
    "-y", "anyapi-mcp-server",
    "--name", "github",
    "--spec", "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.yaml",
    "--base-url", "https://api.github.com",
    "--header", "Authorization: Bearer ${GITHUB_TOKEN}",
  ],
}
```

This produces tools like `github__list_repos`, `github__create_issue`, etc. — the LLM can call any endpoint described by the OpenAPI spec. The same pattern works for any REST API with an OpenAPI spec (Jira, GitLab, Slack, etc.).

### 5. CLI Tools

External CLI commands the LLM discovers at runtime and registers for future use — four management tools in `slife/tools/cli.py`:

| Tool | Description |
|---|---|
| `cli_add_tool` | Register a CLI with name, command, description, and optional install instructions |
| `cli_check_installed` | Check if CLI commands are already registered in `slife.json5` |
| `cli_remove_tool` | Remove a registered CLI |
| `cli_list_tools` | List all registered CLI tools |

This follows the same self-service pattern as `mcp_add_server`: after successfully installing and using a CLI (whether the LLM discovered it or the user asked for it), the LLM calls `cli_add_tool` to persist it. Registered CLIs survive restarts — stored in `slife.json5` → `cli_tools:`.

The tools themselves don't execute commands. The LLM uses `execute_shell` for that — these tools only manage the discovery registry. Design rationale: the LLM already knows how to run shell commands and parse `--help` output; the only thing it can't do is remember a CLI across sessions. That's what `cli_add_tool` provides.

## MCP Integration (slife-mcp)

slife-mcp is an **independent MCP proxy service** — it manages persistent connections to external MCP servers and exposes their tools through a single endpoint. It has zero dependency on slife and can be published as a standalone PyPI package (`pip install slife-mcp`).

```
               stdio / HTTP
slife agent  ←────────────→  slife-mcp (FastMCP)
                                  ├── filesystem MCP (npx)
                                  ├── serper MCP (npx)
                                  └── ... (any MCP server)
```

### slife side (`slife/mcp/`)

**MCPClient** (`client.py`): connects to the wrapper via stdio (child process) or HTTP (standalone). Uses `asyncio.Queue` adapters to bridge subprocess pipes to MCP's `ClientSession`. `disconnect()` decomposed into four phases: cancel bridge tasks, reset state, clean up transport, terminate owned process.

**MCPProxyTool** (`tool_adapter.py`): adapts MCP tools to slife's `Tool` ABC. Sets `name`/`description`/`parameters` at instance level via `object.__setattr__` (class-level attrs are placeholders for `__init_subclass__` validation). Tool names are prefixed with server name: `"filesystem__read_file"`.

**MCPWrapperProcess** (`process.py`): manages the wrapper child process lifecycle — start, create client from existing streams, graceful stop (stdin close → SIGTERM → SIGKILL escalation).

**Startup flow** (`AgentService.start_mcp`):
1. `_connect_mcp_wrapper()` — probe `wrapper_url`, connect via HTTP or fall back to spawning child process
2. `_register_mcp_wrapper_tools()` — discover wrapper management tools, create proxies
3. `_auto_connect_mcp_servers()` — connect to pre-configured servers in parallel; eager servers get their tools discovered immediately, lazy servers connect but skip registration

**Wrapper connection**: slife always probes `mcp.wrapper.url` (default `http://127.0.0.1:9876/mcp`) first. If an HTTP wrapper is running, slife connects to it. If not, slife spawns the wrapper as a child process via stdio. The `wrapper_url` is always set — no guessing.

### slife-mcp side (`slife_mcp/`)

An independent FastMCP server. Auto-detects transport mode via `sys.stdin.isatty()`:

| stdin | Mode | Trigger |
|-------|------|---------|
| PIPE | stdio | Spawned by slife as child process |
| TTY  | HTTP | Run from terminal (`slife-mcp`) |

When run from a terminal, reads `mcp.wrapper.url` from `slife.json5` to determine host/port. `--host`/`--port` CLI flags override the config value.

**Management tools**: `mcp_add_server` / `mcp_remove_server` / `mcp_list_servers` / `mcp_list_tools` / `mcp_check_server` / `mcp_set_disclosure` / `mcp_call_tool` / `mcp_reload`

**Connection pool** (`connection.py`): raw asyncio JSON-RPC over subprocess pipes. No anyio, no `ClientSession` — avoids TaskGroup conflicts with FastMCP.

External MCP servers use standard config format (compatible with Claude Desktop):
```json
{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]}
```

**Standalone package**: `slife_mcp/pyproject.toml` — published as `slife-mcp` on PyPI. Dependencies: `fastmcp` + `json5`. Entry point: `slife-mcp = slife_mcp.server:main`.

### RESTful API via anyapi-mcp-server

[anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server) is an MCP server that converts any OpenAPI specification into callable MCP tools at runtime. It's configured like any other MCP server in `mcp.servers`, with `--spec` pointing to the API's OpenAPI spec and `--base-url` set to the API's base endpoint:

```json5
github: {
  command: "npx",
  args: [
    "-y", "anyapi-mcp-server",
    "--name", "github",
    "--spec", "https://raw.githubusercontent.com/.../api.github.com.yaml",
    "--base-url", "https://api.github.com",
    "--header", "Authorization: Bearer ${GITHUB_TOKEN}",
  ],
}
```

Each OpenAPI endpoint becomes a tool named `{name}__{operationId}` (e.g. `github__repos_list_for_authenticated_user`). The LLM can call any endpoint described by the spec — no per-endpoint code needed.

This pattern works for any REST API with an OpenAPI specification: Jira, GitLab, Slack, Stripe, etc. The system prompt guides the LLM to use this framework when users ask to connect a REST API.

## Config Loading

`Config.from_json5()` (`slife/config.py`) parses the JSON5 file in structured phases:

1. **Models**: `_parse_models_section()` dispatches between provider-dict and flat-list formats. `_parse_provider_models()` handles provider defaults inheritance and duplicate detection.
2. **Env**: `_parse_section()` extracts typed sections with default fallbacks. Env vars are injected into `os.environ` so tools can reference them via `${VAR}`.
3. **Tools & MCP**: Parsed with the same `_parse_section()` helper, eliminating repetitive isinstance+fallback blocks.

`${ENV_VAR}` and `${ENV_VAR:-default}` resolution (`slife/env.py`) works recursively through dicts and lists.

**MCPConfig**: `wrapper_url` always has a value (default `http://127.0.0.1:9876/mcp`). From config: `mcp.wrapper.url`. MCP is enabled when servers are configured, `enabled: true` is explicit, or a custom wrapper is defined.

## UI

Textual TUI in **Claude Code CLI style**: minimal chrome, dark theme, clean message display.

- **ChatView** — scrollable message container (user, assistant, system messages)
- **AssistantMessage** — streaming text with optional thinking block (dim italic, truncated at 500 chars)
- **ToolCallWidget** — collapsible tool call display with header line (amber) and detail block. Single `Static` widget, no child widgets — all rendering via `Content` trees. User data goes through `Content.from_text(markup=False)` for safety.
- **TUIHandler** — bridges `AgentEventHandler` callbacks to Textual widgets
- **StatusBar** — shows model name, thinking indicator, token count, key bindings

All user-facing text (tool output, search results, file contents) is rendered with `markup=False` to prevent `MarkupError` from special characters (`&`, `[`, `]`).

## Project Structure

```
slife/
  agent/               # LLM client, conversation, function-calling loop
    loop.py            #   Function-calling while-loop with streaming
    llm_client.py      #   OpenAI-compatible streaming client
    conversation.py    #   Message history (OpenAI format)
    service.py         #   Wiring: client + tools + loop + MCP
    system_prompt.py   #   Jinja2 template rendering
    multimodal.py      #   Image encoding, /file attachment parsing
  tools/               # Tool implementations (5 categories, auto-discovered)
    base.py            #   Tool ABC with __init_subclass__ validation
    registry.py        #   Name → Tool lookup & execution
    factory.py         #   Auto-discovery via pkgutil + __subclasses__()
    shell.py           #   execute_shell (subprocess with timeout)
    run_python_script.py  #   run_python_script (platform-aware)
    os_info.py         #   get_os_info (current OS)
    skill.py           #   list_skills / use_skill / add_skill / remove_skill
    config_env.py      #   config_env_set / get / remove
    cli.py             #   cli_add_tool / cli_check_installed / cli_remove_tool / cli_list_tools
  mcp/                 # MCP client (slife side)
    client.py          #   stdio/HTTP client with asyncio.Queue adapters
    tool_adapter.py    #   MCP → slife Tool adapter (MCPProxyTool)
    process.py         #   Child process lifecycle manager
  ui/                  # Textual TUI
    app.py             #   Main application (SlifeApp)
    chat.py            #   Message widgets (ChatView, AssistantMessage)
    handler.py         #   Streaming event → UI bridge (TUIHandler)
    tool_display.py    #   Tool call rendering (ToolCallWidget)
  config.py            # JSON5 config loading (ModelConfig, MCPConfig, Config)
  env.py               # ${ENV_VAR} and ${ENV_VAR:-default} resolution
  platform.py          # OS detection, shell syntax (Windows/Unix)
slife_mcp/             # Independent MCP proxy (publishable as slife-mcp)
  server.py            #   FastMCP server, auto-detect HTTP/stdio
  connection.py        #   asyncio JSON-RPC connection pool
  pyproject.toml       #   Standalone package config
  README.md            #   Standalone package docs
skills/                # Skill plugins (on-demand documentation)
tests/                 # pytest suite (331 tests, asyncio_mode=strict)
```
