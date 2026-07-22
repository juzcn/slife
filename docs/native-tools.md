# Slife Native Tool Specification

Native tools are Python classes that run **in-process** with the agent loop —
no network, no subprocess, no serialization.  Every LLM-visible tool is a
`Tool` subclass auto-discovered from the `slife/tools/` package.

---

## 1.  Architecture

```
┌─────────────────────────────────────────┐
│  AgentLoop                              │
│  ├─ LLMClient  (streaming API)          │
│  ├─ ToolRegistry                        │
│  │   ├─ ShellTool          (native)     │
│  │   ├─ GetOsInfoTool      (native)     │
│  │   ├─ MCPProxyTool       (plugin)     │
│  │   └─ …                              │
│  └─ Conversation                        │
└─────────────────────────────────────────┘

Discovery (startup):
  slife/tools/__init__.py
  → factory.create_tools_from_config()
    → pkgutil.iter_modules("slife.tools")
      → import every *.py
        → Tool.__subclasses__()
          → register(cls.from_config(cfg, config))
```

Every `.py` file in `slife/tools/` is imported at startup.  Any `Tool`
subclass found in the module is automatically registered — no decorator,
no manual wiring, no config entry required.

---

## 2.  Minimal Tool (4 attributes + 1 method)

```python
"""Return information about the operating system."""

from slife.tools.base import Tool, NO_PARAMS
from slife.platform import get_os_info

class GetOsInfoTool(Tool):
    name = "get_os_info"
    description = "Return the current operating system: Windows, Linux, or macOS."
    parameters = NO_PARAMS

    async def execute(self, **kwargs) -> str:
        return get_os_info()
```

That is a complete, working tool.  Place it in `slife/tools/my_tool.py`
and it's automatically available to the LLM on next startup.

---

## 3.  Contract

### 3.1  Required class attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Unique tool ID (`snake_case`). Used as the function name in the LLM API. |
| `description` | `str` | One-sentence summary of what the tool does. Shown in tool lists. |
| `parameters` | `dict` | JSON Schema for function arguments. Use `make_params()` or `NO_PARAMS`. |
| `execute()` | `async → str` | The tool's implementation. Receives keyword arguments matching the schema. |

### 3.2  Optional class attributes

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `requires_a2a` | `bool` | `False` | Only register when the MQTT agent mesh is active |
| `_subagent_skip` | `bool` | `False` | Hide from subagent workers (e.g. tools that spawn more subagents) |

### 3.3  Optional classmethod

```python
@classmethod
def from_config(cls, cfg: dict, config: Config | None) -> "Tool":
    """Factory — used when the tool needs constructor parameters."""
    return cls(timeout=cfg.get("timeout", 30))
```

Override this when your tool needs `__init__` arguments (timeout, directory
paths, API clients).  The `cfg` dict comes from the `tools:` array in
`slife.json5`.

---

## 4.  `make_params()` — Schema Builder

Build JSON Schema from keyword field definitions.  Fields **without**
`"default"` are automatically marked `required`:

```python
from slife.tools.base import Tool, make_params

class SearchTool(Tool):
    name = "search"
    description = "Search the web and return results."
    parameters = make_params(
        query={"type": "string", "description": "Search query."},
        limit={"type": "integer", "description": "Max results.", "default": 10},
        region={"type": "string", "description": "Region code.", "default": "us-en"},
    )

    async def execute(self, query: str = "", limit: int = 10, region: str = "us-en", **kwargs) -> str:
        # query is required (no default in schema)
        # limit and region are optional
        ...
```

This generates:

```json
{
  "type": "object",
  "properties": {
    "query":  {"type": "string",  "description": "Search query."},
    "limit":  {"type": "integer", "description": "Max results.",   "default": 10},
    "region": {"type": "string",  "description": "Region code.",   "default": "us-en"}
  },
  "required": ["query"]
}
```

### 4.1  `NO_PARAMS` — No-argument tools

```python
from slife.tools.base import NO_PARAMS

parameters = NO_PARAMS
# Equivalent to: {"type": "object", "properties": {}, "required": []}
```

### 4.2  Complex schemas

For nested objects, arrays of objects, `oneOf`, etc., write the JSON Schema
dict directly — `make_params()` only handles flat keyword arguments.

---

## 5.  `require_params()` — Input Validation

```python
from slife.tools.base import require_params

async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
    if err := require_params(agent_id=agent_id, task=task):
        return err
    # Both agent_id and task are guaranteed non-empty
    ...
```

Returns `None` if all params are truthy, or an error message string.

---

## 6.  `_ConfigPathMixin` — Reading/Writing `slife.json5`

For tools that need to read or modify the config file:

```python
from slife.tools.base import Tool
from slife.tools._config_io import _ConfigPathMixin, read_config, write_config

class MyConfigTool(_ConfigPathMixin, Tool):
    name = "my_config_tool"
    ...

    async def execute(self, **kwargs) -> str:
        raw = read_config(self._config_path)
        # … modify raw …
        write_config(self._config_path, raw)
        return "Done."
```

The mixin provides `self._config_path` (resolved from the active config).
Set `_subagent_skip = True` on these tools — subagents shouldn't modify
the main agent's config.

---

## 7.  `from_config()` — Constructor Arguments

When a tool needs runtime configuration (timeout, directory paths, etc.):

