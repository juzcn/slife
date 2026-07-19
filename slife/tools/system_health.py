"""System health check — reports startup status that logs capture but
the agent can't see, plus live OS/shell/workspace information.

Call this early in a conversation to discover silently-degraded
subsystems: embedding failures, schema migration errors, missing
Python packages, MCP connection issues, etc.  All of these are
invisible to the agent otherwise — they only appear in log files.

Also reports: OS info (system, architecture, Python version),
available shells (bash, cmd, PowerShell), and working directory
status (readable, writable, git repo detection).
"""

import json
import logging

from slife.tools.base import Tool
from slife.health import get_report as get_startup_records

logger = logging.getLogger(__name__)


# ── Active checks (run every time the tool is called) ──────────────


def _check_embedding_config() -> list[dict]:
    """Check the embedding backend configuration and runtime usability."""
    results: list[dict] = []
    from slife.plugins.memory.embeddings import EmbeddingClient
    from slife.plugins.memory.embedding_config import read_embedding_config

    client = EmbeddingClient.from_config()
    cfg = read_embedding_config()

    if cfg is None:
        results.append({
            "component": "embeddings",
            "level": "warning",
            "key": "backend",
            "value": "none",
            "hint": (
                "No embedding backend configured. "
                "Semantic search (hybrid mode) will NOT work. "
                "Keyword search (grep/fts5/time) still works normally. "
                "Use memory_set_embedding to configure one: "
                "GGUF local model or OpenAI-compatible API."
            ),
        })
        return results

    backend = client.backend
    available = client.available

    if available:
        if backend == "gguf":
            gguf_path = cfg.get("gguf_path", "unknown")
            results.append({
                "component": "embeddings",
                "level": "ok",
                "key": "backend",
                "value": "gguf",
                "hint": f"GGUF model ready: {cfg.get('model', '?')} "
                        f"(dim={client.dimension}, path={gguf_path})",
            })
        else:
            results.append({
                "component": "embeddings",
                "level": "ok",
                "key": "backend",
                "value": "api",
                "hint": f"API embeddings ready: {cfg.get('model', '?')} "
                        f"(dim={client.dimension})",
            })
    else:
        if backend == "gguf":
            gguf_path = cfg.get("gguf_path", "unknown")
            results.append({
                "component": "embeddings",
                "level": "warning",
                "key": "backend",
                "value": "gguf",
                "hint": (
                    f"GGUF file exists ({gguf_path}) but "
                    "llama-cpp-python is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: pip install llama-cpp-python. "
                    "Keyword search (grep/fts5/time) still works normally."
                ),
            })
        elif backend == "api":
            results.append({
                "component": "embeddings",
                "level": "warning",
                "key": "backend",
                "value": "api",
                "hint": (
                    "API key configured but openai package is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: pip install openai. "
                    "Keyword search (grep/fts5/time) still works normally."
                ),
            })
        else:
            results.append({
                "component": "embeddings",
                "level": "warning",
                "key": "backend",
                "value": "unknown",
                "hint": (
                    "Embedding backend is unavailable for unknown reasons. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Keyword search (grep/fts5/time) still works normally."
                ),
            })
    return results


def _check_wechat_status(config=None) -> list[dict]:
    """Check the WeChat plugin configuration and session state."""
    results: list[dict] = []
    import os
    import time
    from pathlib import Path

    # Get config if not passed (tool runs outside AgentService context)
    if config is None:
        try:
            from slife.config import Config, parse_cli_agent
            import sys as _sys
            agent_id = parse_cli_agent(_sys.argv)
            # Try loading the config — may fail if no slife.json5 exists
            cfg_path = Path("slife.json5")
            if cfg_path.exists():
                config = Config.from_json5(cfg_path, agent_id=agent_id)
        except Exception:
            pass

    if config is None or config.wechat_config is None:
        results.append({
            "component": "wechat",
            "level": "ok",
            "key": "enabled",
            "value": "unknown",
            "hint": (
                "WeChat plugin: config not available (no slife.json5?). "
                "Default is enabled — will activate when config is loaded."
            ),
        })
        return results

    wc = config.wechat_config
    if not wc.enabled:
        results.append({
            "component": "wechat",
            "level": "ok",
            "key": "enabled",
            "value": "disabled",
            "hint": (
                "WeChat plugin is disabled in config (wechat.enabled: false). "
                "Set wechat.enabled: true in slife.json5 to enable."
            ),
        })
        return results

    # Plugin is enabled — check session
    try:
        from slife.plugins.wechat.config import load_wechat_config
        from slife.plugins.wechat.client import WechatClawbotClient
        SESSION_MAX_AGE = WechatClawbotClient.SESSION_MAX_AGE
    except ImportError:
        SESSION_MAX_AGE = 23 * 3600

    wd = Path(os.environ.get("SLIFE_CONFIG_DIR", "."))
    session = load_wechat_config(config.agent_id, wd)

    if not session.get("bot_token"):
        results.append({
            "component": "wechat",
            "level": "ok",
            "key": "status",
            "value": "not_logged_in",
            "hint": (
                "WeChat plugin is enabled but not logged in. "
                "Call wechat_login to scan QR code and connect. "
                "Tools available: wechat_login, wechat_send_message, "
                "wechat_check_status, wechat_logout."
            ),
        })
        return results

    saved_at = session.get("saved_at", 0)
    age = time.time() - saved_at
    remaining = max(0, SESSION_MAX_AGE - age)

    if remaining <= 0:
        results.append({
            "component": "wechat",
            "level": "warning",
            "key": "status",
            "value": "session_expired",
            "hint": (
                f"WeChat session expired ({age/3600:.1f}h old, max 23h). "
                "Call wechat_login to re-scan QR code."
            ),
        })
    else:
        results.append({
            "component": "wechat",
            "level": "ok",
            "key": "status",
            "value": "logged_in",
            "hint": (
                f"WeChat logged in. Session age: {age/3600:.1f}h, "
                f"remaining: {remaining/3600:.1f}h. "
                "Tools: wechat_send_message, wechat_check_status, wechat_logout."
            ),
        })

    return results


