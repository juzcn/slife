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
- That `slife.json5` has an `env:` section for setting API keys
- That external tools are managed via `mcp_add_server` / `mcp_list_tools` / `mcp_call_tool`

The current system prompt (`slife/agent/templates/system_prompt.j2`):

```
Use list_skills to discover available skills, then use_skill to load one.
If an API key or environment variable is missing, guide the user to set it in slife.json5 under the env: section.
Manage external MCP tools via mcp_add_server, mcp_list_tools, and mcp_call_tool.
```

It is not a job description, not a manual, not a tutorial. It's a lookup table for facts the model has no other way to discover.

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
│ Native     │ MCP              │ Skills               │
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

### MCP Integration

slife-mcp wrapper (`slife_mcp/server.py`) is an independent MCP server process:

```
slife agent ←→ slife-mcp wrapper (FastMCP, stdio/HTTP)
                   ├── fs (filesystem MCP, via npx)
                   ├── brave-search (via npx)
                   └── ... (any MCP server)
```

**Wrapper management tools**: `mcp_add_server` / `mcp_remove_server` / `mcp_list_servers` / `mcp_list_tools` / `mcp_call_tool` / `mcp_reload`

External MCP servers use standard config format (compatible with Claude Desktop):
```json
{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]}
```

**MCPClient** (`slife/mcp/client.py`): connects to the wrapper via stdio (child process) or HTTP (standalone). Uses `asyncio.Queue` adapters to bridge subprocess pipes to MCP's `ClientSession`. `disconnect()` is decomposed into four phases: cancel bridge tasks, reset state, clean up transport, terminate owned process.

**MCPProxyTool** (`slife/mcp/tool_adapter.py`): adapts MCP tools to slife's `Tool` ABC. Sets `name`/`description`/`parameters` at instance level via `object.__setattr__` (class-level attrs are placeholders for `__init_subclass__` validation). Tool names are prefixed with server name: `"filesystem__read_file"`.

**MCPWrapperProcess** (`slife/mcp/process.py`): manages the wrapper child process lifecycle — start, create client from existing streams, graceful stop (stdin close → SIGTERM → SIGKILL escalation).

**Startup flow** (`AgentService.start_mcp`):
1. `_connect_mcp_wrapper()` — probe HTTP, fall back to spawning child process
2. `_register_mcp_wrapper_tools()` — discover wrapper management tools, create proxies
3. `_auto_connect_mcp_servers()` — connect to pre-configured servers, discover external tools

**Connection pool** (`slife_mcp/connection.py`): raw asyncio JSON-RPC over subprocess pipes. No anyio, no `ClientSession` — avoids TaskGroup conflicts with FastMCP.

**Standalone mode**: `uv run python -m slife_mcp.server --transport http --port 9876`

## Config Loading

`Config.from_json5()` (`slife/config.py`) parses the JSON5 file in structured phases:

1. **Models**: `_parse_models_section()` dispatches between provider-dict and flat-list formats. `_parse_provider_models()` handles provider defaults inheritance and duplicate detection.
2. **Env**: `_parse_section()` extracts typed sections with default fallbacks. Env vars are injected into `os.environ` so tools can reference them via `${VAR}`.
3. **Tools & MCP**: Parsed with the same `_parse_section()` helper, eliminating repetitive isinstance+fallback blocks.

`${ENV_VAR}` and `${ENV_VAR:-default}` resolution (`slife/env.py`) works recursively through dicts and lists.

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
  mcp/                 # MCP client integration
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
slife_mcp/             # Independent MCP wrapper server (FastMCP)
  server.py            #   Management tools & HTTP/stdio transport
  connection.py        #   asyncio JSON-RPC connection pool
skills/                # Skill plugins (on-demand documentation)
tests/                 # pytest suite (326 tests, asyncio_mode=strict)
```
