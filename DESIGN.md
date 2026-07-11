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

The current system prompt:

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

## Negative Space

- **Not a framework** — no agent composition, pipelines, or orchestration
- **Not a safety system** — no guardrails, approval gates, or sandboxing beyond the OS
- **Not a multi-agent system** — single conversation, single model, single loop
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in control.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  UI (Textual TUI)                                │
│  slife/ui/                                       │
├──────────────────────────────────────────────────┤
│  Agent Service                                   │
│  slife/agent/service.py                          │
├──────────────────────────────────────────────────┤
│  Agent Loop                                      │
│  slife/agent/loop.py — streaming function call   │
├────────────┬───────────────┬─────────────────────┤
│ Native     │ MCP           │ Skills              │
│ slife/     │ slife/mcp/    │ skills/             │
│ tools/     │ slife_mcp/    │                     │
├────────────┴───────────────┴─────────────────────┤
│  LLM Client (AsyncOpenAI)                        │
│  slife/agent/llm_client.py                       │
├──────────────────────────────────────────────────┤
│  Config (JSON5)                                  │
│  slife/config.py                                 │
└──────────────────────────────────────────────────┘
```

## Agent Loop

Single function-calling loop. All tools (native, MCP, skills) are registered as OpenAI functions in one ToolRegistry. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → Conversation.add_tool_result() → loop
    → no tool calls? → response text → return
```

- No hardcoded strategy, no preset workflows
- Tools are capabilities, the LLM is the decision maker
- Streaming output via `AgentEventHandler` callbacks
- MCP tools and native tools are equal — the LLM sees no difference

## Tool System

`Tool` interface: `name` / `description` / `parameters` (JSON Schema) / `async execute(**kwargs) -> str`

### Native Tools

| Tool | Config Type | Implementation |
|---|---|---|
| `execute_shell` | `"shell"` | `asyncio.create_subprocess_shell`, configurable timeout |
| `web_search` | `"serper"` | Serper.dev API via httpx |
| `get_shell_command` | `"platform"` | Platform-aware command builder (Win cmd / Unix bash) |
| `list_skills` / `use_skill` | `"skill"` | SKILL.md progressive disclosure (YAML frontmatter + markdown) |

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

**Connection pool** (`slife_mcp/connection.py`): raw asyncio JSON-RPC over subprocess pipes. No anyio, no `ClientSession` — avoids TaskGroup conflicts with FastMCP.

**Startup**: slife probes HTTP (`http://127.0.0.1:9876/mcp`) first. Falls back to spawning the wrapper as a child process via stdio.

**Standalone mode**: `uv run python -m slife_mcp.server --transport http --port 9876`

### Memory & Knowledge (planned)

- **Memory**: `memory_save` / `memory_recall` / `memory_list` — cross-session persistence, pluggable backends
- **Knowledge**: `knowledge_search` / `knowledge_index` — external document/code retrieval

## Project Structure

```
slife/
  agent/           # LLM client, conversation, function-calling loop
    loop.py        #   Function-calling while-loop
    llm_client.py  #   OpenAI-compatible streaming client
    conversation.py#   Message history (OpenAI format)
    service.py     #   Wiring: client + tools + loop + MCP
    system_prompt.py#  Jinja2 template rendering
  tools/           # Native tool implementations
    base.py        #   Tool ABC
    registry.py    #   Name → Tool lookup
    factory.py     #   Config type → Tool instances
    shell.py       #   execute_shell
    shell_command.py#  get_shell_command (platform-aware)
    serper.py      #   web_search (Serper.dev)
    skill.py       #   list_skills / use_skill
  mcp/             # MCP client integration
    client.py      #   stdio/HTTP client (asyncio.Queue adapters)
    tool_adapter.py#   MCP → slife Tool adapter
    process.py     #   Child process lifecycle manager
  ui/              # Textual TUI
    app.py         #   Main application
    chat.py        #   Message widgets
    handler.py     #   Streaming event → UI bridge
    tool_display.py#   Tool call rendering
  config.py        # JSON5 config loading (including MCPConfig)
  env.py           # ${ENV_VAR} resolution
  platform.py      # OS detection, shell syntax
slife_mcp/         # Independent MCP wrapper server
  server.py        #   FastMCP server with management tools
  connection.py    #   asyncio JSON-RPC connection pool
skills/            # Skill plugins
tests/             # pytest suite
```
