"""System prompt builder — renders the Slife system prompt.

Identity templates (``agent.j2`` / ``subagent.j2``) own the role framing;
both ``{% include 'slife.j2' %}`` for the shared runtime spec sheet.  Only
project-specific facts the LLM cannot discover from training data or tool
schemas.  No personality, no instructions, no decoration.
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

from slife.paths import (
    get_config_path,
    get_data_dir,
    get_db_path,
    get_logs_dir,
    get_memfiles_dir,
    get_skills_dir,
)

if TYPE_CHECKING:
    from slife.config import Config

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def render_template(template: str, **kwargs: object) -> str:
    """Render a template from the shared ``templates/`` directory.

    The single entry point for model-visible prompt fragments that live
    alongside the identity templates (e.g. the scheduled-task worker brief
    in ``schedule.j2``), so all prompt text is managed as templates in one
    place rather than assembled by hand.
    """
    return _env.get_template(template).render(**kwargs).strip()


def build(config: Config, is_subagent: bool = False) -> str:
    """Render the system prompt for a main agent or a subagent worker.

    The identity template (``agent.j2`` / ``subagent.j2``) opens with the
    role's identity framing and ``{% include 'slife.j2' %}`` for the shared
    runtime spec sheet — one render, no string concatenation.  The
    ``slife.j2`` block is byte-identical in both compositions, so the model
    sees the same world regardless of role.

    All platform facts are computed in :func:`_render_context` — no
    dependency on ``slife.tools`` or any tool module.
    """
    name = "subagent.j2" if is_subagent else "agent.j2"
    return _env.get_template(name).render(**_render_context(config)).strip()


def _render_context(config: Config) -> dict:
    """Compute the render context shared by every identity template.

    ``agent.j2`` ignores the ``subagent_*`` keys; the world template
    (``slife.j2``) is included identically by both identities.
    """
    model = config.active_model
    a2a = config.a2a_config

    return {
        # ── 身份 ──
        "agent_name": config.agent_name,
        # When the agent's persisted memory began (earliest diary turn) —
        # tells the LLM the origin of its session history.
        "memory_start_time": _memory_start_time(config.agent_name),
        # ── 环境 ──
        "platform_type": _platform_type(),
        "platform_name": _os_name(),
        "os_version": _os_version(),
        "arch": platform.machine(),
        "python_cmd": sys.executable,
        "python_version": sys.version.split()[0],
        "package_manager": "uv",
        # ── 上下文窗口策略 ──
        "context_floor": int(config.context_floor * 100),
        "context_ceiling": int(config.context_ceiling * 100),
        # ── 图像与多模态 ──
        "has_vision": model.supports_vision,
        # ── 工具与技能 ──
        "skills_directory": str(get_skills_dir().resolve()),
        "data_dir": str(get_data_dir().resolve()),
        "config_path": str(get_config_path().resolve()),
        "logs_dir": str(get_logs_dir().resolve()),
        "db_path": str(get_db_path(config.agent_name).resolve()),
        "memfiles_dir": str(get_memfiles_dir(config.agent_name).resolve()),
        # ── 多代理通信 (A2A) ──
        "a2a_configured": a2a is not None and a2a.enabled,
        "a2a_transport": (a2a.transport if a2a else "mqtt"),
        "a2a_broker_host": (a2a.broker_host if a2a else "localhost"),
        "a2a_broker_port": (a2a.broker_port if a2a else 1883),
        # ── 自主心跳 ──
        "heartbeat_interval": config.heartbeat_interval,
        # ── 子 agent 身份（agent.j2 不使用）──
        "subagent_name": os.environ.get("SLIFE_SUBAGENT_NAME", ""),
        "created_at": os.environ.get("SLIFE_SUBAGENT_CREATED_AT", ""),
        "context_source": os.environ.get("SLIFE_SUBAGENT_CONTEXT", "clean"),
    }


def build_context_status(
    context_window: int = 0,
    last_context_tokens: int = 0,
    model_name: str = "",
    input_modalities: str = "",
    cwd: str = "",
    shell: str = "",
    context_time_start: str = "",
    presence_events: list[tuple[float, str]] | None = None,
    schedule_status: list[dict] | None = None,
) -> str:
    """Render the dynamic context status footer.

    Time and token are always shown.  Model, CWD, shell are only
    passed (and rendered) when they changed since the last turn.
    *context_time_start* is passed every turn — it shows what time
    window the current context covers, and is updated after restore
    and after each trim.

    *presence_events* is a list of ``(epoch_seconds, text)`` pairs for
    peer agents that came online / went offline / timed out since the
    last turn.  Each line's timestamp uses the same ``%Y-%m-%d %H:%M:%S``
    format as *current_datetime*.  ``text`` is the TUI's own line
    (via ``format_presence_line``), so the context shows exactly what
    the user saw.

    *schedule_status* is a list of open ``{name, due_at, status}`` scheduled
    runs (failed and missed — the same "backfill or skip?" question to the
    user; ``status`` tells them apart per line) rendered after the context
    usage line until the user backfills with ``run_schedule_now`` or
    closes them with ``scheduled_run_skip``.
    """
    now = datetime.now().astimezone()
    last_usage_pct = (
        round(last_context_tokens / context_window * 100, 1)
        if context_window > 0 and last_context_tokens > 0
        else 0
    )
    rendered_presence: list[dict[str, str]] = []
    if presence_events:
        for epoch, text in presence_events:
            ev_time = datetime.fromtimestamp(epoch, tz=now.tzinfo).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rendered_presence.append({"time": ev_time, "text": text})
    rendered_schedule: list[dict] = [
        {"name": r.get("name", ""), "due_at": r.get("due_at", ""),
         "status": r.get("status", "")}
        for r in (schedule_status or [])
    ]
    return _env.get_template("context_status.j2").render(
        current_datetime=now.strftime("%Y-%m-%d %H:%M:%S"),
        utc_offset=now.strftime("%z"),
        last_context_tokens=f"{last_context_tokens:,}",
        last_usage_pct=last_usage_pct,
        model_name=model_name,
        context_window=f"{context_window:,}" if context_window else "",
        input_modalities=input_modalities,
        cwd=cwd,
        shell=shell,
        context_time_start=context_time_start,
        presence_events=rendered_presence,
        schedule_status=rendered_schedule,
    ).strip()


# ═══════════════════════════════════════════════════════════════════════
# Helpers — all platform facts are computed here, not imported from tools
# ═══════════════════════════════════════════════════════════════════════


def _memory_start_time(agent_name: str) -> str:
    """Earliest persisted turn time from the SQLite diary — ``""`` if none.

    This is the agent's true memory origin: the diary is append-only (old
    turns are evicted from the *context*, never deleted from the diary),
    so the earliest ``created_at`` is stable across trims and session
    restarts.  It belongs in the static system prompt, not the per-turn
    footer.  Reads directly (sync, bounded timeout) — no tool dependency.
    """
    try:
        from slife.paths import get_db_path

        db_path = get_db_path(agent_name)
        if not db_path.is_file():
            return ""
        import sqlite3

        con = sqlite3.connect(str(db_path), timeout=2)
        try:
            row = con.execute("SELECT MIN(created_at) FROM diary").fetchone()
            return row[0] if row and row[0] else ""
        finally:
            con.close()
    except Exception:
        return ""


def _platform_type() -> str:
    """``"native"`` | ``"wsl"`` | ``"headless"``."""
    if os.environ.get("SLIFE_SUBAGENT_NAME") or not sys.stdin.isatty():
        return "headless"
    if sys.platform == "linux":
        try:
            if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
                return "wsl"
        except OSError:
            pass
        try:
            with open("/proc/version", encoding="ascii", errors="replace") as f:
                content = f.read().lower()
                if "microsoft" in content or "wsl" in content:
                    return "wsl"
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return "native"


def _os_name() -> str:
    """Human-readable OS name: ``"Windows"`` | ``"Linux"`` | ``"macOS"``."""
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    if system == "Linux":
        return "Linux"
    return system


def _os_version() -> str:
    """Human-readable OS version (not the raw kernel build string).

    On Windows ``platform.uname().release`` is the NT build number
    (e.g. ``"10.0.26200"``), which is meaningless to a model.  Map it to
    the marketing name; on macOS use the product version; elsewhere fall
    back to the kernel release.
    """
    system = platform.system()
    if system == "Windows":
        release = platform.uname().release  # e.g. "10.0.26200"
        try:
            build = int(release.split(".")[-1])
        except (ValueError, IndexError):
            return release
        return "11" if build >= 22000 else "10"
    if system == "Darwin":
        return platform.mac_ver()[0] or platform.uname().release
    return platform.uname().release
