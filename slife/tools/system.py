"""System introspection & health check tools.

Tools:
    check_os_info            — OS name, version, architecture, Python version
    check_shells             — available shells (PowerShell, Bash, cmd, uv)
    check_workspace          — CWD, permissions, git, package manager
    check_embedding          — embedding backend status
    check_wechat             — WeChat plugin status
    system_health            — orchestrate all checks + startup records
    list_tools               — enumerate native vs MCP-proxied tools (with category filter)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import tomllib
from pathlib import Path
from shutil import which
from typing import ClassVar

from slife.paths import get_data_dir, get_environment_info
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
    category: ClassVar[str] = "System"
    description = "OS name, CPU architecture, Python version and path as JSON."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_os_info(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_shells
# ═══════════════════════════════════════════════════════════════════════

def _detect_current_shell() -> str:
    """Detect the shell that launched slife (the parent shell)."""
    if os.name != "nt":
        return os.environ.get("SHELL", os.environ.get("SHELL", "sh"))
    # Windows: PowerShell sets PSModulePath; otherwise assume cmd
    if os.environ.get("PSModulePath"):
        return "powershell"
    return "cmd"


def check_shells() -> list[dict]:
    """Return shell availability as health-check entries."""
    results: list[dict] = []

    # Report the shell slife is running under first — LLM should match
    # command syntax to this shell, not to shells merely on PATH.
    current = _detect_current_shell()
    results.append({"component": "shell", "level": "ok", "key": "current_shell",
                    "value": current,
                    "hint": f"slife is running under {current}. Use {current} syntax for shell commands."})

    pwsh_path = which("powershell.exe" if os.name == "nt" else "pwsh") or which("powershell")
    if pwsh_path:
        results.append({"component": "shell", "level": "ok", "key": "powershell",
                        "value": pwsh_path, "hint": f"PowerShell available: {pwsh_path}"})
    elif os.name == "nt":
        results.append({"component": "shell", "level": "warning", "key": "powershell",
                        "value": "not_found",
                        "hint": "PowerShell not found on PATH. Some commands may not work."})
    else:
        results.append({"component": "shell", "level": "ok", "key": "powershell",
                        "value": "not_found",
                        "hint": "PowerShell not installed (non-Windows platform — expected, not an error)."})

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
    category: ClassVar[str] = "System"
    description = "Available shells (PowerShell, Bash, cmd) and uv on PATH, as JSON."
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

    # ── Slife's own environment (dev vs production) ────────────────
    env_info = get_environment_info()
    results.append({"component": "workspace", "level": "ok", "key": "environment",
                    "value": env_info["mode"],
                    "hint": env_info["hint"]})

    # ── User's CWD project detection ───────────────────────────────
    pyproject = Path(cwd) / "pyproject.toml"
    data = None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        pass

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
    category: ClassVar[str] = "System"
    description = "CWD, dev/prod mode, package manager, permissions, git status as JSON."
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
                     "Install with: uv pip install llama-cpp-python. Keyword search (grep/fts5/time) still works normally."),
            "transformer": (f"Transformer model configured ({cfg.get('model', '?')}) but "
                            "sentence-transformers is NOT installed. Semantic search (hybrid mode) will NOT work. "
                            "Install with: uv pip install sentence-transformers. Keyword search (grep/fts5/time) still works normally."),
            "api": ("API key configured but openai package is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: uv pip install openai. Keyword search (grep/fts5/time) still works normally."),
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
    category: ClassVar[str] = "System"
    description = "Embedding backend status (gguf/transformer/api/none) and availability."
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
        results.append({"component": "wechat", "level": "warning", "key": "status",
                        "value": "plugin_unavailable",
                        "hint": "WeChat plugin is enabled but the wechat package is not installed."})
        return results

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
    category: ClassVar[str] = "System"
    description = "WeChat plugin status: disabled, not_logged_in, logged_in, or session_expired."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_wechat(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_mcp_servers
# ═══════════════════════════════════════════════════════════════════════

async def check_mcp_servers() -> list[dict]:
    """Return MCP server status by calling mcp_list_servers."""
    results: list[dict] = []
    try:
        from slife.tools.registry import get_registry
        registry = get_registry()
        if registry is None:
            return [{"component": "mcp_servers", "level": "warning",
                     "key": "status", "value": "not_initialized",
                     "hint": "Tool registry not yet initialized — MCP status unavailable."}]

        # Find the mcp_list_servers proxy tool (registered by slife-mcp).
        mcp_list_tool = None
        for t in registry.list_tools():
            if t.name == "mcp_list_servers" or t.name.endswith("__mcp_list_servers"):
                mcp_list_tool = t
                break

        if mcp_list_tool is None:
            return [{"component": "mcp_servers", "level": "warning",
                     "key": "status", "value": "tool_missing",
                     "hint": "mcp_list_servers tool not found — slife-mcp may not be running."}]

        raw = await mcp_list_tool.execute()
        data = json.loads(raw)

        if not isinstance(data, list) or len(data) == 0:
            return [{"component": "mcp_servers", "level": "ok",
                     "key": "status", "value": "none",
                     "hint": "No external MCP servers configured."}]

        for server in data:
            name = server.get("name", "?")
            state = server.get("state", "unknown")
            tool_count = server.get("tool_count", 0)
            transport = server.get("transport", "")
            error_msg = server.get("error", "")
            enabled = server.get("enabled", True)

            if not enabled:
                results.append({
                    "component": "mcp_servers", "level": "ok",
                    "key": name, "value": "disabled",
                    "hint": f"MCP server '{name}' is disabled.",
                })
            elif state == "running":
                results.append({
                    "component": "mcp_servers", "level": "ok",
                    "key": name, "value": f"connected ({tool_count} tools)",
                    "hint": f"MCP server '{name}' connected via {transport}, {tool_count} tools available.",
                })
            elif state == "stopped":
                detail = f" — {error_msg}" if error_msg else ""
                results.append({
                    "component": "mcp_servers", "level": "warning",
                    "key": name, "value": f"disconnected{detail}",
                    "hint": f"MCP server '{name}' is NOT connected.{detail} Use mcp_check_server to diagnose.",
                })
            else:
                results.append({
                    "component": "mcp_servers", "level": "warning",
                    "key": name, "value": state,
                    "hint": f"MCP server '{name}' state={state}.",
                })

        return results
    except Exception as e:
        logger.warning("check_mcp_servers_failed err=%s", e)
        return [{"component": "mcp_servers", "level": "error",
                 "key": "check_failed", "value": str(e),
                 "hint": f"Failed to check MCP servers: {e}"}]


# ═══════════════════════════════════════════════════════════════════════
# system_health orchestrator
# ═══════════════════════════════════════════════════════════════════════

_CHECK_FUNCTIONS: list[str] = [
    "check_os_info",
    "check_shells",
    "check_workspace",
    "check_embedding",
    "check_wechat",
    "check_mcp_servers",
]


async def _run_checks() -> list[dict]:
    """Call every registered check function via dynamic lookup.

    Supports both sync and async check functions.  Uses ``getattr`` on
    the current module so that test patches (``unittest.mock.patch``)
    work — they replace the module attribute.  Failures in individual
    checks are recorded as error entries so one broken check never
    blocks the rest of the report.
    """
    import inspect as _inspect
    import sys as _sys
    _mod = _sys.modules[__name__]

    all_entries: list[dict] = []
    for func_name in _CHECK_FUNCTIONS:
        try:
            fn = getattr(_mod, func_name)
            if _inspect.iscoroutinefunction(fn):
                entries = await fn()
            else:
                entries = fn()
            all_entries.extend(entries)
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
    category: ClassVar[str] = "System"
    description = "Unified health report: startup records + OS/shell/workspace/embedding/WeChat/MCP, with healthy flag and summary."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        startup = get_startup_records()
        dynamic = await _run_checks()
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