```python
class ShellTool(Tool):
    name = "execute_shell"
    description = "Execute a shell command."
    parameters = make_params(
        command={"type": "string", "description": "The command to run."},
    )

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, config):
        return cls(timeout=cfg.get("timeout", 30))

    async def execute(self, command: str = "", **kwargs) -> str:
        proc = await asyncio.create_subprocess_shell(command, …)
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.timeout,
        )
        ...
```

Users can override the timeout in `slife.json5`:

```json5
{
  tools: [
    { name: "execute_shell", timeout: 60 },
  ],
}
```

---

## 8.  Tool Categories & Naming

| Prefix | Category | Example |
|--------|----------|---------|
| `a2a_*` | Agent Communication | `a2a_send_task`, `a2a_list_agents` |
| `mcp_*` | MCP Server Management | `mcp_add_server`, `mcp_list_tools` |
| `cli_*` | CLI Tools | `cli_add_tool`, `cli_list_tools` |
| `config_env_*` | Configuration | `config_env_set`, `config_secret_register` |
| `credential_*` | Credentials | `credential_check`, `inject_credential` |
| `memory_*` | Memory (plugin-proxied) | `memory_search`, `memory_open` |
| `execute_*` / `run_*` | Code Execution | `execute_shell`, `run_python_script` |
| `list_*` | Discovery/Meta | `list_native_tools`, `list_skills` |
| `system_*` | System | `system_health`, `get_os_info` |

Use these prefixes for consistency.  The `list_native_tools` tool uses
them to auto-categorize.

---

## 9.  Multi-Tool Files

A single file can define multiple related tools — this is preferred when
they share helpers or state:

```python
# slife/tools/my_feature.py

"""MyFeature — three related tools."""

from slife.tools.base import Tool, make_params

# Shared helpers
def _shared_helper(x: str) -> str:
    return x.upper()

class MyFeatureCreateTool(Tool):
    name = "my_feature_create"
    ...

class MyFeatureListTool(Tool):
    name = "my_feature_list"
    ...

class MyFeatureDeleteTool(Tool):
    name = "my_feature_delete"
    ...
```

Reference: `slife/tools/a2a.py` (10 tools), `slife/tools/cli.py` (4 tools),
`slife/tools/skill.py` (4 tools).

---

## 10.  Common Anti-Patterns

### ❌  Forgetting `**kwargs`

```python
# Broken — unexpected kwargs from the LLM will crash
async def execute(self, query: str) -> str:
    ...

# Correct — always accept **kwargs
async def execute(self, query: str = "", **kwargs) -> str:
    ...
```

### ❌  Blocking I/O in `execute()`

```python
# Broken — blocks the event loop
async def execute(self, **kwargs) -> str:
    import time
    time.sleep(5)                    # ❌  blocking
    return subprocess.run(["cmd"])   # ❌  blocking

# Correct — use async equivalents
async def execute(self, **kwargs) -> str:
    import asyncio
    await asyncio.sleep(5)                                     # ✅
    proc = await asyncio.create_subprocess_exec("cmd", …)      # ✅
```

### ❌  Importing heavy libraries at module level

```python
# Slows down startup for ALL tools
import torch   # ❌  at module level

class MLTool(Tool):
    async def execute(self, **kwargs) -> str:
        import torch   # ✅  lazy import inside execute()
        ...
```

### ❌  `from_config` without `__init__`

```python
class MyTool(Tool):
    # Broken — from_config calls cls(timeout=…), but __init__ doesn't accept it
    @classmethod
    def from_config(cls, cfg, config):
        return cls(timeout=cfg.get("timeout", 30))

    # Fixed — add matching __init__
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
```

---

## 11.  Testing a Tool

### 11.1  Direct instantiation

```python
import asyncio
from slife.tools.my_tool import MyTool

async def test():
    tool = MyTool()
    result = await tool.execute(query="test")
    print(result)

asyncio.run(test())
```

### 11.2  Through the registry

```python
from slife.tools.factory import create_tools_from_config
from slife.config import Config

config = Config.from_json5("slife.json5")
registry = create_tools_from_config(config=config)

tool = registry.get("my_tool_name")
result = await tool.execute(arg="value")
```

### 11.3  Check that your tool is discovered

```python
from slife.tools.factory import create_tools_from_config
registry = create_tools_from_config()
for t in registry.list_tools():
    print(t.name)
```

---

## 12.  Built-in Tool Reference

| File | Tools | Pattern |
|------|-------|---------|
| `os_info.py` | 1 (`get_os_info`) | Simplest — no params, no deps |
| `shell.py` | 1 (`execute_shell`) | `from_config` + subprocess |
| `pip.py` | 1 (`install_python_package`) | Subprocess with async timeout |
| `run_python_script.py` | 1 (`run_python_script`) | Input parsing + subprocess |
| `credentials.py` | 3 (`credential_check`, `inject_credential`, `uninject_credential`) | `_ConfigPathMixin` + multi-tool |
| `cli.py` | 4 (`cli_add_tool`, …) | `_ConfigPathMixin` + multi-tool |
| `skill.py` | 4 (`list_skills`, …) | Custom mixin (`_SkillDirMixin`) |
| `a2a.py` | 10 (`a2a_send_task`, …) | Large multi-tool, transport references |
| `system_health.py` | 1 (`system_health`) | Complex execute, multiple check functions |
| `config_env.py` | 4 (`config_env_set`, …) | `_ConfigPathMixin` + multi-tool |
| `list_native_tools.py` | 1 (`list_native_tools`) | Registry introspection |

Study these for real-world patterns — they cover the full range of
complexity from trivial to advanced.