def _check_os_info() -> list[dict]:
    """Report detailed OS information."""
    import platform

    results: list[dict] = []

    uname = platform.uname()
    results.append({
        "component": "os",
        "level": "ok",
        "key": "system",
        "value": uname.system,
        "hint": f"OS: {uname.system} {uname.release} ({uname.version})",
    })
    results.append({
        "component": "os",
        "level": "ok",
        "key": "architecture",
        "value": uname.machine,
        "hint": f"Architecture: {uname.machine} (processor: {uname.processor or 'unknown'})",
    })

    # Python info
    import sys
    results.append({
        "component": "os",
        "level": "ok",
        "key": "python_version",
        "value": sys.version.split()[0],
        "hint": f"Python {sys.version}",
    })
    results.append({
        "component": "os",
        "level": "ok",
        "key": "python_executable",
        "value": sys.executable,
        "hint": f"Python executable: {sys.executable}",
    })

    return results


def _check_shell_info() -> list[dict]:
    """Check available shells: bash, cmd, PowerShell."""
    import os
    from shutil import which

    results: list[dict] = []

    # PowerShell (always check — most useful on Windows, available on others)
    pwsh_path = which("powershell.exe" if os.name == "nt" else "pwsh") or which("powershell")
    if pwsh_path:
        results.append({
            "component": "shell",
            "level": "ok",
            "key": "powershell",
            "value": pwsh_path,
            "hint": f"PowerShell available: {pwsh_path}",
        })
    else:
        results.append({
            "component": "shell",
            "level": "warning",
            "key": "powershell",
            "value": "not_found",
            "hint": "PowerShell not found on PATH. Some commands may not work.",
        })

    # Bash / Git Bash
    bash_path = which("bash")
    if bash_path:
        results.append({
            "component": "shell",
            "level": "ok",
            "key": "bash",
            "value": bash_path,
            "hint": f"Bash available: {bash_path}",
        })
    else:
        results.append({
            "component": "shell",
            "level": "ok",
            "key": "bash",
            "value": "not_found",
            "hint": (
                "Bash not found on PATH. On Windows, install Git Bash "
                "or WSL for POSIX shell support."
            ),
        })

    # cmd (Windows)
    if os.name == "nt":
        cmd_path = which("cmd.exe") or which("cmd")
        results.append({
            "component": "shell",
            "level": "ok",
            "key": "cmd",
            "value": cmd_path or os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "hint": f"Command Prompt available: {cmd_path or 'COMSPEC'}",
        })

    # uv (Python package manager)
    uv_path = which("uv") or which("uv.exe")
    if uv_path:
        results.append({
            "component": "shell",
            "level": "ok",
            "key": "uv",
            "value": uv_path,
            "hint": f"uv package manager available: {uv_path}",
        })
    else:
        results.append({
            "component": "shell",
            "level": "warning",
            "key": "uv",
            "value": "not_found",
            "hint": "uv not found on PATH. Install: https://docs.astral.sh/uv/",
        })

    return results


