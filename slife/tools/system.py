"""System introspection & health check tools.

Tools:
    check_os_info            — OS name, version, architecture, Python version
    check_shells             — available shells (PowerShell, Bash, cmd, uv)
    check_workspace          — CWD, permissions, git, package manager
    check_embedding          — embedding backend status
    check_wechat             — WeChat plugin status
    system_health            — orchestrate all checks + startup records
    list_native_tools        — enumerate native vs MCP-proxied tools
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import tomllib
from collections import defaultdict
from pathlib import Path
from shutil import which
from typing import ClassVar

from slife.paths import get_data_dir
from slife.tools.base import Tool
from slife.health import get_report as get_startup_records

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# check_os_info
# ═══════════════════════════════════════════════════════════════════════

def check_os_info() -> list[dict]:
    """Return detailed OS information as health-check entries."""
    results: list[dict] = []
    uname = platform.uname()

    results.append({"component": "os", "level": "ok", "key": "system",
                    "value": uname.system,
                    "hint": f"OS: {uname.system} {uname.release} ({uname.version})"})
    results.append({"component": "os", "level": "ok", "key": "architecture",
                    "value": uname.machine,
                    "hint": f"Architecture: {uname.machine} (processor: {uname.processor or 'unknown'})"})
    results.append({"component": "os", "level": "ok", "key": "python_version",
                    "value": sys.version.split()[0],
                    "hint": f"Python {sys.version}"})
    results.append({"component": "os", "level": "ok", "key": "python_executable",
                    "value": sys.executable,
                    "hint": f"Python executable: {sys.executable}"})
    return results


class CheckOsInfoTool(Tool):
    """Report OS name, version, CPU architecture, and Python environment."""

    name = "check_os_info"
    description = (
        "Return OS name and version, CPU architecture, Python version, "
        "and Python executable path as a structured JSON list. "
        "The first entry contains the OS name for quick shell-syntax decisions."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_os_info(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_shells
# ═══════════════════════════════════════════════════════════════════════

def check_shells() -> list[dict]:
    """Return shell availability as health-check entries."""
    results: list[dict] = []

    pwsh_path = which("powershell.exe" if os.name == "nt" else "pwsh") or which("powershell")
    if pwsh_path:
        results.append({"component": "shell", "level": "ok", "key": "powershell",
                        "value": pwsh_path, "hint": f"PowerShell available: {pwsh_path}"})
    else:
        results.append({"component": "shell", "level": "warning", "key": "powershell",
                        "value": "not_found",
                        "hint": "PowerShell not found on PATH. Some commands may not work."})

    bash_path = which("bash")
    if bash_path:
        results.append({"component": "shell", "level": "ok", "key": "bash",
                        "value": bash_path, "hint": f"Bash available: {bash_path}"})
    else:
        results.append({"component": "shell", "level": "ok", "key": "bash",
                        "value": "not_found",
                        "hint": "Bash not found on PATH. On Windows, install Git Bash or WSL for POSIX shell support."})

    if os.name == "nt":
        cmd_path = which("cmd.exe") or which("cmd")
        results.append({"component": "shell", "level": "ok", "key": "cmd",
                        "value": cmd_path or os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
                        "hint": f"Command Prompt available: {cmd_path or 'COMSPEC'}"})

    uv_path = which("uv") or which("uv.exe")
    if uv_path:
        results.append({"component": "shell", "level": "ok", "key": "uv",
                        "value": uv_path, "hint": f"uv package manager available: {uv_path}"})
    else:
        results.append({"component": "shell", "level": "warning", "key": "uv",
                        "value": "not_found",
                        "hint": "uv not found on PATH. Install: https://docs.astral.sh/uv/"})
    return results


class CheckShellsTool(Tool):
    """Check which shells and package managers are on PATH."""

    name = "check_shells"
    description = (
        "Return which shells (PowerShell, Bash, cmd) and tools (uv) are "
        "available on PATH, with their executable paths or 'not_found'. "
        "Use before running commands to pick the right shell and syntax."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_shells(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_workspace
# ═══════════════════════════════════════════════════════════════════════

def check_workspace() -> list[dict]:
    """Return workspace status as health-check entries."""
    cwd = os.getcwd()
    results: list[dict] = [{
        "component": "workspace", "level": "ok", "key": "cwd",
        "value": cwd, "hint": f"Current working directory: {cwd}",
    }]

    pyproject = Path(cwd) / "pyproject.toml"
    data = None
    is_slife_project = False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        is_slife_project = data.get("project", {}).get("name") == "slife"
    except Exception:
        pass

    data_dir = get_data_dir()
    if is_slife_project:
        results.append({"component": "workspace", "level": "ok", "key": "environment",
                        "value": "development",
                        "hint": f"Development mode — data files stay in CWD: {cwd}"})
    else:
        results.append({"component": "workspace", "level": "ok", "key": "environment",
                        "value": "production",
                        "hint": f"Production mode — data files live in ~/.slife/ (data_dir={data_dir})"})

    uv_lock = Path(cwd) / "uv.lock"
    requirements = Path(cwd) / "requirements.txt"
    setup_py = Path(cwd) / "setup.py"
    setup_cfg = Path(cwd) / "setup.cfg"
    has_uv_tool = data and "uv" in data.get("tool", {})

    if uv_lock.exists() or has_uv_tool:
        extras = [e for e in (["uv.lock"] if uv_lock.exists() else []) + (
            ["[tool.uv] in pyproject.toml"] if has_uv_tool else [])]
        results.append({"component": "workspace", "level": "ok", "key": "package_manager",
                        "value": "uv",
                        "hint": f"uv project ({', '.join(extras)}). Install: uv sync"})
    elif requirements.exists() or setup_py.exists() or setup_cfg.exists():
        extras2 = [e for e in (["requirements.txt"] if requirements.exists() else [])
                   + (["setup.py"] if setup_py.exists() else [])
                   + (["setup.cfg"] if setup_cfg.exists() else [])]
        results.append({"component": "workspace", "level": "ok", "key": "package_manager",
                        "value": "pip",
                        "hint": f"pip project ({', '.join(extras2)}). Install: pip install -e ."})
    elif pyproject.exists():
        results.append({"component": "workspace", "level": "ok", "key": "package_manager",
                        "value": "pyproject_only",
                        "hint": "pyproject.toml exists but no lock file."})
    else:
        results.append({"component": "workspace", "level": "ok", "key": "package_manager",
                        "value": "none",
                        "hint": "Not a Python project (no pyproject.toml/requirements.txt/setup.py)."})

    results.append({"component": "workspace",
                    "level": "ok" if os.access(cwd, os.R_OK) else "error",
                    "key": "readable", "value": "yes" if os.access(cwd, os.R_OK) else "no",
                    "hint": f"Working directory is {'NOT ' if not os.access(cwd, os.R_OK) else ''}readable: {cwd}"})
    results.append({"component": "workspace",
                    "level": "ok" if os.access(cwd, os.W_OK) else "warning",
                    "key": "writable", "value": "yes" if os.access(cwd, os.W_OK) else "no",
                    "hint": f"Working directory is {'NOT ' if not os.access(cwd, os.W_OK) else ''}writable: {cwd}"})

    git_dir = os.path.join(cwd, ".git")
    results.append({"component": "workspace", "level": "ok", "key": "git_repo",
                    "value": "yes" if os.path.isdir(git_dir) else "no",
                    "hint": "Working directory is a Git repository." if os.path.isdir(git_dir)
                    else "Not a Git repository (or .git is a file/submodule)."})
    return results


class CheckWorkspaceTool(Tool):
    """Report working directory context: path, git, permissions, package manager."""

    name = "check_workspace"
    description = (
        "Return the working directory path, dev/production mode, "
        "Python package manager (uv/pip), read/write permissions, "
        "and git repository status as structured JSON. "
        "The first entry contains the CWD path."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_workspace(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_embedding
# ═══════════════════════════════════════════════════════════════════════

def check_embedding() -> list[dict]:
    """Return embedding backend status as health-check entries."""
    results: list[dict] = []
    from slife.plugins.memory.embeddings import EmbeddingClient
    from slife.plugins.memory.embedding_config import read_embedding_config

    client = EmbeddingClient.from_config(quiet=True)
    cfg = read_embedding_config()

    if cfg is None:
        results.append({"component": "embeddings", "level": "warning", "key": "backend",
                        "value": "none",
                        "hint": ("No embedding backend configured. Semantic search (hybrid mode) will NOT work. "
                                 "Keyword search (grep/fts5/time) still works normally. "
                                 "Use memory_set_embedding to configure one: "
                                 "GGUF local model, transformer (sentence-transformers), or OpenAI-compatible API.")})
        return results

    backend = client.backend
    available = client.available

    if available:
        hints = {
            "gguf": f"GGUF model ready: {cfg.get('model', '?')} (dim={client.dimension}, path={cfg.get('gguf_path', 'unknown')})",
            "transformer": f"Transformer model ready: {cfg.get('model', '?')} (dim={client.dimension})",
        }
        results.append({"component": "embeddings", "level": "ok", "key": "backend",
                        "value": backend,
                        "hint": hints.get(backend, f"API embeddings ready: {cfg.get('model', '?')} (dim={client.dimension})")})
    else:
        warnings = {
            "gguf": (f"GGUF file exists ({cfg.get('gguf_path', 'unknown')}) but "
                     "llama-cpp-python is NOT installed. Semantic search (hybrid mode) will NOT work. "
                     "Install with: pip install llama-cpp-python. Keyword search (grep/fts5/time) still works normally."),
            "transformer": (f"Transformer model configured ({cfg.get('model', '?')}) but "
                            "sentence-transformers is NOT installed. Semantic search (hybrid mode) will NOT work. "
                            "Install with: pip install sentence-transformers. Keyword search (grep/fts5/time) still works normally."),
            "api": ("API key configured but openai package is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: pip install openai. Keyword search (grep/fts5/time) still works normally."),
        }
        results.append({"component": "embeddings", "level": "warning", "key": "backend",
                        "value": backend,
                        "hint": warnings.get(backend, "Embedding backend is unavailable for unknown reasons. "
                                             "Semantic search (hybrid mode) will NOT work. "
                                             "Keyword search (grep/fts5/time) still works normally.")})
    return results


class CheckEmbeddingTool(Tool):
    """Check embedding backend for semantic memory search."""

    name = "check_embedding"
    description = (
        "Return which embedding backend is configured (gguf/transformer/api/none) "
        "and whether it is actually usable. When unavailable, hybrid memory "
        "search degrades gracefully — keyword search still works."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_embedding(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_wechat
# ═══════════════════════════════════════════════════════════════════════

def _get_wechat_config():
    """Try to load slife config for wechat status.  Returns None on failure."""
    try:
        from slife.config import Config, parse_cli_agent
        agent_id = parse_cli_agent(sys.argv)
        cfg_path = get_data_dir() / "slife.json5"
        if cfg_path.exists():
            return Config.from_json5(cfg_path, agent_id=agent_id)
    except Exception:
        pass
    return None


def check_wechat(config=None) -> list[dict]:
    """Return WeChat plugin status as health-check entries."""
    results: list[dict] = []

    if config is None:
        config = _get_wechat_config()

    if config is None or config.wechat_config is None:
        results.append({"component": "wechat", "level": "ok", "key": "enabled",
                        "value": "unknown",
                        "hint": "WeChat plugin: config not available (no slife.json5?). "
                                "Default is enabled — will activate when config is loaded."})
        return results

    wc = config.wechat_config
    if not wc.enabled:
        results.append({"component": "wechat", "level": "ok", "key": "enabled",
                        "value": "disabled",
                        "hint": "WeChat plugin is disabled in config (wechat.enabled: false). "
                                "Set wechat.enabled: true in slife.json5 to enable."})
        return results

    try:
        from slife.plugins.wechat.config import load_wechat_config
        from slife.plugins.wechat.client import WechatClawbotClient
        SESSION_MAX_AGE = WechatClawbotClient.SESSION_MAX_AGE
    except ImportError:
        SESSION_MAX_AGE = 23 * 3600

    session = load_wechat_config(config.agent_id, get_data_dir())

    if not session.get("bot_token"):
        results.append({"component": "wechat", "level": "ok", "key": "status",
                        "value": "not_logged_in",
                        "hint": "WeChat plugin is enabled but not logged in. "
                                "Call wechat_login to scan QR code and connect."})
        return results

    saved_at = session.get("saved_at", 0)
    age = time.time() - saved_at
    remaining = max(0, SESSION_MAX_AGE - age)

    if remaining <= 0:
        results.append({"component": "wechat", "level": "warning", "key": "status",
                        "value": "session_expired",
                        "hint": f"WeChat session expired ({age / 3600:.1f}h old, max 23h). "
                                "Call wechat_login to re-scan QR code."})
    else:
        results.append({"component": "wechat", "level": "ok", "key": "status",
                        "value": "logged_in",
                        "hint": f"WeChat logged in. Session age: {age / 3600:.1f}h, "
                                f"remaining: {remaining / 3600:.1f}h."})
    return results


class CheckWechatTool(Tool):
    """Check WeChat plugin status: enabled, logged in, session expiry."""

    name = "check_wechat"
    description = (
        "Return WeChat plugin state: disabled, not_logged_in, session_expired, "
        "or logged_in (with session age and remaining time). "
        "Use before messaging to verify connectivity."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_wechat(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# system_health orchestrator
# ═══════════════════════════════════════════════════════════════════════

_CHECK_FUNCTIONS: list[str] = [
    "check_os_info",
    "check_shells",
    "check_workspace",
    "check_embedding",
    "check_wechat",
]


def _run_checks() -> list[dict]:
    """Call every registered check function via dynamic lookup.

    Uses ``getattr`` on the current module so that test patches
    (``unittest.mock.patch``) work — they replace the module attribute.
    Failures in individual checks are recorded as error entries
    so one broken check never blocks the rest of the report.
    """
    import sys as _sys
    _mod = _sys.modules[__name__]

    all_entries: list[dict] = []
    for func_name in _CHECK_FUNCTIONS:
        try:
            fn = getattr(_mod, func_name)
            all_entries.extend(fn())
        except Exception as e:
            logger.warning("health_check_failed check=%s err=%s", func_name, e)
            all_entries.append({
                "component": "system_health", "level": "error",
                "key": f"{func_name}_failed", "value": str(e),
                "hint": f"Check {func_name}() raised {type(e).__name__}: {e}",
            })
    return all_entries


def _group_by_component(entries: list[dict]) -> dict[str, list[dict]]:
    """Group flat entry list by component for structured display."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        comp = e.get("component", "unknown")
        groups.setdefault(comp, []).append(e)
    return groups


