"""Config & credential management tools.

Tools:
    config_env_set           — write any env var to slife.json5
    config_env_get           — read env var config (shell → slife.json5)
    config_env_remove        — remove an env var from slife.json5
    credential_check         — verify credential in shell/config/keyring
    inject_credential        — load secret from keyring into os.environ
    uninject_credential      — remove secret from os.environ (keyring untouched)
"""

from __future__ import annotations

import logging
import os

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _env_section(raw: dict) -> dict:
    env = raw.setdefault("env", {})
    if not isinstance(env, dict):
        logger.warning("env_config_not_dict")
        env = {}
        raw["env"] = env
    return env


def _mcp_env_sections(raw: dict) -> dict[str, dict]:
    """Extract per-server env dicts from ``mcp.servers.<name>.env``."""
    result: dict[str, dict] = {}
    servers = raw.get("mcp", {}).get("servers", {})
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                server_env = cfg.get("env", {})
                if isinstance(server_env, dict) and server_env:
                    result[name] = dict(server_env)
    return result


def _lookup_one(key: str, env: dict, mcp_envs: dict[str, dict]) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"{key} = {env_val} [shell]"

    sources = []
    config_val = env.get(key)
    if config_val and config_val not in (None, ""):
        sources.append(("slife.json5", str(config_val)))
    for server_name, server_env in sorted(mcp_envs.items()):
        val = server_env.get(key)
        if val and val not in (None, ""):
            sources.append((f"mcp/{server_name}", str(val)))

    if not sources:
        return f"'{key}' is not set."

    lines = [f"{key}:"]
    for source_name, value in sources:
        marker = " ← active" if source_name == sources[0][0] else ""
        lines.append(f"  [{source_name}]{marker}: {value}")
    return "\n".join(lines)


def _format_one(key: str, value: str) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"  {key} = {env_val} [shell]"
    is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
    note = " [PLACEHOLDER]" if is_placeholder else " [unset]"
    return f"  {key} = {value}{note}"


# ═══════════════════════════════════════════════════════════════════════
# config_env_set
# ═══════════════════════════════════════════════════════════════════════