def _check_working_dir() -> list[dict]:
    """Report the current working directory."""
    import os

    cwd = os.getcwd()
    results: list[dict] = [{
        "component": "workspace",
        "level": "ok",
        "key": "cwd",
        "value": cwd,
        "hint": f"Current working directory: {cwd}",
    }]

    # Dev vs production environment detection
    import tomllib
    from pathlib import Path
    pyproject = Path(cwd) / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        is_slife_project = data.get("project", {}).get("name") == "slife"
    except Exception:
        is_slife_project = False
        data = None

    data_dir = os.environ.get("SLIFE_DATA_DIR", "")
    if is_slife_project:
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "environment",
            "value": "development",
            "hint": (
                f"Development mode — data files (DB, logs, config) "
                f"stay in CWD: {cwd}"
            ),
        })
    else:
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "environment",
            "value": "production",
            "hint": (
                f"Production mode — data files live in "
                f"~/.slife/ (SLIFE_DATA_DIR={data_dir or '~/.slife/'})"
            ),
        })

    # Package manager detection (uv vs pip)
    uv_lock = Path(cwd) / "uv.lock"
    requirements = Path(cwd) / "requirements.txt"
    setup_py = Path(cwd) / "setup.py"
    setup_cfg = Path(cwd) / "setup.cfg"
    has_uv_tool = data and "uv" in data.get("tool", {})

    if uv_lock.exists() or has_uv_tool:
        extras: list[str] = []
        if uv_lock.exists():
            extras.append("uv.lock")
        if has_uv_tool:
            extras.append("[tool.uv] in pyproject.toml")
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "package_manager",
            "value": "uv",
            "hint": f"uv project ({', '.join(extras)}). Install: uv sync",
        })
    elif requirements.exists() or setup_py.exists() or setup_cfg.exists():
        extras2: list[str] = []
        if requirements.exists():
            extras2.append("requirements.txt")
        if setup_py.exists():
            extras2.append("setup.py")
        if setup_cfg.exists():
            extras2.append("setup.cfg")
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "package_manager",
            "value": "pip",
            "hint": f"pip project ({', '.join(extras2)}). Install: pip install -e .",
        })
    elif pyproject.exists():
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "package_manager",
            "value": "pyproject_only",
            "hint": (
                "pyproject.toml exists but no lock file — "
                "could be uv, pip, or other build system."
            ),
        })
    else:
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "package_manager",
            "value": "none",
            "hint": "Not a Python project (no pyproject.toml/requirements.txt/setup.py).",
        })

    # Quick check: readable, writable, is a git repo?
    if os.access(cwd, os.R_OK):
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "readable",
            "value": "yes",
            "hint": "Working directory is readable.",
        })
    else:
        results.append({
            "component": "workspace",
            "level": "error",
            "key": "readable",
            "value": "no",
            "hint": f"Working directory is NOT readable: {cwd}",
        })

    if os.access(cwd, os.W_OK):
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "writable",
            "value": "yes",
            "hint": "Working directory is writable.",
        })
    else:
        results.append({
            "component": "workspace",
            "level": "warning",
            "key": "writable",
            "value": "no",
            "hint": f"Working directory is NOT writable: {cwd}",
        })

    # Git repo detection
    git_dir = os.path.join(cwd, ".git")
    if os.path.isdir(git_dir):
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "git_repo",
            "value": "yes",
            "hint": "Working directory is a Git repository.",
        })
    else:
        results.append({
            "component": "workspace",
            "level": "ok",
            "key": "git_repo",
            "value": "no",
            "hint": "Not a Git repository (or .git is a file/submodule).",
        })

    return results


# ── Grouping ───────────────────────────────────────────────────────


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
    warn_comps = [
        comp for comp, es in groups.items()
        if _component_status(es) == "warning"
    ]
    err_comps = [
        comp for comp, es in groups.items()
        if _component_status(es) == "error"
    ]
    parts: list[str] = [f"{ok_count} ok"]
    if warn_comps:
        parts.append(f"{len(warn_comps)} warning(s): {', '.join(warn_comps)}")
    if err_comps:
        parts.append(f"{len(err_comps)} error(s): {', '.join(err_comps)}")
    return "; ".join(parts)


def _overall_healthy(groups: dict[str, list[dict]]) -> bool:
    return all(
        _component_status(es) == "ok"
        for es in groups.values()
    )


# ── Tool ───────────────────────────────────────────────────────────


class SystemHealthTool(Tool):
    """Report system startup health — embedding availability, schema
    migration errors, missing packages, MCP server status, etc.

    The agent should call this at the start of a conversation (or when
    the user first asks about system capabilities) to discover problems
    that are only visible in log files.
    """

    name = "system_health"
    description = (
        "Check Slife system health. Reports OS information (system, "
        "architecture, Python version), available shells (bash, cmd, "
        "PowerShell), working directory (readable/writable, git status), "
        "embedding backend status, schema migration errors, missing Python "
        "packages, MCP server connection status, and other startup issues "
        "that are only logged to files (invisible to you). "
        "Call this at conversation start or when the user asks about "
        "system capabilities — silently-degraded features can't be "
        "detected otherwise."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        # Phase 1: Pre-recorded entries from startup (config, model,
        #          memory_service, mcp_wrapper, mcp_server, a2a, subagent)
        startup = get_startup_records()

        # Phase 2: Dynamic checks — run fresh every call
        os_info = _check_os_info()
        shell_info = _check_shell_info()
        workspace = _check_working_dir()
        embedding = _check_embedding_config()
        wechat = _check_wechat_status()

        all_entries = startup + os_info + shell_info + workspace + embedding + wechat
        groups = _group_by_component(all_entries)

        # Build per-component status
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
