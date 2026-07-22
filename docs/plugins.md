# Slife Plugin Server Specification

The plugin system lets third-party developers add MCP tools to slife by
writing a single Python file.  Each plugin runs as a child process with a
FastMCP Streamable HTTP server — the harness spawns it, discovers its
port, connects, and registers its tools as LLM-visible functions.

---

## 1.  Architecture

```
┌─────────────────────┐     stdio (port signal)
│  slife (harness)    │◄────────────────────────────┐
│                     │                             │
│  AgentService       │     Streamable HTTP          │
│  ├─ MCPClient ──────┼───── http://127.0.0.1:N/mcp ─┼── FastMCP server
│  ├─ ToolRegistry ◄──┼── tools/list → proxy tools   │   (child process)
│  └─ AgentLoop       │                             │
└─────────────────────┘                             │
                                           ┌────────┴──────────┐
                                           │  plugin server.py │
                                           │  @mcp.tool(…)     │
                                           │  def main():      │
                                           │    run_plugin_svr │
                                           └───────────────────┘
```

1. The harness calls `python -m slife.plugins.<name>.server`
2. The plugin binds `127.0.0.1:0`, writes `{"port": <N>}` to stdout, closes stdout
3. The harness reads the port, connects via `MCPClient`, calls `tools/list`
4. Every tool is wrapped as an `MCPProxyTool` and registered in the LLM registry

---

## 2.  Minimal Plugin (10 lines)

```python
"""slife-my-plugin — does something useful."""

from slife.server_utils import create_plugin_server, run_plugin_server

mcp, _log_path, logger = create_plugin_server(
    "slife-my-plugin",
    instructions="Describe what this plugin does and how to use its tools.",
)

@mcp.tool(name="my_tool")
async def my_tool(query: str = "", limit: int = 10) -> str:
    """Search something and return results."""
    return f"Results for: {query} (limit={limit})"

def main():
    run_plugin_server(mcp)

if __name__ == "__main__":
    main()
```

That is **the entire plugin**.  No manual port binding, no logging setup,
no FastMCP boilerplate — `create_plugin_server` and `run_plugin_server`
handle all of it.

---

## 3.  Contract

### 3.1  File location

```
slife/plugins/<name>/server.py
```

The harness spawns it as `python -m slife.plugins.<name>.server`.
The module MUST have a `main()` function.

### 3.2  Required elements

| Element | How |
|---------|-----|
| FastMCP instance | `mcp, _log_path, logger = create_plugin_server("slife-<name>", instructions="…")` |
| Tools | Decorated with `@mcp.tool(name="<tool_name>")` |
| `main()` | `def main(): run_plugin_server(mcp)` |
| `if __name__` | `if __name__ == "__main__": main()` |

### 3.3  Tool naming

- Use `snake_case`: `my_search`, `fetch_page`, `send_alert`
- Avoid prefixes — the harness adds `<server>__<tool>` automatically
- Make the first line of the docstring a one-sentence summary (shown in tool lists)

### 3.4  Transport

Always `127.0.0.1` + Streamable HTTP.  Plugins are **never** exposed to the network.
`run_plugin_server` enforces this.

---

## 4.  `create_plugin_server()` — the factory

```python
from slife.server_utils import create_plugin_server

mcp, log_path, logger = create_plugin_server(
    "slife-memory",             # name → drives logger name + log suffix
    instructions="…",           # shown in server metadata
)
```

This single call replaces:

| Old boilerplate | Replaced by |
|-----------------|-------------|
| `setup_server_logging("_suffix")` | auto-derived from name |
| `logging.getLogger("slife_name")` | auto-derived from name |
| `FastMCP("name", instructions=…)` | handled internally |

Returns `(mcp, log_path, logger)` — everything the plugin needs.

---

## 5.  `run_plugin_server()` — the runner

```python
from slife.server_utils import run_plugin_server

def main():
    logger.info("my_plugin_start log=%s pid=%s", log_path, os.getpid())
    run_plugin_server(mcp)           # blocks until shutdown
    logger.info("my_plugin_stop")
```

Handles port binding → parent signalling → FastMCP startup.
Pass `port=<N>` to use a fixed port for debugging:

```python
run_plugin_server(mcp, port=9877)
```

---

## 6.  Logging

Every plugin gets two log channels automatically:

| Channel | Level | Destination |
|---------|-------|-------------|
| stderr | DEBUG | Drained by harness, relayed to its log file |
| File | DEBUG | `logs/YYYYMMDD_HHMMSS_<agent>_<suffix>.log` |

Use the `logger` returned by `create_plugin_server()`:

