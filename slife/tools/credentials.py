"""Credential management tools for the LLM agent.

Talk directly to the OS keyring via credstore.  credential_check returns
a comprehensive status across all sources — shell env, slife.json5
references, and OS keyring — so the LLM can verify where a credential
is configured without seeing the full secret.
Use config_secret_register to register secret env vars in slife.json5
(writes ${VAR} placeholder — user stores the real value via credstore CLI).
Use config_env_set / config_env_get for non-secret env vars.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from slife.tools._config_io import _ConfigPathMixin, read_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _mask_value(value: str) -> str:
    """Mask a credential value — first 4 + last 4."""
    if len(value) > 8:
        return f"{value[:4]}…{value[-4:]}"
    return "***"


def _find_json5_refs(raw: dict, key: str) -> list[str]:
    """Scan a slife.json5 dict for ``${KEY}`` references.

    Returns a list of human-readable paths where *key* is referenced
    (e.g. ``["env:", "models/providers/deepseek", "mcp/servers/github"]``).
    """
    refs: list[str] = []
    _scan_json5(raw, key, "", refs)
    return refs


def _scan_json5(node, key: str, path: str, refs: list[str]) -> None:
    """Recursively scan *node* for ``${KEY}`` in string values."""
    target = f"${{{key}}}"

    if isinstance(node, dict):
        for k, v in node.items():
            child_path = f"{path}/{k}" if path else k
            if isinstance(v, str) and target in v:
                # Use the parent path (e.g. "env:" or "mcp/servers/github")
                refs.append(_simplify_path(path) if path else k)
            elif isinstance(v, (dict, list)):
                _scan_json5(v, key, child_path, refs)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str) and target in item:
                refs.append(_simplify_path(path) if path else f"[{i}]")
            elif isinstance(item, (dict, list)):
                _scan_json5(item, key, f"{path}[{i}]", refs)


def _simplify_path(path: str) -> str:
    """Convert a raw slash-path to a display-friendly form.

    Examples::

        "env"                  → "env:"
        "models/providers/ds"  → "models/providers/ds"
        "mcp/servers/gh/env"   → "mcp/servers/gh"
    """
    # Strip trailing "/env" since the key being in env: is the main signal
    if path.endswith("/env"):
        path = path[:-4]
    # Strip leading "/" if present
    return path.lstrip("/")


class CredentialCheckTool(_ConfigPathMixin, Tool):
    """Check credential status across all sources — masked, LLM-safe.

    Reports where a credential exists (shell env, slife.json5 references,
    OS keyring) so the LLM can verify configuration before attempting
    operations that need the key.

    NEVER exposes the full secret — masked values show only first 4 +
    last 4 characters (e.g. ``sk-a…B3f2``).
    """

    requires_a2a = False

    name = "credential_check"
    description = (
        "Check credential status across all sources — LLM-safe, never "
        "exposes the full secret. "
        "Reports whether the key is set in: shell environment, "
        "slife.json5 (${VAR} references), and OS keyring. "
        "Use before running operations that need an API key — "
        "check first, then use inject_credential to load it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Credential key to check, e.g. 'DEEPSEEK_API_KEY', "
                    "'GITHUB_TOKEN'. Reports status across shell env, "
                    "slife.json5, and OS keyring — NEVER the full secret."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import get_credential

        lines = [f"{key} status:"]

        # ── 1. Shell environment ──────────────────────────────
        env_val = os.environ.get(key)
        if env_val:
            lines.append(f"  [shell]      : ✓ set ({_mask_value(env_val)})")
        else:
            lines.append(f"  [shell]      : ✗ not set")

        # ── 2. slife.json5 references ─────────────────────────
        raw = read_config(self._config_path)
        refs = _find_json5_refs(raw, key)
        if refs:
            locations = ", ".join(refs)
            lines.append(f"  [slife.json5]: ✓ referenced ({locations})")
        else:
            lines.append(f"  [slife.json5]: ✗ not referenced")

        # ── 3. OS keyring ────────────────────────────────────
        cred_val = get_credential(key)
        if cred_val:
            lines.append(f"  [credstore]  : ✓ stored ({_mask_value(cred_val)})")
        else:
            lines.append(f"  [credstore]  : ✗ not stored")

        return "\n".join(lines)


class InjectCredentialTool(Tool):
    """Load a secret from the OS keyring into the current process environment.

    Reads the secret from the keyring and sets it directly in os.environ
    — temporary, this process only, gone when the process exits.  The
    secret NEVER appears in the return value — the LLM only sees a
    confirmation.  Use credential_check first to verify the key exists.
    Use uninject_credential to clean up when done.
    """

    requires_a2a = False

    name = "inject_credential"
    description = (
        "Load a secret from the OS keyring into the current process "
        "environment (os.environ) — temporary, gone on exit. "
        "LLM-safe: return value is a confirmation ONLY, never the secret. "
        "The key becomes available to subprocesses spawned after injection. "
        "Use credential_check first to verify the key exists. "
        "Use uninject_credential to remove from the environment when done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Credential key to load from keyring, e.g. 'DEEPSEEK_API_KEY', "
                    "'GITHUB_TOKEN'. The secret is placed in os.environ — "
                    "NEVER returned to the LLM."
                ),
            },
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
    """Remove an env var from the current process — keyring is untouched.

    Only clears os.environ for this process.  Does NOT touch the OS
    keyring — the credential remains safely stored.  Use this to clean
    up after inject_credential when the secret is no longer needed.
    """

    requires_a2a = False

    name = "uninject_credential"
    description = (
        "Remove an env var from the current process environment only. "
        "Does NOT touch the OS keyring — the secret remains stored. "
        "Use to clean up after inject_credential when the key is no "
        "longer needed by subprocesses in this session."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var to remove from os.environ, e.g. 'DEEPSEEK_API_KEY'. "
                    "Only clears current process — keyring secret is unaffected."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        existed = key in os.environ
        os.environ.pop(key, None)
        if existed:
            return f"Removed {key} from environment."
        return f"{key} was not set in the environment."


