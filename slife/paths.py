"""Canonical filesystem paths for slife.

Dev mode (detected via ``pyproject.toml`` in CWD): everything lives in the
project root.  Production: everything lives in ``~/.slife/``.

Import from here instead of calling ``os.environ.get("SLIFE_…")`` directly.
"""

import os
import sys
from pathlib import Path

def is_dev() -> bool:
    """Check whether we're running from the slife source tree.

    Dev = the loaded slife package is a source dir under the CWD (a repo
    checkout), not the installed wheel in site-packages.  The old check (any
    ``pyproject.toml`` named ``slife`` in the CWD) misclassified a production
    install launched from inside a repo checkout as dev, scattering session
    data into that CWD.
    """
    try:
        mod = sys.modules.get("slife")
        f = getattr(mod, "__file__", None)
        if not isinstance(f, str) or not f:
            return False
        pkg_dir = Path(f).resolve().parent
        return pkg_dir.is_relative_to(Path.cwd().resolve())
    except Exception:
        return False


def get_data_dir() -> Path:
    """Root directory for all slife data.

    Production: ``~/.slife/``
    Dev mode:   CWD (project root)
    """
    env = os.environ.get("SLIFE_DATA_DIR")
    if env:
        return Path(env)
    if is_dev():
        return Path.cwd()
    return Path.home() / ".slife"


def get_config_path() -> Path:
    """Path to ``slife.json5``."""
    return get_data_dir() / "slife.json5"


def get_logs_dir() -> Path:
    """Directory for per-session log files."""
    return get_data_dir() / "logs"


def get_db_path(agent_name: str = "slife") -> Path:
    """Path to the SQLite memory database for *agent_name*.

    Uses ``SLIFE_AGENT_NAME`` from the environment when available (mirrors
    :func:`get_memfiles_dir`), falling back to ``"slife"`` — so health tools
    and any caller that omits the agent resolve the right per-agent database
    instead of always reporting the default ``slife.db``.
    """
    agent = os.environ.get("SLIFE_AGENT_NAME", agent_name)
    return get_data_dir() / f"{agent}.db"


def get_venv_python() -> str:
    """Return the Python executable path for the current venv.

    In production this is the slife tool's isolated venv Python
    (e.g. ``~/.uv/tools/slife/Scripts/python.exe``).
    In dev it's whatever ``sys.executable`` points to.
    """
    return sys.executable


def get_skills_dir() -> Path:
    """Directory containing skill subdirectories.

    Always ``<data_dir>/skills/`` — ``~/.slife/skills/`` in production,
    ``<project_root>/skills/`` in dev mode.
    """
    return get_data_dir() / "skills"


def get_images_dir() -> Path:
    """Directory for cached image files.

    Images are written here by ``show_image`` / MCP tools for immediate
    TUI rendering.  Lives under ``logs/`` so it is git-ignored and treated
    as ephemeral runtime output alongside session logs.  On session restore
    images are re‑resolved from disk — files that no longer exist show a
    ``⚠`` placeholder.
    """
    return get_data_dir() / "logs" / "images"


def get_memfiles_dir(agent_name: str = "slife") -> Path:
    """Directory for user-saved files — one per agent.

    Files saved via the memfiles plugin (``file_save`` / ``url_save`` /
    ``note_save`` / ``diary_write``) land here — plain files browsable by the
    user and accessible via both local path and sharing URL.

    Uses ``SLIFE_AGENT_NAME`` from the environment when available (mirrors
    :func:`get_db_path`), falling back to ``"slife"``.
    """
    agent = os.environ.get("SLIFE_AGENT_NAME", agent_name)
    return get_data_dir() / f"{agent}.files"
