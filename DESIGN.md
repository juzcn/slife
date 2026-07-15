# slife Design

## Minimum Harness

The harness does only what the LLM physically cannot do:

1. **Execute tools** — the LLM requests function calls; the harness runs them and returns results.
2. **Maintain conversation state** — the harness holds the message list and feeds it back each turn.
3. **Stream responses** — the harness delivers tokens to the UI as they arrive.
4. **Persist memory** — the harness saves every message, thinking block, and tool output so nothing is lost. The LLM decides what to recall and when.

Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job. The harness does not route, validate, retry, or second-guess.

## Lean System Prompt

**The system prompt contains only project-specific information not in the LLM's training data.**

The prompt is rendered from `slife/agent/templates/system_prompt.j2` via Jinja2. When `--agent <id>` is provided, the template receives `agent_name` and injects it: `` You are slife (your name is Freud) `` — giving each agent instance a distinct identity without adding noise when no name is set.

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
- That `a2a_list_agents` and `a2a_list_subagents` discover remote and local agents
- That `a2a_send_task` can delegate work to any agent (local subagent or remote MQTT peer)
- That `a2a_spawn_subagent` creates local workers for parallel computation
- That every conversation is permanently recorded — use `memory_search` to find past discussions, errors, and decisions before starting new work
- That `memory_search` has three modes: `grep` for exact strings (error messages, code), `fts5` for keyword topics, `hybrid` for semantic similarity

The system prompt at `slife/agent/templates/system_prompt.j2` encodes these
facts in short sections — Platform, Configuration, and Tools (Skills /
CLI / MCP / REST APIs / A2A / Memory).  That file is the authoritative source; the list
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
| **Memory** | Two-level | `memory_list_recent` / `memory_search` | `memory_open` |
| **Skills** | Two-level, per-skill | `list_skills` | `use_skill` |
| **MCP** (incl. REST APIs) | Two-level, per-server | `mcp_list_tools` / `mcp_list_servers` | `mcp_set_disclosure("eager")` |
| **Native** | Always loaded | — | — |
| **CLI** | Metadata-only, no schema cost | — | — |

