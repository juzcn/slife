# slife-memory

Permanent memory MCP service for [slife](https://github.com/juzcn/slife) — records every conversation like a diary with hybrid keyword + semantic search.

## Architecture

```
slife agent ←── MCP ──→ slife-memory
                           │
                     ~/.slife/slife.db
                       ├── diary (conversations)
                       ├── diary_fts (FTS5 keyword search)
                       └── diary_semantic (sqlite-vec semantic search)
```

slife-memory is an **independent MCP service** — same pattern as slife-mcp. It runs as a child process (stdio) or standalone HTTP server. slife connects via MCP protocol, discovers its tools, and registers them as proxy tools.

## Quick Start

```bash
# As a child process (slife handles this automatically)
uv run python -m slife_memory.server

# As a standalone HTTP service
uv run python -m slife_memory.server
# → Reads memory.url from slife.json5 for host/port
```

## Tools

All tools take an `author` parameter for user isolation (maps to `--user` in slife).

| Tool | Tier | What it does |
|---|---|---|
| `memory_open_diary` | summary | Start a new conversation or detect an interrupted one |
| `memory_close_diary` | summary | Mark conversation as complete, optionally add summary |
| `memory_list_recent` | summary | Browse recent diary entries (titles + summaries) |
| `memory_update_diary` | summary | Save messages after each turn |
| `memory_search` | search | Hybrid search (FTS5 + vec0 → RRF merge) |
| `memory_open` | load | Read a full conversation by rowid |
| `memory_summarize` | load | Write title/summary/tags/key-moments |

### Progressive Disclosure

Tools follow the same tiered pattern as slife's skills and MCP tools:

1. **Summary tier** (always loaded): open/close/list/update — lightweight, always available
2. **Search tier** (loaded when needed): `memory_search` — hybrid keyword + semantic
3. **Load tier** (loaded on demand): `memory_open`, `memory_summarize`

## Configuration

In `slife.json5`:

```json5
{
  memory: {
    enabled: true,
    url: "http://127.0.0.1:9877/mcp",
    db_path: "~/.slife/slife.db",
    embedding: {
      model: "text-embedding-3-small",
    },
  }
}
```

Embedding credentials are inherited from `models.providers` — same API key as the chat model.

## Recovery Flow

```
slife startup
  → memory_open_diary(author="alice")
    → checks for status='进行中'
    → if found: returns interrupted=true + session info
    → if not: creates fresh diary entry
```

If a previous session is found (interrupted or completed), slife prompts the user to restore or start fresh.

## Requirements

- Python ≥ 3.13
- SQLite ≥ 3.41 (for vec0 LIMIT support)

## License

MIT
