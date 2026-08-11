"""Credential management tools.

credential_check    — verify credential in shell/config/keyring
credential_inject   — load secret from keyring into os.environ
credential_uninject — remove secret from os.environ (keyring untouched)
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _mask_value(value: str) -> str:
    if len(value) > 8:
        return f"{value[:4]}…{value[-4:]}"
    return "***"


def _simplify_path(path: str) -> str:
    if path.endswith("/env"):
        path = path[:-4]
    return path.lstrip("/")


def _scan_json5(node, key: str, path: str, refs: list[str]) -> None:
    target = f"${{{key}}}"
    if isinstance(node, dict):
        for k, v in node.items():
            child_path = f"{path}/{k}" if path else k
            if isinstance(v, str) and target in v:
                refs.append(_simplify_path(path) if path else k)
            elif isinstance(v, (dict, list)):
                _scan_json5(v, key, child_path, refs)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str) and target in item:
                refs.append(_simplify_path(path) if path else f"[{i}]")
            elif isinstance(item, (dict, list)):
                _scan_json5(item, key, f"{path}[{i}]", refs)


def _find_json5_refs(raw: dict, key: str) -> list[str]:
    refs: list[str] = []
    _scan_json5(raw, key, "", refs)
    return refs


# ═══════════════════════════════════════════════════════════════════════

class CredentialCheckTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "credential_check"
    category: ClassVar[str] = "Credentials"
    description = "Check credential in shell, slife.json5, and OS keyring. Values always masked."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Credential name, e.g. DEEPSEEK_API_KEY."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        from credstore import get_credential
        lines = [f"{key} status:"]
        env_val = os.environ.get(key)
        lines.append(f"  [shell]      : {'✓ set (' + _mask_value(env_val) + ')' if env_val else '✗ not set'}")
        raw = read_config(self._config_path)
        refs = _find_json5_refs(raw, key)
        lines.append(f"  [slife.json5]: {'✓ referenced (' + ', '.join(refs) + ')' if refs else '✗ not referenced'}")
        cred_val = get_credential(key)
        lines.append(f"  [credstore]  : {'✓ stored (' + _mask_value(cred_val) + ')' if cred_val else '✗ not stored'}")
        return "\n".join(lines)


class InjectCredentialTool(Tool):
    name = "credential_inject"
    category: ClassVar[str] = "Credentials"
    description = "Load a secret from OS keyring into os.environ. Temporary, this process only."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Credential name, e.g. DEEPSEEK_API_KEY."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        from credstore import get_credential
        value = get_credential(key)
        if value is None:
            return f"Error: '{key}' not found in the OS keyring."
        os.environ[key] = value
        del value
        return f"Set {key} from keyring (temporary, this process only)."


class UninjectCredentialTool(Tool):
    name = "credential_uninject"
    category: ClassVar[str] = "Credentials"
    description = "Remove an env var from os.environ. Keyring secret remains stored."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name, e.g. DEEPSEEK_API_KEY."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        existed = key in os.environ
        os.environ.pop(key, None)
        return f"Removed {key} from environment." if existed else f"{key} was not set in the environment."
