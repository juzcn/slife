"""Install Python packages into the slife environment.

Gives the agent a clean way to install missing dependencies without
guessing at shell commands (pip vs uv pip vs pip install --system etc.).
"""

import asyncio
import logging
import sys

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class InstallPythonPackageTool(Tool):
    """Install a Python package into slife's environment via uv."""

    name = "install_python_package"
    description = (
        "Install one or more Python packages into slife's Python environment. "
        "Use this when a script fails with ModuleNotFoundError. "
        "Packages are installed via uv pip install — fast, reliable, no guessing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "string",
                "description": (
                    "Package name(s) to install, space-separated. "
                    "Supports version pins: 'requests>=2.31' or 'requests==2.31.0'. "
                    "Example: 'requests' or 'requests beautifulsoup4'"
                ),
            },
        },
        "required": ["packages"],
    }

    async def execute(self, **kwargs) -> str:
        packages_str = kwargs["packages"].strip()
        if not packages_str:
            return "Error: no package names provided."

        packages = packages_str.split()
        logger.info("pip_install packages=%s", packages)

        proc = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", "--python", sys.executable, *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            logger.info("pip_install_done packages=%s", packages)
            return out or f"✓ Installed: {', '.join(packages)}"
        else:
            logger.warning("pip_install_failed packages=%s err=%s", packages, err)
            return f"Error installing {', '.join(packages)}:\n{err}" if err else f"Error installing {', '.join(packages)} (exit {proc.returncode})"
