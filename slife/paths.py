"""Canonical filesystem paths for slife.

Dev mode (detected via ``pyproject.toml`` in CWD): everything lives in the
project root.  Production: everything lives in ``~/.slife/``.

Import from here instead of calling ``os.environ.get("SLIFE_…")`` directly.
"""

import os
from pathlib import Path


def _is_dev() -> bool:
    """Check whether we're running from the slife source tree."""
    try:
        import tomllib

        data = tomllib.loads(
            Path("pyproject.toml").read_text(encoding="utf-8")
        )
        return data.get("project", {}).get("name") == "slife"
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
    if _is_dev():
        return Path.cwd()
    return Path.home() / ".slife"


def get_config_path() -> Path:
    """Path to ``slife.json5``."""
    return get_data_dir() / "slife.json5"


def get_logs_dir() -> Path:
    """Directory for per-session log files."""
    return get_data_dir() / "logs"


def get_db_path(agent_id: str = "slife") -> Path:
    """Path to the SQLite memory database for *agent_id*."""
    return get_data_dir() / f"{agent_id}.db"


def get_skills_dir() -> Path:
    """Directory containing skill subdirectories.

    In production, skills are bundled inside the installed slife package.
    In dev mode, they're at ``<project_root>/skills/``.
    """
    pkg_skills = Path(__file__).resolve().parent / "skills"
    if pkg_skills.is_dir():
        return pkg_skills
    return get_data_dir() / "skills"
