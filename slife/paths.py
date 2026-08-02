"""Canonical filesystem paths for slife.

Dev mode (detected via ``pyproject.toml`` in CWD): everything lives in the
project root.  Production: everything lives in ``~/.slife/``.

Import from here instead of calling ``os.environ.get("SLIFE_…")`` directly.
"""

import os
import sys
from pathlib import Path

def is_dev() -> bool:
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
    if is_dev():
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


def get_venv_python() -> str:
    """Return the Python executable path for the current venv.

    In production this is the slife tool's isolated venv Python
    (e.g. ``~/.uv/tools/slife/Scripts/python.exe``).
    In dev it's whatever ``sys.executable`` points to.
    """
    return sys.executable


def get_environment_info() -> dict:
    """Return structured environment description.

    Used by the system prompt template and ``check_workspace`` to keep
    environment descriptions in a single place.
    """
    if is_dev():
        return {
            "mode": "development",
            "hint": "uv managed project (editable workspace).",
        }
    return {
        "mode": "production",
        "hint": (
            "isolated venv (uv tool install). "
            "Add packages via: uv pip install --python "
            + str(get_venv_python()) +
            " <pkg>. "
            "⚠️  Do NOT run `uv tool install slife` yourself or kill the "
            "slife process — both will terminate this agent."
        ),
    }


def get_skills_dir() -> Path:
    """Directory containing skill subdirectories.

    Always ``<data_dir>/skills/`` — ``~/.slife/skills/`` in production,
    ``<project_root>/skills/`` in dev mode.
    """
    return get_data_dir() / "skills"


def get_images_dir() -> Path:
    """Directory for cached image files.

    Images are written here by ``show_image`` for immediate TUI rendering
    and reconstructed from the BLOB table during session restore.  Lives
    under ``logs/`` so it is git-ignored and treated as ephemeral runtime
    output alongside session logs.
    """
    return get_data_dir() / "logs" / "images"
