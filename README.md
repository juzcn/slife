# slife

Terminal-based AI agent — a function-calling loop with minimum harness. Chat with an LLM that can execute shell commands, search the web, load on-demand skills, connect to MCP servers, and call any REST API via OpenAPI specs.

## Quick Start

```bash
# Install
uv sync

# Configure
cp slife.json5.example slife.json5
# Edit slife.json5 — set your model's API key via ${DEEPSEEK_API_KEY}
# The example includes the minimum bootstrap MCP servers — ready to use.

# Run
uv run slife
```

## Configuration

Edit `slife.json5`.  The only key you need is your LLM provider's API key — the minimum bootstrap MCP servers (filesystem, fetch) need no auth.

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

  // Minimum bootstrap MCP servers (no API keys needed)
  mcp: {
    servers: {
      filesystem: {
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."],
        description: "Local filesystem — read, write, list files.",
      },
      fetch: {
        command: "uvx",
        args: ["mcp-server-fetch"],
        description: "Fetch web pages and convert to markdown.",
      },
      // github: { … } — uncomment when you have GITHUB_TOKEN
    },
  },
}
```

## Tools

slife supports four categories of tools. All are unified as OpenAI function definitions — the LLM sees no difference between them.

### 1. Native Functions

All tools in `slife/tools/` are auto-discovered at startup — no `tools[]` config required. Use `slife.json5` only to override defaults (e.g. shell timeout) or disable a tool.

| Tool | What it does |
|------|-------------|
| `execute_shell` | Execute a shell command and return its stdout and stderr |
| `run_python_script` | Build a platform-correct shell command for Python with JSON args |
| `get_os_info` | Return current OS: Windows, Linux, or macOS |
| `config_env_set` | Write an env var to slife.json5 and inject into os.environ |
| `config_env_get` | Read env vars from slife.json5 |
| `config_env_remove` | Delete an env var from slife.json5 and os.environ |
| `cli_add_tool` | Persist a CLI to slife.json5 for future discovery |
| `cli_remove_tool` | Delete a CLI registration from slife.json5 |
| `cli_list_tools` | List all registered CLI tools with descriptions |

### 2. Skills

On-demand documentation plugins — the agent loads them only when needed. Four tools auto-discovered from `slife/tools/skill.py`:

| Tool | What it does |
|------|-------------|
| `list_skills` | List all installed skills with names and descriptions |
| `use_skill` | Return the full SKILL.md content for a named skill |
| `add_skill` | Write skill files or extract an archive to the skills directory |
| `remove_skill` | Delete a skill directory and all its contents |

Skills live under `skills/` — each is a directory with a `SKILL.md` file. See the [Skills](#skills) section below.

### 3. MCP Tools

Tools from external MCP servers (filesystem, web search, fetch, etc.) connected through [slife-mcp](https://pypi.org/project/slife-mcp/). Configure servers in `mcp.servers` and tools are discovered automatically at startup. Each server's tools are prefixed with the server name (e.g. `filesystem__read_file`, `serper__search`).

See [MCP Integration](#mcp-integration) below.

### 4. RESTful API Tools

Any REST API with an OpenAPI spec becomes callable via [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server) — a core capability pre-configured as a commented template in `slife.json5`. Uncomment the `github` block when you have a `GITHUB_TOKEN`, or call `mcp_add_server` to connect other APIs (Jira, GitLab, Slack, Stripe…) using the same pattern:

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

| Tool | What it does |
|------|-------------|
| `cli_add_tool` | Register a CLI with name, description, and install instructions |
| `cli_remove_tool` | Remove a registered CLI |
| `cli_list_tools` | List all registered CLIs |

Registered CLIs are persisted to `slife.json5` → `cli_tools:`. The tools themselves don't execute commands — the LLM uses `execute_shell` for that. They just ensure the LLM remembers specialized CLIs across sessions.

All native tools are auto-discovered at startup. Use `slife.json5`'s optional `tools` array only to override defaults or disable individual tools by name. MCP and RESTful API tools are managed through `mcp.servers` configuration.

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
  tools/               # Extensible tool system (5 categories, auto-discovered)
    base.py            #   Tool ABC with __init_subclass__ validation
    registry.py        #   Name → Tool lookup & execution
    factory.py         #   Auto-discovery via pkgutil + __subclasses__()
    shell.py           #   execute_shell (subprocess with timeout)
    run_python_script.py  #   run_python_script (platform-aware)
    os_info.py         #   get_os_info (current OS)
    skill.py           #   list_skills / use_skill / add_skill / remove_skill
    config_env.py      #   config_env_set / get / remove
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