```python
mcp, _log_path, logger = create_plugin_server(…)

logger.info("something_happened key=%s", value)
logger.debug("detailed_diagnostic data=%s", data)
logger.warning("recoverable_issue detail=%s", detail)
logger.error("something_broken err=%s", e)
```

---

## 7.  The Lazy-Init Rule (CRITICAL)

**Never call `asyncio.run()` before `mcp.run()`.**

```python
# ❌  BROKEN — aiosqlite connection bound to a dead event loop
def main():
    asyncio.run(init_db())      # event loop #1 (destroyed after return)
    run_plugin_server(mcp)      # event loop #2 (FastMCP / uvicorn)
    # All DB operations hang forever — results can never be delivered
    # from loop #1's thread pool to loop #2.

# ✅  CORRECT — lazy-init on first use inside FastMCP's loop
_store = None
_lock = asyncio.Lock()

async def _ensure_store():
    global _store
    if _store is not None:
        return _store
    async with _lock:
        if _store is not None:
            return _store
        _store = await init_db()   # runs in FastMCP's event loop
        return _store

@mcp.tool(name="my_tool")
async def my_tool(arg: str = "") -> str:
    store = await _ensure_store()
    return await store.query(arg)
```

This applies to **all** async resources: `aiosqlite` connections,
`aiohttp` sessions, async Redis clients, etc.

---

## 8.  Auto-Discovery (zero config)

Plugins are discovered the same way as native tools — by scanning
``slife.plugins.*`` for packages containing ``server.py``::

    from slife.plugins import discover_plugins

    >>> discover_plugins()
    [("memory", "slife.plugins.memory.server"),
     ("mcp",    "slife.plugins.mcp.server"),
     ("wechat", "slife.plugins.wechat.server"),
     …]

The harness calls this at startup and spawns every discovered plugin.
**No ``slife.json5`` entry is needed.**  Drop a package into
``slife/plugins/my-plugin/`` and it is picked up on next launch.

Startup order
  ``memory`` always starts first (synchronously) so session restore
  can read from its database before the chat UI appears.  All other
  plugins start in parallel.

For external non‑Python MCP servers (npx, uvx, remote HTTP), use the
``mcp.servers`` config section — those are connected via the
``mcp_add_server`` tool, not auto‑discovery.

---

## 9.  Advanced Patterns

### 9.1  Plugin-specific CLI arguments

```python
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    global _db_path
    _db_path = Path(args.db) if args.db else _get_default_db_path()

    logger.info("my_plugin_start db=%s", _db_path)
    run_plugin_server(mcp, port=args.port)
    logger.info("my_plugin_stop")
```

### 9.2  Global state (connection pool, cache)

```python
# Module-level — shared across all tool invocations
_pool = ConnectionPool()
_cache: dict[str, Any] = {}

@mcp.tool(name="query")
async def query(term: str = "") -> str:
    if term in _cache:
        return _cache[term]
    result = await _pool.fetch(term)
    _cache[term] = result
    return result
```

### 9.3  Harness-only tools (not exposed to LLM)

Some tools are called programmatically by the harness, not by the LLM.
Name them clearly and document them:

```python
@mcp.tool(
    name="my_plugin_drain_incoming",
    description="Drain queued messages. Harness-only.",
)
async def my_plugin_drain_incoming() -> str:
    ...
```

The harness filters these out by name before registering LLM-visible tools.

---

## 10.  Testing & Debugging

### 10.1  Run the plugin standalone

```bash
uv run python -m slife.plugins.my_plugin.server --port 9877
```

Then test with curl:

```bash
curl -X POST http://127.0.0.1:9877/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### 10.2  Check logs

```bash
# Main harness log (includes plugin stderr)
tail -f logs/*_slife.log

# Plugin-specific log
tail -f logs/*_slife__my_plugin.log
```

### 10.3  Common issues

| Symptom | Likely cause |
|---------|-------------|
| Port signal timeout (10 s) | Plugin crashed before binding — check stderr in harness log |
| Tool calls hang forever | `asyncio.run()` used before `mcp.run()` — see lazy-init rule |
| Tools not appearing | Plugin started but `tools/list` failed — check plugin log |
| Connection refused | Plugin bound to wrong host — must be `127.0.0.1` |

---

## 11.  Built-in Plugin Reference

| Plugin | Module | Purpose |
|--------|--------|---------|
| slife-mcp | `slife.plugins.mcp.server` | MCP gateway — manage external MCP server connections |
| slife-memory | `slife.plugins.memory.server` | Turn-based long-term memory (SQLite + embeddings) |
| slife-wechat | `slife.plugins.wechat.server` | WeChat iLink bidirectional messaging |

These are the reference implementations — study them when building a new plugin.
