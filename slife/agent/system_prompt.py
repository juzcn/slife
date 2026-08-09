"""System prompt builder — renders the Slife runtime spec sheet.

Only project-specific facts the LLM cannot discover from training data or
tool schemas.  No personality, no instructions, no decoration.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

from slife.paths import (
    get_config_path,
    get_data_dir,
    get_db_path,
    get_images_dir,
    get_logs_dir,
    get_skills_dir,
)

if TYPE_CHECKING:
    from slife.config import Config

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def build(config: Config) -> str:
    """Render the system prompt from the runtime spec sheet template.

    All platform facts are computed here — no dependency on
    ``slife.tools`` or any tool module.
    """
    model = config.active_model
    now = datetime.now().astimezone()
    a2a = config.a2a_config
    agent_name: str = (a2a.agent_name if a2a else "") or config.agent_id

    return _env.get_template("system_prompt.j2").render(
        # ── 1. 环境 ──
        agent_name=agent_name,
        model_name=model.display_name,
        context_window=f"{model.context_window:,}",
        input_modalities=", ".join(model.input_modalities),
        hostname=socket.gethostname(),
        platform_type=_platform_type(),
        platform_name=_os_name(),
        os_version=platform.uname().release,
        arch=platform.machine(),
        workspace=os.getcwd(),
        default_shell=_current_shell(),
        python_cmd=sys.executable,
        python_version=sys.version.split()[0],
        package_manager="uv",
        current_datetime=now.strftime("%Y-%m-%d %H:%M:%S"),
        utc_offset=now.strftime("%z"),
        # ── 2. 上下文窗口策略 ──
        context_floor=int(config.context_floor * 100),
        context_ceiling=int(config.context_ceiling * 100),
        tool_result_max_percent=int(config.tool_result_ceiling * 100),
        # ── 3. 图像与多模态 ──
        has_vision=model.supports_vision,
        # ── 4. 凭证解析链 ──
        credstore_backend=_credstore_backend(),
        # ── 5. 工具与技能 ──
        mcp_tool_prefix="server_name__",
        skills_directory=str(get_skills_dir().resolve()),
        data_dir=str(get_data_dir().resolve()),
        config_path=str(get_config_path().resolve()),
        logs_dir=str(get_logs_dir().resolve()),
        db_path=str(get_db_path(config.agent_id).resolve()),
        images_dir=str(get_images_dir().resolve()),
        # ── 6. 多代理通信 (A2A) ──
        a2a_configured=a2a is not None and a2a.enabled,
        a2a_transport=(a2a.transport if a2a else "mqtt"),
        a2a_broker_host=(a2a.broker_host if a2a else "localhost"),
        a2a_broker_port=(a2a.broker_port if a2a else 1883),
    ).strip()


def build_context_status(
    context_window: int = 0,
    last_context_tokens: int = 0,
    model_name: str = "",
    input_modalities: str = "",
    cwd: str = "",
    shell: str = "",
    context_time_start: str = "",
    presence_events: list[tuple[float, str]] | None = None,
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
    ).strip()


# ═══════════════════════════════════════════════════════════════════════
# Helpers — all platform facts are computed here, not imported from tools
# ═══════════════════════════════════════════════════════════════════════


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


def _current_shell() -> str:
    """Detect the shell that launched slife."""
    if os.name != "nt":
        return os.environ.get("SHELL", "sh")
    if os.environ.get("PSModulePath"):
        return "powershell"
    return "cmd"


def _credstore_backend() -> str:
    """Safe lookup of the active credstore backend name."""
    try:
        from credstore import get_backend_name
        return get_backend_name()
    except ImportError:
        return "不可用"
    except Exception:
        return "未知"
