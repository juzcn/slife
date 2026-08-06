"""REST API management — register external APIs backed by anyapi-mcp-server.

rest_api_add / rest_api_remove / rest_api_list.
Persisted to slife.json5 → rest_apis: section.
"""

import logging
from pathlib import Path

from slife.tools._config_io import (
    _ConfigPathMixin,
    format_source_info,
    read_config,
    write_config,
)
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_REST_APIS_KEY = "rest_apis"


def _rest_api_section(raw: dict) -> dict:
    section = raw.setdefault(_REST_APIS_KEY, {})
    if not isinstance(section, dict):
        section = {}
        raw[_REST_APIS_KEY] = section
    return section


def _format_rest_apis(rest_apis: dict) -> str:
    """Format a rest_apis dict into a human-readable summary."""
    if not rest_apis:
        return "No REST APIs registered."

    lines = []
    for name, cfg in rest_apis.items():
        if not isinstance(cfg, dict):
            continue
        spec = cfg.get("spec_url", "")
        base = cfg.get("base_url", "")
        desc = cfg.get("description", "(no description)")
        api_key = cfg.get("api_key", "")
        source = cfg.get("source")

        line = f"- **{name}**: {desc}\n  spec: `{spec}`\n  base_url: `{base}`"
        if api_key:
            line += f"\n  auth: `${{{api_key}}}`"
        src_str = format_source_info(source)
        if src_str:
            line += f"\n  source: {src_str}"
        lines.append(line)

    return "\n".join(lines)


def get_rest_apis_summary(config_path: Path) -> str:
    """Read rest_apis from file (fallback when Config is not available)."""
    raw = read_config(config_path)
    rest_apis = raw.get(_REST_APIS_KEY, {})
    if not isinstance(rest_apis, dict) or not rest_apis:
        return "No REST APIs registered."
    return _format_rest_apis(rest_apis)


class RestApiAddTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_add"
    category = "REST API"
    _subagent_skip = True
    description = (
        "Register or update an external REST API from its OpenAPI spec (upsert — idempotent). "
        "Auto-generates typed tools for every endpoint. "
        "For authenticated APIs pass the credential variable name "
        "(e.g. 'GITHUB_TOKEN') — set via credstore first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name. Generated tools are prefixed name__tool.",
            },
            "spec_url": {
                "type": "string",
                "description": "OpenAPI spec URL (JSON or YAML).",
            },
            "base_url": {
                "type": "string",
                "description": "API base URL, e.g. https://api.github.com.",
            },
            "api_key": {
                "type": "string",
                "description": "Credential variable name for Bearer auth. Omit for public APIs.",
            },
            "description": {
                "type": "string",
                "description": "What this API does. Helps decide when to invoke its tools.",
            },
        },
        "required": ["name", "spec_url", "base_url"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        spec_url: str = kwargs["spec_url"]
        base_url: str = kwargs["base_url"]
        api_key: str = kwargs.get("api_key", "")
        description: str = kwargs.get("description", "")

        

        is_update = False
        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None:
            is_update = name in config.rest_apis
            config.save_rest_api(
                name=name, spec_url=spec_url, base_url=base_url,
                api_key=api_key, description=description,
            )
        else:
            # Fallback: write directly to file (e.g. in tests)
            raw = read_config(self._config_path)
            section = _rest_api_section(raw)
            is_update = name in section
            entry: dict = {"spec_url": spec_url, "base_url": base_url}
            if api_key:
                entry["api_key"] = api_key
            if description:
                entry["description"] = description
            section[name] = entry
            write_config(self._config_path, raw)

        logger.info("rest_api_saved name=%s spec=%s", name, spec_url)

        mcp_args = [
            "-y", "anyapi-mcp-server",
            "--name", name,
            "--spec", spec_url,
            "--base-url", base_url,
        ]
        if api_key:
            mcp_args.extend(["--header", f"Authorization: Bearer ${{{api_key}}}"])

        action = "Updated" if is_update else "Registered"
        result_lines = [
            f"[OK] {action} REST API '{name}'.",
            f"  spec: {spec_url}",
            f"  base_url: {base_url}",
        ]
        if api_key:
            result_lines.append(f"  auth: credential ${{{api_key}}}")
        if description:
            result_lines.append(f"  description: {description}")

        mcp = getattr(ctx, "rest_api_mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                mcp_result = await mcp.call_tool(  # type: ignore[union-attr]
                    "mcp_add_server",
                    {
                        "name": name,
                        "command": "npx",
                        "args": mcp_args,
                        "description": description,
                        "activate": True,
                    },
                )
                result_lines.append(f"\n{mcp_result}")
            except Exception as e:
                logger.warning("rest_api_mcp_connect_failed name=%s err=%s", name, e)
                result_lines.append(
                    f"\nConfig saved but connect failed: {e}. "
                    f"Will auto-connect on restart."
                )
        else:
            result_lines.append(
                "\nMCP gateway not ready. Config saved — will auto-connect on restart."
            )

        return "\n".join(result_lines)


class RestApiRemoveTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_remove"
    category = "REST API"
    _subagent_skip = True
    description = "Unregister a REST API. Disconnects and removes from config."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "API name, from rest_api_list."},
        },
        "required": ["name"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]

        

        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None:
            if name not in config.rest_apis:
                return f"REST API '{name}' is not registered."
            config.remove_rest_api(name)
        else:
            # Fallback: write directly to file
            raw = read_config(self._config_path)
            rest_apis = raw.get(_REST_APIS_KEY, {})
            if not isinstance(rest_apis, dict) or name not in rest_apis:
                return f"REST API '{name}' is not registered."
            del rest_apis[name]
            write_config(self._config_path, raw)

        logger.info("rest_api_removed name=%s", name)

        mcp = getattr(ctx, "rest_api_mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                await mcp.call_tool("mcp_remove_server", {"name": name})  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("rest_api_remove_mcp_failed name=%s err=%s", name, e)

        return f"[OK] Removed REST API '{name}'."


class RestApiListTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_list"
    category = "REST API"
    description = "List registered REST APIs with specs, base URLs, and auth."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        

        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None and config.rest_apis:
            return _format_rest_apis(config.rest_apis)
        return get_rest_apis_summary(self._config_path)


# ═══════════════════════════════════════════════════════════════════════
# rest_api_set
# ═══════════════════════════════════════════════════════════════════════


class RestApiSet(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_set"
    category = "REST API"
    _subagent_skip = True
    description = "Enable or disable a REST API. Connects/disconnects immediately."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "API name, from rest_api_list."},
            "enabled": {"type": "boolean", "description": "Enable or disable."},
        },
        "required": ["name", "enabled"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        enabled: bool = kwargs["enabled"]

        

        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None:
            if name not in config.rest_apis:
                return f"'{name}' not found in rest_apis."
            entry = config.rest_apis[name]
            if not isinstance(entry, dict):
                return f"'{name}' in rest_apis is malformed."
            entry["enabled"] = enabled
            config.save_rest_api(
                name=name,
                spec_url=entry.get("spec_url", ""),
                base_url=entry.get("base_url", ""),
                api_key=entry.get("api_key", ""),
                description=entry.get("description", ""),
                source=entry.get("source"),
            )
        else:
            raw = read_config(self._config_path)
            entries = raw.get("rest_apis", {})
            if not isinstance(entries, dict) or name not in entries:
                return f"'{name}' not found in rest_apis."
            entry = entries[name]
            if not isinstance(entry, dict):
                return f"'{name}' in rest_apis is malformed."
            entry["enabled"] = enabled
            write_config(self._config_path, raw)

        mcp = getattr(ctx, "rest_api_mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                await mcp.call_tool("mcp_set_server", {"name": name, "enabled": enabled})  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("rest_api_set_mcp_failed name=%s err=%s", name, e)

        state = "enabled" if enabled else "disabled"
        logger.info("rest_api_set name=%s enabled=%s", name, enabled)
        return f"[OK] REST API '{name}' {state}."
