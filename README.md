# slife

Terminal-based AI agent — a function-calling loop with minimum harness. Chat with an LLM that can execute shell commands, search the web, load on-demand skills, and connect to MCP servers.

## Quick Start

```bash
# Install
uv sync

# Configure
cp slife.json5.example slife.json5
# Edit slife.json5 — set your API keys via ${ENV_VAR} references

# Run
uv run slife
```

## Configuration

Edit `slife.json5`. Key sections:

```json5
{
  models: {
    providers: {
      deepseek: {
        base_url: "https://api.deepseek.com",
        api_key: "${DEEPSEEK_API_KEY}",
        models: [
          { model: "deepseek-v4-flash", name: "DeepSeek V4 Flash" },
          { model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true },
        ],
      },
    },
  },
  active_model: "deepseek/deepseek-v4-pro",
  agent: { max_iterations: 10 },
  tools: [
    { type: "platform" },
    { type: "shell", timeout: 30 },
    { type: "serper" },
    { type: "skill", skills_dir: "skills" },
  ],

  // MCP integration (optional)
  mcp: {
    servers: {
      "filesystem": {
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"],
      },
    },
  },
}
```

API keys use `${ENV_VAR}` syntax — set them in your environment, not in the config file.

## Tools

| Tool | Type | What it does |
|------|------|-------------|
| `execute_shell` | `shell` | Run shell commands on the host machine |
| `get_shell_command` | `platform` | Translate intent into OS-correct shell syntax |
| `web_search` | `serper` | Google Search via Serper.dev API |
| `list_skills` | `skill` | List available skill plugins |
| `use_skill` | `skill` | Load a skill's documentation into context |

Add or remove tools from the `tools[]` list to control what the agent can do.

### MCP Integration

slife can use tools from any MCP-compatible server. The slife-mcp wrapper manages persistent connections to external MCP servers:

```
slife agent ←→ slife-mcp wrapper ←→ external MCP servers
```

Configure servers under `mcp.servers` using standard MCP format (compatible with Claude Desktop configs). The wrapper exposes management tools (`mcp_add_server`, `mcp_list_tools`, `mcp_call_tool`, etc.) to control connections at runtime.

**Two modes:**

| Mode | Command | When to use |
|------|---------|-------------|
| Child process (default) | Auto-started by slife on launch | Normal use — no manual setup |
| Standalone HTTP | `uv run python -m slife_mcp.server --transport http --port 9876` | Share MCP connections across clients, or debug independently |

**Standalone CLI flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--transport` | `stdio` | `stdio` (child process) or `http` (standalone) |
| `--port` | `9876` | HTTP listen port |
| `--host` | `127.0.0.1` | HTTP listen address |

On startup, slife probes `http://127.0.0.1:9876/mcp` first — if the wrapper is already running standalone, slife connects to it instead of spawning a child process.

See [DESIGN.md](DESIGN.md) for architecture details.

## Skills

Skills are on-demand documentation plugins. The agent loads them only when needed, keeping the context lean.

```
skills/baidu-search/
  SKILL.md              # Instructions the agent reads
  scripts/search.py     # Supporting code
```

Flow: the agent calls `list_skills` → sees what's available → calls `use_skill("baidu-search")` to load full instructions.

To add a skill, create a directory under `skills/` with a `SKILL.md` file.

## Tips

- **`/file image.png`** — attach an image for vision models
- **`Ctrl+C`** — clear the conversation
- **`Ctrl+Q`** — quit

## Design

slife is a **minimum-harness agent**. The harness only does three things the LLM cannot: execute tools, maintain conversation state, and stream responses. Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job.

The system prompt is intentionally lean. It only contains project-specific information not in the LLM's training data. The LLM already knows how function calling works.

See [DESIGN.md](DESIGN.md) for the full design rationale.

## Project Structure

```
slife/
  agent/           # Core agent loop, LLM client, conversation
    loop.py        #   Function-calling while-loop
    llm_client.py  #   OpenAI-compatible streaming client
    conversation.py#   Message history (OpenAI format)
    service.py     #   Wiring: client + tools + loop + MCP
    system_prompt.py#  Jinja2 template rendering
  tools/           # Extensible tool system
    base.py        #   Tool ABC
    registry.py    #   Name → Tool lookup
    factory.py     #   Config type → Tool instances
    shell.py       #   execute_shell
    shell_command.py#  get_shell_command (platform-aware)
    serper.py      #   web_search (Serper.dev)
    skill.py       #   list_skills / use_skill
  mcp/             # MCP client integration
    client.py      #   stdio/HTTP client
    tool_adapter.py#   MCP → slife Tool adapter
    process.py     #   Child process lifecycle
  ui/              # Textual TUI
    app.py         #   Main application
    chat.py        #   Message widgets
    handler.py     #   Streaming event → UI bridge
    tool_display.py#   Tool call rendering
  config.py        # JSON5 config loading
  env.py           # ${ENV_VAR} resolution
  platform.py      # OS detection, shell syntax
slife_mcp/         # Independent MCP wrapper server
  server.py        #   FastMCP server with management tools
  connection.py    #   asyncio JSON-RPC connection pool
skills/            # Skill plugins
tests/             # pytest suite
```

## Requirements

- Python ≥ 3.13
- `uv` (Python package manager)
- Node.js (only if using npx-based MCP servers like filesystem)

## License

MIT