class ConfigEnvSetTool(_ConfigPathMixin, Tool):
    """Write an env var to slife.json5. For any non-secret value; use ${VAR} references for secrets."""

    name = "config_env_set"
    _subagent_skip = True
    description = (
        "Write an env var to slife.json5. For any non-secret value "
        "(EDITOR, LANG, etc.) or a ${VAR} reference to a credstore secret. "
        "Omit value to write a <YOUR_VAR> placeholder for the user to fill in. "
        "Never write plaintext secrets — use credstore set on the CLI for those."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name, e.g. EDITOR, DEEPSEEK_API_KEY."},
            "value": {"type": "string", "description": "Value to set. Omit to write a placeholder."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str | None = kwargs.get("value")
        raw = read_config(self._config_path)
        env = _env_section(raw)
        if value:
            env[key] = value
            os.environ[key] = str(value)
            write_config(self._config_path, raw)
            logger.info("env_set key=%s", key)
            return f"[OK] {key} = {value}"
        else:
            placeholder = f"<YOUR_{key.upper().strip('<>')}>"
            env[key] = placeholder
            write_config(self._config_path, raw)
            logger.info("env_set_placeholder key=%s", key)
            return f"[OK] {key} placeholder written.\nEdit slife.json5 → env: → {key} with the real value."


# ═══════════════════════════════════════════════════════════════════════
# config_env_get
# ═══════════════════════════════════════════════════════════════════════

class ConfigEnvGetTool(_ConfigPathMixin, Tool):
    """Read env var config: shell → slife.json5. Does NOT query the OS keyring."""

    name = "config_env_get"
    description = (
        "Look up an env var: shell value first, then slife.json5 entry. "
        "${VAR} placeholders are shown as-is, never resolved to secrets. "
        "Omit key to list all configured vars across env: and MCP servers. "
        "For secrets use credential_check."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name to look up. Omit to list all."},
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        raw = read_config(self._config_path)
        env = _env_section(raw)
        mcp_envs = _mcp_env_sections(raw)

        if key:
            return _lookup_one(key, env, mcp_envs)

        lines = []
        if env:
            lines.append("env:")
            for k in sorted(env.keys()):
                lines.append(_format_one(k, env.get(k, "")))
        else:
            lines.append("env: (empty)")
        for server_name, server_env in sorted(mcp_envs.items()):
            lines.append(f"mcp/{server_name}:")
            for k in sorted(server_env.keys()):
                lines.append(_format_one(k, server_env.get(k, "")))
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# config_env_remove
# ═══════════════════════════════════════════════════════════════════════

class ConfigEnvRemoveTool(_ConfigPathMixin, Tool):
    """Remove an env var entry from slife.json5. Keyring secrets are unaffected."""

    name = "config_env_remove"
    _subagent_skip = True
    description = (
        "Remove an env var from slife.json5 only. Does NOT touch the OS "
        "keyring — secrets stored via credstore remain safe."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name to remove from slife.json5."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        raw = read_config(self._config_path)
        env = _env_section(raw)
        if key not in env:
            return f"'{key}' is not in slife.json5 — nothing to remove."
        del env[key]
        write_config(self._config_path, raw)
        logger.info("env_removed key=%s", key)
        return f"[OK] Removed '{key}' from slife.json5."


# ═══════════════════════════════════════════════════════════════════════
# Credential helpers
# ═══════════════════════════════════════════════════════════════════════

def _mask_value(value: str) -> str:
    """Mask a credential value — first 4 + last 4."""
    if len(value) > 8:
        return f"{value[:4]}…{value[-4:]}"
    return "***"


def _simplify_path(path: str) -> str:
    """Convert a raw slash-path to a display-friendly form."""
    if path.endswith("/env"):
        path = path[:-4]
    return path.lstrip("/")


def _scan_json5(node, key: str, path: str, refs: list[str]) -> None:
    """Recursively scan *node* for ``${KEY}`` in string values."""
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
    """Scan a slife.json5 dict for ``${KEY}`` references."""
    refs: list[str] = []
    _scan_json5(raw, key, "", refs)
    return refs


# ═══════════════════════════════════════════════════════════════════════
# credential_check
# ═══════════════════════════════════════════════════════════════════════

class CredentialCheckTool(_ConfigPathMixin, Tool):
    """Check where a credential exists: shell env, slife.json5, OS keyring. Never exposes secrets."""

    requires_a2a = False
    name = "credential_check"
    description = (
        "Check a credential across three sources: shell environment, "
        "slife.json5 (${VAR} references), and OS keyring. "
        "Values are always masked (sk-a…B3f2). Call before inject_credential."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Credential name, e.g. DEEPSEEK_API_KEY, GITHUB_TOKEN."},
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


# ═══════════════════════════════════════════════════════════════════════
# inject_credential
# ═══════════════════════════════════════════════════════════════════════

class InjectCredentialTool(Tool):
    """Load a secret from OS keyring into os.environ — temporary, this process only."""

    requires_a2a = False
    name = "inject_credential"
    description = (
        "Load a secret from the OS keyring into the current process environment. "
        "Temporary — gone on exit. Return value confirms success only, "
        "never contains the secret. Call credential_check first to verify."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Credential name to load from keyring, e.g. DEEPSEEK_API_KEY."},
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


# ═══════════════════════════════════════════════════════════════════════
# uninject_credential
# ═══════════════════════════════════════════════════════════════════════

class UninjectCredentialTool(Tool):
    """Remove an env var from the current process. Keyring secret is untouched."""

    requires_a2a = False
    name = "uninject_credential"
    description = (
        "Remove an env var from os.environ in the current process only. "
        "The keyring secret remains stored. Use after inject_credential "
        "when the secret is no longer needed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var to remove, e.g. DEEPSEEK_API_KEY."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        existed = key in os.environ
        os.environ.pop(key, None)
        return f"Removed {key} from environment." if existed else f"{key} was not set in the environment."