**Memory** tools follow the same pattern: `memory_list_recent` and `memory_search` return lightweight results (titles, summaries, snippets). `memory_open` loads the full conversation. `memory_search` supports three modes — `grep` (exact substring), `fts5` (keyword ranking), `hybrid` (fts5 + semantic vector search with RRF merge).

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
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in control — including of multi-agent coordination via A2A.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py│
├──────────────────────────────────────────────────────┤
│  Agent Service                                       │
│  slife/agent/service.py — wires client+tools+loop    │
│  Manages MCP, Memory, A2A/MQTT, and subagent         │
│  Inbox: serializes human + MQTT + subagent messages   │
├──────────────────────────────────────────────────────┤
│  Agent Loop                                          │
│  slife/agent/loop.py — streaming function-calling    │
│  Emits: thinking chunks, text chunks, tool events     │
│  Conversation: full context + context window trimming  │
├──────────┴──────────────┴──────────────┴─────────────────────┴──────────────────┤
│  Native Tools (auto-discovered from slife/tools/*)                               │
│  shell.py  run_python_script.py  os_info.py  config_env.py  cli.py  skill.py    │
│                                                                                  │
│  Memory Tools    Skills        MCP Tools        A2A Tools                        │
│  slife_memory/   skills/ dir   slife/mcp/      slife/a2a/                       │
│  server.py       SKILL.md      client.py       MQTT + subagent                  │
│  store.py                      process.py      slife/subagent/                  │
│                                tool_adapter.py                                   │
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
         slife agent ──── stdio/HTTP ──── slife-memory
         (slife/)                          (slife_memory/)
                                           │
                                    ~/.slife/slife.db
                                      ├── diary
                                      ├── diary_fts (FTS5)
                                      └── diary_semantic (vec0)

         slife agent ──── MQTT ────────── mosquitto ─── other slife instances
         (slife/)         P2P mesh                        (remote peers)

         slife agent ──── JSON-RPC 2.0 ─── subagent (headless)
         (parent)           stdin/stdout        (child process)
```

## Agent Loop

Single function-calling loop. All tools (native functions, skills, MCP, RESTful API, CLI, memory) are registered as OpenAI functions in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → Conversation.add_tool_result() → loop
    → no tool calls? → response text → return
    → save turn to diary (permanent memory)
    → trim context if > 80% window (oldest turns → diary, keep 20%)
```

- No hardcoded strategy, no preset workflows
- Tools are capabilities, the LLM is the decision maker
- Streaming output via `AgentEventHandler` protocol callbacks
- MCP tools, native tools, and memory tools are equal — the LLM sees no difference
- Iteration limit (`max_iterations`) prevents infinite loops
- **Context window management**: when the conversation exceeds 80% of the model's context window, the oldest turns are trimmed down to 20%. Trimmed content is archived in permanent memory — the LLM can recall it via `memory_search` when needed.

### Context Window Management

The active conversation stays within 20%–80% of the model's context window:

```
                context_window (e.g. 131072 tokens)
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary)    │  current context      │  headroom  │
│   recall via            │  20% ~ 80%            │  20%       │
│   memory_search         │  LLM's working memory │            │
└──────────────────────────────────────────────────────────────┘
                           ↑                      ↑
                       floor=0.2             ceiling=0.8
```

- **Save**: after each turn, the full conversation (including thinking + tool outputs) is written to the diary.
- **Trim**: if tokens exceed `context_ceiling × window`, oldest complete turns are removed until tokens ≤ `context_floor × window`. Only the active Conversation is trimmed — the diary retains everything.
- **Recall**: `memory_search` excludes the active diary (`status != '进行中'`), but trimmed content from the current session is in the diary and can be found.

Configure in `slife.json5`:
```json5
agent: {
    max_iterations: 10,
    context_floor: 0.2,    // trim down to 20%
    context_ceiling: 0.8,  // trigger trim at 80%
}
```

## Permanent Memory (slife-memory)

Conversations, tool outputs, errors, thinking — everything the agent sees is permanently recorded like a diary. The memory service runs as an **independent MCP process**, symmetrical to slife-mcp.

### Why a Separate Process

```
slife crash ──→ slime-memory still alive ──→ marks diary as '意外中断'
                                              │
slife restart ──→ memory_open_diary() ──→ "Found interrupted session. Restore?"
```

If memory were in-process, a crash would race with the final write. A separate process observes the disconnection and marks the crash — no race window.

### Architecture

```
                         MCP protocol
slife agent ───────────────┼────────────────
         │                 │                │
    stdio/HTTP        stdio/HTTP       stdio/HTTP
         │                 │                │
    ┌─────────┐    ┌──────────────┐    ┌──────────────┐
    │slife-mcp│    │slife-memory  │    │  future MCP  │
    │ port    │    │ port 9877    │    │  services…   │
    │ 9876    │    │              │    │              │
    └─────────┘    └──────┬───────┘    └──────────────┘
                          │
                   ~/.slife/slife.db
                     ├── diary            (one row = one conversation)
                     ├── diary_fts (FTS5) (keyword search, BM25 ranking)
                     └── diary_semantic   (vec0, cosine KNN)
```

### Diary Schema — Designed for LLM Readability

```sql
CREATE TABLE diary (
    author         TEXT,     -- who (--user flag, default "default")
    title          TEXT,     -- conversation title
    created_at     TEXT,     -- when it started
    updated_at     TEXT,     -- last update
    status         TEXT,     -- '进行中' | '已完成' | '意外中断'

    messages       TEXT,     -- full OpenAI-format conversation JSON
                             -- includes: system prompt, user input,
                             -- thinking blocks, tool calls + arguments,
                             -- tool outputs, final responses

    summary        TEXT,     -- 1-2 sentence gist of the conversation
    tags           TEXT,     -- comma-separated topic tags
    key_moments    TEXT,     -- important decisions, bugs found, insights

    who_helped     TEXT,     -- agent name (--agent flag)
    what_model     TEXT,     -- model used

    how_many_turns   INTEGER,
    how_many_tokens  INTEGER
);
```

One row = one complete conversation. `messages` is a JSON array of all messages including thinking — a single `SELECT` restores the full state.

### Memory Tools

All tools take an `author` parameter for user isolation (`--user` on CLI, defaults to `"default"`).

| Tier | Tool | What it does |
|---|---|---|
| **summary** | `memory_open_diary` | Start new conversation or detect interrupted one |
| **summary** | `memory_close_diary` | Mark conversation complete, optionally add summary/tags |
| **summary** | `memory_list_recent` | Flip through recent diary entries (titles + summaries) |
| **summary** | `memory_update_diary` | Save conversation after each turn |
| **search** | `memory_search` | Three modes: `grep` (exact string), `fts5` (keyword), `hybrid` (keyword + semantic) |
| **load** | `memory_open` | Read a full conversation by rowid |
| **load** | `memory_summarize` | Write title, summary, tags, key moments |
| **load** | `memory_forget` | Delete a diary entry |

### Search Modes

| Mode | Backend | Best for | Example |
|---|---|---|---|
| `grep` | SQLite LIKE | Exact strings, error messages, code snippets | `"ConnectionError: timeout"` |
| `fts5` | FTS5 index + BM25 | Topic/keyword search | `"MCP连接问题"` |
| `hybrid` | FTS5 + vec0 KNN → RRF | Semantic similarity, fuzzy recall | `"上次怎么修的那个连接泄漏"` |

All modes exclude the current active diary (`status != '进行中'`) — the LLM doesn't need to "recall" what's already in its context window.

### Embeddings

Semantic search (`hybrid` mode) uses vector embeddings. Two configurable backends:

```json5
memory: {
    embedding: {
        // Backend 1: local GGUF model (offline, no API cost)
        gguf_path: "/path/to/bge-m3-q4_k_m.gguf",
        model: "bge-m3",
        dim: 1024,

        // Backend 2: OpenAI-compatible API (falls back when no gguf_path)
        // model: "text-embedding-3-small",
    }
}
```

If no embedding backend is configured, `hybrid` mode degrades to `fts5`-only — keyword search still works.

### What Gets Saved

Every turn, the full `Conversation.messages` list is written to the diary:

| Content | In diary? | In API calls? |
|---|---|---|
| System prompt | ✅ | ✅ |
| User input | ✅ | ✅ |
| Assistant thinking | ✅ | ❌ (stripped by `to_openai_messages()`) |
| Tool call name + arguments | ✅ | ✅ |
| Tool execution output | ✅ | ✅ |
| Assistant final response | ✅ | ✅ |
| Image attachments | ✅ | ✅ |

Thinking is stored in a `thinking` field on assistant messages — preserved for memory recall, stripped before sending to the API (not a standard OpenAI message field).

### Crash Recovery

```
slife startup
  → start_memory()           // connect or spawn slife-memory
  → memory_open_diary()      // check for status='进行中'
    ├─ none found → fresh diary created
    └─ found! → returns interrupted=true + session info
         → TUI shows recovery prompt:
           ⚡ 发现中断的对话
             「重构工具系统」
             12 轮对话 · 2026-07-15 11:45
             状态：意外中断 · 58,023 tokens
             /restore — 从中断处继续
             /discard — 丢弃，开始新对话
             /preview — 查看对话内容
```

On restore: messages are deserialized from JSON → `Conversation` rebuilt → UI widgets rendered → LLM can continue exactly where it left off.

## Tool System

`Tool` ABC (`slife/tools/base.py`): `name` / `description` / `parameters` (JSON Schema) / `async execute(**kwargs) -> str`

Validation happens at class definition time via `__init_subclass__` — every `Tool` subclass must define non-empty `name`, `description`, and `parameters`.

Tool loading (`slife/tools/factory.py`): all modules in `slife.tools.*` are imported via `pkgutil.iter_modules`, then `Tool.__subclasses__()` discovers every valid subclass automatically. No manual registry — new tools are picked up as soon as their module exists in the package. The `slife.json5` `tools` array is optional: use it only to override defaults (`{name: "execute_shell", timeout: 60}`) or disable a tool (`{name: "execute_shell", enabled: false}`). Each entry matches against `Tool.name` — every tool has a unique name, so overrides are always per-tool.

slife supports six categories of tools, all unified under the `Tool` ABC and registered in a single `ToolRegistry`. The LLM sees no difference between them — all are OpenAI function definitions.

### 1. Memory Tools

Permanent conversation storage with hybrid search (`slife_memory/`). Eight tools — four summary-level (always loaded), one search, three load-level. Memory tools are implemented as an independent MCP service, discovered by slife through the same `MCPClient` + `MCPProxyTool` pattern as all other MCP tools. The LLM uses them to search past conversations, recall context, and manage the diary.

| Tool | Tier |
|---|---|
| `memory_open_diary` / `memory_close_diary` | summary |
| `memory_list_recent` / `memory_update_diary` | summary |
| `memory_search` (grep / fts5 / hybrid) | search |
| `memory_open` / `memory_summarize` / `memory_forget` | load |

### 2. Native Function Tools

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

### 3. A2A Tools

14 auto-discovered tools in `slife/tools/a2a.py` implementing the full A2A protocol — discovery, task routing, lifecycle, and notifications. Shared helpers `_get_transports()` and `_require_params()` eliminate per-tool boilerplate. Transport routing is subagent-first (fast, local), MQTT fallback (network).

| Tool | A2A method | Description |
|------|-----------|-------------|
| `a2a_list_agents` | — | Discover remote MQTT peers (requires A2A) |
| `a2a_list_subagents` | — | List local subagent workers |
| `a2a_send_task` | message/send | Send task and wait for result (sync) |
| `a2a_send_task_async` | — | Fire-and-forget with task ID for polling |
| `a2a_get_task_result` | tasks/get | Poll task status from shared TaskStore |
| `a2a_list_tasks` | tasks/list | Filterable task listing across all agents |
| `a2a_cancel_task` | tasks/cancel | Best-effort cancellation |
| `a2a_subscribe_task` | tasks/subscribe | Block until task completion (event-driven/poll) |
| `a2a_agent_card` | — | Agent introspection (local or remote) |
| `a2a_spawn_subagent` | — | Create local worker with same LLM + tools |
| `a2a_stop_subagent` | — | Stop a locally-managed subagent |
| `a2a_notify_user` | — | Fire desktop notification to operator |
| `a2a_broadcast` | — | Scatter/gather — send to all known agents |

### 4. Skills

On-demand documentation plugins using progressive disclosure (see Progressive Disclosure section above). Four tools in `slife/tools/skill.py`:

| Tool | Implementation |
|---|---|
| `list_skills` | Discover available SKILL.md files under `skills_dir` |
| `use_skill` | Load a skill's full markdown body into context |
| `add_skill` | Install a skill from files or a zip/tar.gz archive |
| `remove_skill` | Remove an installed skill |

Skills are discovered by scanning directories under `skills/` for `SKILL.md` files with YAML frontmatter (`name`, `description`). The shared `_iter_skills()` helper in `slife/tools/skill.py` handles directory scanning and frontmatter parsing once, used by all four skill tools.  Common `__init__` and `from_config` logic is extracted into the `_SkillDirMixin` class, avoiding duplication across `ListSkillsTool`, `UseSkillTool`, `AddSkillTool`, and `RemoveSkillTool`.

### 5. MCP Tools

External MCP servers connected through [slife-mcp](https://pypi.org/project/slife-mcp/) — an independent MCP proxy service. Each external server's tools are adapted to the `Tool` ABC via `MCPProxyTool` and registered with a `{server}__` prefix (e.g. `filesystem__read_file`, `serper__search`).

MCP tools are not configured via `tools[]` — they are discovered dynamically when slife-mcp connects to configured servers. Supports progressive disclosure via `disclosure: "lazy"` (see Progressive Disclosure section above). See the MCP Integration section below for architecture details.

### 6. RESTful API Tools (anyapi-mcp-server)

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

### 7. CLI Tools

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

**MCPProxyTool** (`tool_adapter.py`): adapts MCP tools to slife's `Tool` ABC. Sets `name`/`description`/`parameters` at instance level via `object.__setattr__` (class-level attrs are placeholders for `__init_subclass__` validation). Tool names are prefixed with server name: `"filesystem__read_file"`. Used by both slife-mcp and slife-memory — same adapter, different servers.

**MCPWrapperProcess** (`process.py`): manages the wrapper child process lifecycle — start, create client from existing streams, graceful stop (stdin close → SIGTERM → SIGKILL escalation). Generic — used for both slife-mcp and slife-memory.

**Startup flow** (`AgentService.start_mcp`):
1. `_connect_mcp_wrapper()` — probe `wrapper_url`, connect via HTTP or fall back to spawning child process
2. `_register_mcp_wrapper_tools()` — discover wrapper management tools, create proxies
3. `_auto_connect_mcp_servers()` — connect to pre-configured servers in parallel; eager servers get their tools discovered immediately, lazy servers connect but skip registration

The same pattern is used for `start_memory()` — probe → connect HTTP or spawn stdio → discover tools → create proxies.

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

## A2A — Agent-to-Agent

Two transports, unified interface. The LLM sees one agent pool.

### Architecture

```
                    a2a_list_agents / a2a_send_task
                           │
            ┌──────────────┴──────────────┐
            │                             │
     MQTT Transport              Subagent Transport
     (--agent enables)            (always available)

  ┌─────────────────┐       ┌──────────────────────┐
  │ MQTT Broker      │       │ Parent Process        │
  │ (mosquitto)      │       │  SubagentManager      │
  │                  │       │  ├─ sub-1 (headless)  │
  │ slife/+/presence │       │  │  JSON-RPC stdin/stdout
  │ slife/+/inbox    │       │  ├─ sub-2 (headless)  │
  │ slife/+/result   │       │  │  JSON-RPC stdin/stdout
  └─────────────────┘       │  └─ ...               │
                            └──────────────────────┘
```

### MQTT Transport (`slife/a2a/`)

Remote slife instances discover each other and delegate tasks over MQTT.
Enabled via `--agent <id>` CLI flag. The `mqtt` config section provides
broker connection details only — it never auto-enables A2A.

- **MQTTAdapter** (`mqtt.py`): paho-mqtt → asyncio.Queue bridge with LWT
- **A2AClient** (`client.py`): presence heartbeat, peer discovery, task routing
- **BrokerManager** (`broker.py`): optional mosquitto auto-spawn
- **TaskStore** (`a2a/task_store.py`): shared task-lifecycle tracking — records every send/result/cancel across both transports with status, timestamps, and result text. Singleton via `get_store()`.
- **Inbox** (`agent/inbox.py`): unified message queue — serializes human + MQTT + subagent messages through `Inbox.run()` background processor. `ConversationStore` manages per-agent conversation state (persistent for human, one-shot for remotes).
- **Inbox listener** uses `MQTTAdapter.messages()` async iterators (the same pattern as the peer watchdog loop) to consume inbox and result queues. Two forwarder tasks merge both streams into a single queue, avoiding the task-creation/cancellation cycle that previously leaked orphaned `asyncio.Queue.get()` tasks and silently consumed inbound messages.

### Remote Task UI Integration

When a remote task arrives via MQTT, it appears in the chat view **exactly like a locally-typed message**: the source agent's name becomes the prompt prefix (`Jack> task…`), the LLM's thinking and response stream to the chat, and tool calls render as collapsible widgets. This is achieved via:

- **Handler factory** — `start_a2a()` accepts a `handler_factory` callable that creates fresh `TUIHandler` instances per task. The factory is set before the inbox starts, so even the first remote task has a handler.
- **ConversationStore fallback** — `handler_for()` falls back: source-specific handler → human handler → default factory. This guarantees a handler is always available.
- **UserMessage prefix** — `UserMessage` accepts a `prefix` parameter (default `"> "`). Human messages use the agent's own name, remote messages use the source agent's name. Built as a single Rich `Content` with range styling to avoid Content-concatenation newline artifacts.

### Subagent Transport (`slife/subagent/`)

Local child-process workers spawned via `asyncio.create_subprocess_exec`.
Always available — no config toggle needed.

- **headless.py**: slife without TUI, JSON-RPC 2.0 over stdin/stdout
- **process.py**: `SubagentProcess` (pipe bridge + task dispatch), `SubagentManager` (spawn/stop/list)
- **tools.py**: `a2a_spawn_subagent`, `a2a_stop_subagent` lifecycle tools
- JSON-RPC methods: `tasks/send`, `shutdown`

### Unified Tools (`slife/tools/a2a.py`)

All 14 A2A tools are auto-discovered `Tool` subclasses in `slife/tools/a2a.py`.
They resolve transports lazily at call time via two shared helpers:
`_get_transports()` returns `(manager, client)` from module-level references
set by `AgentService` at startup; `_require_params()` validates required
arguments.  This eliminates the repeated three-line import stanza that
previously appeared in every tool's `execute()` method.

The tools use a consistent routing pattern — subagent first (fast, local),
then MQTT fallback (network).  The LLM never needs to know which transport
a given agent uses.

### Protocol

Subagent IPC uses JSON-RPC 2.0 per the A2A specification (§9):

```
→ {"jsonrpc":"2.0","method":"tasks/send","params":{"task":"…"},"id":"x"}
← {"jsonrpc":"2.0","result":"…","id":"x"}
```

MQTT transport uses topic-based publish/subscribe with the same task semantics.

### Nested Prevention

Subagents set `SLIFE_SUBAGENT_NAME` in their environment. `start_subagent()`
checks for this and skips subagent manager creation to prevent recursive spawning.

## User Isolation

Multiple users on the same machine are isolated by `--user`:

```bash
slife --user alice              # alice's diary, alice's memories
slife --user bob --agent bob    # bob's diary + A2A identity "bob"
slife                            # default user, no A2A
```

`--user` and `--agent` are orthogonal:
- `--user` → memory isolation key (who owns the diary)
- `--agent` → A2A network identity (who I am on the MQTT mesh)

Every memory tool takes an `author` parameter. The `diary` table uses `author` as the primary isolation column. `diary_semantic` (vec0) uses `author` as a partition key — KNN search is automatically scoped to one user with zero cross-user overhead.

## Config Loading

`Config.from_json5()` (`slife/config.py`) parses the JSON5 file in structured phases:

1. **Models**: `_parse_models_section()` dispatches between provider-dict and flat-list formats. `_parse_provider_models()` handles provider defaults inheritance and duplicate detection.
2. **Env**: `_parse_section()` extracts typed sections with default fallbacks. Env vars are injected into `os.environ` so tools can reference them via `${VAR}`.
3. **Tools & MCP**: Parsed with the same `_parse_section()` helper, eliminating repetitive isinstance+fallback blocks.

`${ENV_VAR}` and `${ENV_VAR:-default}` resolution (`slife/env.py`) works recursively through dicts and lists.

**MCPConfig**: `wrapper_url` always has a value (default `http://127.0.0.1:9876/mcp`). From config: `mcp.wrapper.url`. MCP is enabled when servers are configured, `enabled: true` is explicit, or a custom wrapper is defined.

**MemoryConfig**: enabled by default (`memory.enabled: true`). `db_path` defaults to `~/.slife/slife.db`. Embedding backend auto-detected: local GGUF takes priority over API; if neither is configured, semantic search degrades gracefully (FTS5 still works).

## UI

Textual TUI in **Claude Code CLI style**: minimal chrome, dark theme, clean message display.

- **ChatView** — scrollable message container (user, assistant, system messages)
- **UserMessage** — configurable prompt prefix; defaults to `> ` but becomes `Jack> ` when `--agent Jack` is set, giving each agent instance a visible identity in the chat. Remote tasks from other agents use the source name as prefix.
- **AssistantMessage** — streaming text with optional thinking block (dim italic, truncated at 500 chars). Click to expand, Enter/Space to toggle collapse.
- **ToolCallWidget** — collapsible tool call display with header line (amber) and detail block. Single `Static` widget, no child widgets — all rendering via `Content` trees. User data goes through `Content.from_text(markup=False)` for safety.
- **TUIHandler** — bridges `AgentEventHandler` callbacks to Textual widgets
- **StatusBar** — shows model name, thinking indicator, token count, key bindings
- **Recovery prompt** — on startup with an interrupted session, displays session info and offers `/restore` `/discard` `/preview` slash commands

All user-facing text (tool output, search results, file contents) is rendered with `markup=False` to prevent `MarkupError` from special characters (`&`, `[`, `]`).

## Project Structure

```
slife/
  agent/               # LLM client, conversation, function-calling loop
    loop.py            #   Function-calling while-loop with streaming
    llm_client.py      #   OpenAI-compatible streaming client
    conversation.py    #   Message history + context window trimming
    service.py         #   Wiring: client + tools + loop + MCP + Memory + A2A
    system_prompt.py   #   Jinja2 template rendering
    multimodal.py      #   Image encoding, /file attachment parsing
    inbox.py           #   Unified message queue (human + MQTT + subagent)
  a2a/                 # A2A: agent identity, MQTT, broker, TaskStore
    identity.py        #   AgentId, AgentMessage
    card.py            #   AgentCard
    client.py          #   A2AClient — MQTT P2P mesh
    mqtt.py            #   MQTTAdapter — paho-mqtt → asyncio bridge
    broker.py          #   BrokerManager — mosquitto lifecycle
    task_store.py      #   TaskRecord + TaskStore — task lifecycle tracking
    config.py          #   A2AConfig (enabled via --agent)
    tools.py           #   Tool re-exports for backward compatibility
  subagent/            # Subagent: local child processes
    headless.py        #   JSON-RPC 2.0 runner (no TUI)
    process.py         #   SubagentProcess + SubagentManager
    tools.py           #   a2a_spawn_subagent, a2a_stop_subagent re-exports
  tools/               # Tool implementations (7 categories, auto-discovered)
    base.py            #   Tool ABC with __init_subclass__ validation
    registry.py        #   Name → Tool lookup & execution
    factory.py         #   Auto-discovery via pkgutil + __subclasses__()
    a2a.py             #   14 A2A protocol tools (unified transports)
    shell.py           #   execute_shell (subprocess with timeout)
    run_python_script.py  #   run_python_script (platform-aware)
    os_info.py         #   get_os_info (current OS)
    skill.py           #   list_skills / use_skill / add_skill / remove_skill
    config_env.py      #   config_env_set / get / remove
    cli.py             #   cli_add_tool / cli_check_installed / cli_remove_tool / cli_list_tools
    _config_io.py      #   Shared JSON5 read/write helpers
  mcp/                 # MCP client (slife side — shared by slife-mcp & slife-memory)
    client.py          #   stdio/HTTP client with asyncio.Queue adapters
    tool_adapter.py    #   MCP → slife Tool adapter (MCPProxyTool)
    process.py         #   Child process lifecycle manager
  ui/                  # Textual TUI
    app.py             #   Main application (SlifeApp) — memory + recovery UI
    chat.py            #   Message widgets (ChatView, AssistantMessage)
    handler.py         #   Streaming event → UI bridge (TUIHandler)
    tool_display.py    #   Tool call rendering (ToolCallWidget)
    commands.py        #   Slash-command handling (/exit, /restore, etc.)
    command_palette.py #   Slash-command completion dropdown
  config.py            # JSON5 config loading (ModelConfig, MCPConfig, MemoryConfig, Config)
  env.py               # ${ENV_VAR} and ${ENV_VAR:-default} resolution
  platform.py          # OS detection, shell syntax (Windows/Unix)
  logfmt.py            # Structured logging with session/request IDs
slife_mcp/             # Independent MCP proxy (publishable as slife-mcp)
  server.py            #   FastMCP server — 8 management tools
  connection.py        #   asyncio JSON-RPC connection pool
  pyproject.toml       #   Standalone package config
  README.md            #   Standalone package docs
slife_memory/          # Independent memory MCP service (publishable as slife-memory)
  server.py            #   FastMCP server — 8 memory tools
  store.py             #   SQLite + FTS5 + vec0 hybrid search
  embeddings.py        #   GGUF local or OpenAI API embedding backend
  search.py            #   RRF (Reciprocal Rank Fusion) merge
  schema.sql           #   DDL — diary table + FTS5 + vec0
  pyproject.toml       #   Standalone package config
  README.md            #   Standalone package docs
skills/                # Skill plugins (on-demand documentation)
tests/                 # pytest suite (577 tests, asyncio_mode=strict)
```

## The Knowledge Base Effect

A side effect of the memory architecture: **everything the agent sees becomes a knowledge base**. Tool outputs — file contents, web search results, API responses, error messages — are all stored in `diary.messages` and indexed by FTS5 + vec0. Over time, the diary becomes a searchable archive of everything the agent has encountered.

The LLM can search its own past observations:
- `memory_search(mode="grep", query="ConnectionError")` — find every past occurrence of a specific error
- `memory_search(mode="fts5", query="MCP连接问题")` — find past discussions about a topic
- `memory_search(mode="hybrid", query="那次修好的内存泄漏")` — find the conversation where a bug was fixed

No separate knowledge base, no vector database, no indexing pipeline. The diary IS the knowledge base — every tool output, every thinking trace, every decision is recorded in its original context and searchable through the same interface.
