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

    Dev = the CWD is the project root (its ``pyproject.toml`` names the
    project) AND the loaded slife package is that checkout's source ``slife/``
    subdir — an editable install or ``python -m slife`` from the tree.  A
    production install (uv tool / pipx / pip) always loads from a site-packages
    dir whose parent is not the CWD, so it stays production no matter where the
    CWD is:

    * launched from inside a checkout — the checkout's ``pyproject.toml`` is
      in the CWD but the loaded package lives in site-packages (the old
      pyproject-only check misfired here, scattering data into the checkout);
    * launched from home — uv tools install under ``~/.local`` /
      ``%LOCALAPPDATA%``, i.e. *under* the CWD, so a package-under-CWD check
      would misfire here too (site-packages copy, not the tree).
    """
    try:
        import tomllib

        root = Path.cwd().resolve()
        data = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )
        if data.get("project", {}).get("name") != "slife":
            return False

        mod = sys.modules.get("slife")
        f = getattr(mod, "__file__", None)
        if not isinstance(f, str) or not f:
            return False
        pkg_dir = Path(f).resolve().parent
        # The loaded package must be this checkout's ``slife/`` subdir (editable
        # installs point back at the tree), not a site-packages copy.
        return pkg_dir.parent == root
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
    """Directory for per-session log files.

    Uses ``SLIFE_LOG_DIR`` from the environment when available (the main
    process exports it so plugin children — internal AND external — resolve
    the same directory without importing slife), falling back to
    ``<data_dir>/logs``.
    """
    env = os.environ.get("SLIFE_LOG_DIR")
    if env:
        return Path(env)
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


def get_jobs_dir() -> Path:
    """Directory containing job modules for the job-coding plugin.

    Each ``*.py`` here defines one or more plain functions that become
    MCP tools (job-coding convention: any public module-level function is a
    tool).  Always ``<data_dir>/jobs/`` — ``~/.slife/jobs/`` in production,
    ``<project_root>/jobs/`` in dev mode.
    """
    return get_data_dir() / "jobs"


def get_memfiles_dir(agent_name: str = "slife") -> Path:
    """Directory for user-saved files — one per agent.

    Files saved via the memfiles plugin (``file_save`` / ``url_save`` /
    ``note_save`` / ``diary_write``) land here — plain files browsable by the
    user.  Public sharing of a file is a separate concern handled by the
    sharefile plugin.

    Uses ``SLIFE_AGENT_NAME`` from the environment when available (mirrors
    :func:`get_db_path`), falling back to ``"slife"``.
    """
    agent = os.environ.get("SLIFE_AGENT_NAME", agent_name)
    return get_data_dir() / f"{agent}.files"
