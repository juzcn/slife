# slife

Terminal-based AI agent — a function-calling loop with minimum harness. Chat with an LLM that can execute shell commands, load on-demand skills, connect to MCP servers, and call any REST API via OpenAPI specs.

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
    { type: "skill", skills_dir: "skills" },
    { type: "config_env" },
    { type: "cli_manager" },
  ],

  // MCP integration (optional)
  mcp: {
    // wrapper.url — slife probes this first, falls back to child process
    wrapper: {
      url: "http://127.0.0.1:9876/mcp",
    },
    servers: {
      "filesystem": {
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"],
        description: "Local filesystem operations — read, write, list files.",
      },
    },
  },
}
```

API keys use `${ENV_VAR}` syntax — set them in your environment, not in the config file.

## Tools

slife supports four categories of tools. All are unified as OpenAI function definitions — the LLM sees no difference between them.

### 1. Native Functions

Built-in tools configured via `tools[]` in `slife.json5`:

| Tool | Config Type | What it does |
|------|-------------|-------------|
| `execute_shell` | `shell` | Run shell commands on the host machine |
| `get_shell_command` | `platform` | Translate intent into OS-correct shell syntax |
| `config_env_set` | `config_env` | Set env vars in slife.json5 |
| `config_env_get` | `config_env` | Read env vars from slife.json5 |
| `config_env_remove` | `config_env` | Remove env vars from slife.json5 |

### 2. Skills

On-demand documentation plugins — the agent loads them only when needed:

| Tool | Config Type | What it does |
|------|-------------|-------------|
| `list_skills` | `skill` | List available skill plugins |
| `use_skill` | `skill` | Load a skill's documentation into context |

Skills live under `skills/` — each is a directory with a `SKILL.md` file. See the [Skills](#skills) section below.

### 3. MCP Tools

Tools from external MCP servers (filesystem, web search, fetch, etc.) connected through [slife-mcp](https://pypi.org/project/slife-mcp/). Configure servers in `mcp.servers` and tools are discovered automatically at startup. Each server's tools are prefixed with the server name (e.g. `filesystem__read_file`, `serper__search`).

See [MCP Integration](#mcp-integration) below.

### 4. RESTful API Tools

Any REST API with an OpenAPI spec becomes callable via [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server). Configure it as an MCP server pointing to the API's spec:

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

This produces tools like `github__list_repos`, `github__create_issue`, etc. Works with any REST API that has an OpenAPI spec (Jira, GitLab, Slack, Stripe…).

### 5. CLI Tools

On-demand CLI discovery. The LLM runs `--help` to learn any unfamiliar CLI, then registers it for future sessions:

| Tool | Config Type | What it does |
|------|-------------|-------------|
| `cli_add_tool` | `cli_manager` | Register a CLI with name, description, and install instructions |
| `cli_remove_tool` | `cli_manager` | Remove a registered CLI |
| `cli_list_tools` | `cli_manager` | List all registered CLIs |

Registered CLIs are persisted to `slife.json5` → `cli_tools:`. The tools themselves don't execute commands — the LLM uses `execute_shell` for that. They just ensure the LLM remembers specialized CLIs across sessions.

Add or remove native tools from the `tools[]` list to control what the agent can do. MCP and RESTful API tools are managed through `mcp.servers` configuration.

### MCP Integration

slife uses tools from any MCP-compatible server via [slife-mcp](https://pypi.org/project/slife-mcp/) — an independent MCP proxy that manages persistent connections:

```
slife agent ←→ slife-mcp ←→ external MCP servers (filesystem, search, REST APIs…)
```

**Two ways to run slife-mcp:**

| Mode | How | Description |
|------|-----|-------------|
| Child process | Auto-started by slife | No setup needed — slife spawns it via stdio |
| Standalone | `slife-mcp` | Independent HTTP service, share across clients |

**Standalone usage:**

```bash
pip install slife-mcp

# Run (auto-detects HTTP/stdio mode)
slife-mcp                      # TTY + slife.json5 → HTTP
slife-mcp --port 8888          # Custom port
slife-mcp --host 0.0.0.0       # Listen on all interfaces
```

When the wrapper is running standalone, slife probes `mcp.wrapper.url` on startup and connects via HTTP instead of spawning a child process. If nothing is listening, it falls back to spawning its own.

**RESTful APIs** are connected through MCP using [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server), which converts OpenAPI specs to MCP tools at runtime. Add it to `mcp.servers` with `--spec`, `--base-url`, and any required `--header` values. The LLM can then call any endpoint in the spec — no per-endpoint code needed.

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
- **`Ctrl+L`** — clear the conversation
- **`Ctrl+C`** — quit
- **`Esc`** — focus the input field

## Design

slife is a **minimum-harness agent**. The harness only does three things the LLM cannot: execute tools, maintain conversation state, and stream responses. Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job.

The system prompt is intentionally lean. It only contains project-specific information not in the LLM's training data.

See [DESIGN.md](DESIGN.md) for the full design rationale.

## Project Structure

```
slife/
  agent/               # Core agent loop, LLM client, conversation
    loop.py            #   Function-calling while-loop with streaming
    llm_client.py      #   OpenAI-compatible streaming client
    conversation.py    #   Message history (OpenAI format)
    service.py         #   Wiring: client + tools + loop + MCP
    system_prompt.py   #   Jinja2 template rendering
    multimodal.py      #   Image encoding, /file attachment parsing
  tools/               # Extensible tool system (5 categories)
    base.py            #   Tool ABC with __init_subclass__ validation
    registry.py        #   Name → Tool lookup & execution
    factory.py         #   Config type → Tool instances (TOOL_BUILDERS)
    shell.py           #   execute_shell (subprocess with timeout)
    shell_command.py   #   get_shell_command (platform-aware)
    skill.py           #   list_skills / use_skill (progressive disclosure)
    config_env.py      #   config_env_set/get/remove
    cli.py             #   cli_add_tool / cli_remove_tool / cli_list_tools
  mcp/                 # MCP client (slife side)
    client.py          #   stdio/HTTP client with asyncio.Queue adapters
    tool_adapter.py    #   MCP → slife Tool adapter (MCPProxyTool)
    process.py         #   Child process lifecycle manager
  ui/                  # Textual TUI (Claude Code CLI style)
    app.py             #   Main application
    chat.py            #   Message widgets
    handler.py         #   Streaming event → UI bridge
    tool_display.py    #   Tool call rendering (expandable widgets)
  config.py            # JSON5 config loading (ModelConfig, MCPConfig, Config)
  env.py               # ${ENV_VAR} and ${ENV_VAR:-default} resolution
  platform.py          # OS detection, shell syntax (Windows/Unix)
slife_mcp/             # Independent MCP proxy service (publishable package)
  server.py            #   FastMCP server with management tools
  connection.py        #   asyncio JSON-RPC connection pool
  pyproject.toml       #   Standalone package config (pip install slife-mcp)
skills/                # Skill plugins (on-demand documentation)
tests/                 # pytest suite (331 tests, asyncio_mode=strict)
```

## Requirements

- Python ≥ 3.13
- `uv` (Python package manager)
- Node.js (only if using npx-based MCP servers)

## License

MIT
