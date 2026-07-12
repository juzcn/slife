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
- That external MCP servers are managed via `mcp_add_server`
- That some MCP servers need user-provided configuration arguments and must not be called with empty args
- That `config_env_set` can write placeholders when a value isn't available yet

The current system prompt (`slife/agent/templates/system_prompt.j2`):

```
Use list_skills to discover available skills, then use_skill to load one.
When adding an MCP server via mcp_add_server, research its requirements first
-- don't pass empty args to servers that need configuration.
Set missing API keys or other env vars via config_env_set with a placeholder
in slife.json5 env: section.
```

### Design Principles

1. **Project-specific only.** If the LLM can infer it from tool schemas or training data, it doesn't belong here.

2. **Tool schemas over prompts.** Usage instructions live in function `description` and `parameters` — the prompt never repeats what a schema already says. `config_env_set`'s schema describes its parameters; the prompt only says *when* to use it.

3. **Don't block on missing values.** When a tool or server needs an API key the user doesn't have yet, set a placeholder and move on. Never make the user provide a key before installation can proceed. This is a behavioral rule the LLM wouldn't discover from schemas alone.

4. **Minimal is correct.** Every line must carry a fact the model has no other way to discover. If a line can be removed without losing project-specific knowledge, remove it.

5. **Not a job description.** No personality, no tone, no "you are a helpful assistant." The prompt is a lookup table for slife-specific conventions.

## Tool Schemas Over Prompts

Anything expressible in the function schema (`name`, `description`, `parameters`) stays in the function schema. The system prompt does not describe tools.

## Skills: Progressive Disclosure

Some capabilities require domain knowledge too long for a system prompt. Skills load that knowledge on demand via `list_skills` / `use_skill`, keeping context lean until the knowledge is needed.

Skills are discovered by scanning directories under `skills/` for `SKILL.md` files with YAML frontmatter (`name`, `description`). The shared `_iter_skills()` helper in `slife/tools/skill.py` handles directory scanning and frontmatter parsing once, used by both `get_skills_summary` and `_read_skill`.

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
├────────────┬──────────────────┬──────────────────────┤
│ Native     │ MCP Client       │ Skills               │
│ Tools      │ slife/mcp/       │ slife/tools/skill.py │
│ shell.py   │ client.py        │ skills/ directory    │
│ shell_     │ process.py       │                      │
│ command.py │ tool_adapter.py  │                      │
├────────────┴──────────────────┴──────────────────────┤
│  LLM Client (AsyncOpenAI)                            │
│  slife/agent/llm_client.py — streaming + thinking    │
├──────────────────────────────────────────────────────┤
│  Config (JSON5)                                      │
│  slife/config.py — env resolution, model parsing     │
└──────────────────────────────────────────────────────┘

                    slife agent ──── stdio/HTTP ──── slife-mcp
                    (slife/)                          (slife_mcp/)
                                                     Independent MCP proxy
                                                     pip install slife-mcp
```

## Agent Loop

Single function-calling loop. All tools (native, MCP, skills) are registered as OpenAI functions in one `ToolRegistry`. The LLM decides what to call and when.

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

### Native Tools

| Tool | Config Type | Implementation |
|---|---|---|
| `execute_shell` | `"shell"` | `asyncio.create_subprocess_shell`, configurable timeout |
| `get_shell_command` | `"platform"` | Platform-aware command builder (cmd.exe on Windows, bash on Unix) |
| `list_skills` / `use_skill` | `"skill"` | SKILL.md progressive disclosure (YAML frontmatter + markdown body) |

Tool loading (`slife/tools/factory.py`): `_TOOL_BUILDERS` maps config `type` strings to factory functions. Each builder receives the config entry dict and returns a `Tool` or list of `Tool` instances. Unknown types log a warning and are skipped.

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
3. `_auto_connect_mcp_servers()` — connect to pre-configured servers, discover external tools

**Wrapper connection**: slife always probes `mcp.wrapper.url` (default `http://127.0.0.1:9876/mcp`) first. If an HTTP wrapper is running, slife connects to it. If not, slife spawns the wrapper as a child process via stdio. The `wrapper_url` is always set — no guessing.

### slife-mcp side (`slife_mcp/`)

An independent FastMCP server. Auto-detects transport mode via `sys.stdin.isatty()`:

| stdin | Mode | Trigger |
|-------|------|---------|
| PIPE | stdio | Spawned by slife as child process |
| TTY  | HTTP | Run from terminal (`slife-mcp`) |

When run from a terminal, reads `mcp.wrapper.url` from `slife.json5` to determine host/port. `--host`/`--port` CLI flags override the config value.

**Management tools**: `mcp_add_server` / `mcp_remove_server` / `mcp_list_servers` / `mcp_list_tools` / `mcp_call_tool` / `mcp_reload`

**Connection pool** (`connection.py`): raw asyncio JSON-RPC over subprocess pipes. No anyio, no `ClientSession` — avoids TaskGroup conflicts with FastMCP.

External MCP servers use standard config format (compatible with Claude Desktop):
```json
{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]}
```

**Standalone package**: `slife_mcp/pyproject.toml` — published as `slife-mcp` on PyPI. Dependencies: `fastmcp` + `json5`. Entry point: `slife-mcp = slife_mcp.server:main`.

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
  tools/               # Native tool implementations
    base.py            #   Tool ABC with __init_subclass__ validation
    registry.py        #   Name → Tool lookup & execution
    factory.py         #   Config type → Tool instances (TOOL_BUILDERS)
    shell.py           #   execute_shell (subprocess with timeout)
    shell_command.py   #   get_shell_command (platform-aware)
    skill.py           #   list_skills / use_skill (shared _iter_skills)
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