def _component_status(entries: list[dict]) -> str:
    """Worst status across a group: ok < warning < error."""
    levels = {e.get("level", "ok") for e in entries}
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    return "ok"


def _build_summary(groups: dict[str, list[dict]]) -> str:
    """One-line summary: '3 ok, 2 warnings (embeddings, memory), 0 errors'."""
    ok_count = sum(1 for es in groups.values() if _component_status(es) == "ok")
    warn_comps = [comp for comp, es in groups.items() if _component_status(es) == "warning"]
    err_comps = [comp for comp, es in groups.items() if _component_status(es) == "error"]
    parts: list[str] = [f"{ok_count} ok"]
    if warn_comps:
        parts.append(f"{len(warn_comps)} warning(s): {', '.join(warn_comps)}")
    if err_comps:
        parts.append(f"{len(err_comps)} error(s): {', '.join(err_comps)}")
    return "; ".join(parts)


def _overall_healthy(groups: dict[str, list[dict]]) -> bool:
    return all(_component_status(es) == "ok" for es in groups.values())


class SystemHealthTool(Tool):
    """Run all subsystem checks + startup records → unified health report."""

    name = "system_health"
    description = (
        "Return a unified health report as JSON: startup records (config, model, "
        "MCP servers, memory, A2A, subagent), OS/shell/workspace/embedding/WeChat "
        "status, with an overall healthy flag and summary line. "
        "Call at conversation start to discover silently-degraded subsystems. "
        "Individual checks: check_os_info, check_shells, check_workspace, "
        "check_embedding, check_wechat."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        startup = get_startup_records()
        dynamic = _run_checks()
        all_entries = startup + dynamic
        groups = _group_by_component(all_entries)

        components: dict[str, dict] = {}
        for comp, entries in groups.items():
            components[comp] = {
                "status": _component_status(entries),
                "entries": entries,
            }

        result = {
            "healthy": _overall_healthy(groups),
            "summary": _build_summary(groups),
            "components": components,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# list_native_tools
# ═══════════════════════════════════════════════════════════════════════

_PLUGIN_LABELS: dict[str, str] = {
    "memory": "Memory (built-in plugin)",
    "wechat": "WeChat (built-in plugin)",
}


def _classify(name: str) -> str:
    """Map a native tool name to a category label."""
    if name.startswith("a2a_"):
        return "Agent Communication (A2A)"
    if name.startswith("mcp_"):
        return "MCP Server Management"
    if name.startswith("cli_"):
        return "CLI Tools"
    if name.startswith("config_env"):
        return "Configuration"
    if name in ("list_skills", "use_skill", "add_skill", "remove_skill"):
        return "Skills"
    if name.startswith("system_") or name.startswith("check_"):
        return "System"
    if name.startswith("execute_") or name.startswith("install_") or name.startswith("run_"):
        return "Code Execution"
    if name.startswith("list_"):
        return "Meta"
    if name.startswith("credential_") or name.startswith("inject_") or name.startswith("uninject_"):
        return "Credentials"
    return "Other"


class ListNativeToolsTool(Tool):
    """List all built-in tools grouped by category, plus MCP-connected servers."""

    name: ClassVar[str] = "list_native_tools"
    description: ClassVar[str] = (
        "Return your complete tool inventory: native (built-in) tools grouped "
        "by category, and external tools from each connected MCP server. "
        "Use when asked what tools you have — more reliable than memory."
    )
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        from slife.tools.registry import get_registry
        from slife.mcp.tool_adapter import MCPProxyTool

        registry = get_registry()
        if registry is None:
            return "Tool registry is not available (called before initialization)."

        all_tools = registry.list_tools()
        if not all_tools:
            return "No tools are currently registered."

        natives: list[Tool] = []
        mcp_proxies: dict[str, list[Tool]] = defaultdict(list)
        for t in all_tools:
            if isinstance(t, MCPProxyTool):
                mcp_proxies[t._server].append(t)
            else:
                natives.append(t)

        lines: list[str] = [f"## Native Tools ({len(natives)} total)\n"]
        native_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for t in sorted(natives, key=lambda t: t.name):
            category = _classify(t.name)
            desc = t.description.split(".")[0].strip() + "."
            native_groups[category].append((t.name, desc))

        for category in sorted(native_groups):
            items = native_groups[category]
            lines.append(f"### {category} ({len(items)})")
            for name, desc in items:
                lines.append(f"- **`{name}`** — {desc}")
            lines.append("")

        if mcp_proxies:
            lines.append(f"## MCP-Connected Servers ({len(mcp_proxies)} servers)\n")
            for server in sorted(mcp_proxies):
                tools = mcp_proxies[server]
                label = _PLUGIN_LABELS.get(server, f"MCP: {server}")
                tool_names = sorted(t.name for t in tools)
                lines.append(f"- **{label}** ({len(tools)} tools): "
                             + ", ".join(f"`{n}`" for n in tool_names))
            lines.append("")

        return "\n".join(lines)
