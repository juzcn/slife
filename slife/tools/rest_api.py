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

_rest_api_mcp_client: object | None = None


def set_rest_api_mcp_client(client: object) -> None:
    global _rest_api_mcp_client
    _rest_api_mcp_client = client


def _rest_api_section(raw: dict) -> dict:
    section = raw.setdefault(_REST_APIS_KEY, {})
    if not isinstance(section, dict):
        section = {}
        raw[_REST_APIS_KEY] = section
    return section


def get_rest_apis_summary(config_path: Path) -> str:
    raw = read_config(config_path)
    rest_apis = raw.get(_REST_APIS_KEY, {})
    if not isinstance(rest_apis, dict) or not rest_apis:
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


class RestApiAddTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_add"
    _subagent_skip = True
    description = (
        "Register an external REST API from its OpenAPI spec. "
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

        raw = read_config(self._config_path)
        section = _rest_api_section(raw)

        if name in section:
            return f"REST API '{name}' is already registered. Use rest_api_remove first."

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

        result_lines = [
            f"[OK] Registered REST API '{name}'.",
            f"  spec: {spec_url}",
            f"  base_url: {base_url}",
        ]
        if api_key:
            result_lines.append(f"  auth: credential ${{{api_key}}}")
        if description:
            result_lines.append(f"  description: {description}")

        if _rest_api_mcp_client is not None:
            try:
                mcp_result = await _rest_api_mcp_client.call_tool(  # type: ignore[union-attr]
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
        raw = read_config(self._config_path)
        rest_apis = raw.get(_REST_APIS_KEY, {})

        if not isinstance(rest_apis, dict) or name not in rest_apis:
            return f"REST API '{name}' is not registered."

        del rest_apis[name]
        write_config(self._config_path, raw)
        logger.info("rest_api_removed name=%s", name)

        if _rest_api_mcp_client is not None:
            try:
                await _rest_api_mcp_client.call_tool("mcp_remove_server", {"name": name})  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("rest_api_remove_mcp_failed name=%s err=%s", name, e)

        return f"[OK] Removed REST API '{name}'."


class RestApiListTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_list"
    description = "List registered REST APIs with specs, base URLs, and auth."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
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
        raw = read_config(self._config_path)
        entries = raw.get("rest_apis", {})
        if not isinstance(entries, dict) or name not in entries:
            return f"'{name}' not found in rest_apis."
        entry = entries[name]
        if not isinstance(entry, dict):
            return f"'{name}' in rest_apis is malformed."
        entry["enabled"] = enabled
        write_config(self._config_path, raw)

        if _rest_api_mcp_client is not None:
            try:
                await _rest_api_mcp_client.call_tool("mcp_set_server", {"name": name, "enabled": enabled})  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("rest_api_set_mcp_failed name=%s err=%s", name, e)

        state = "enabled" if enabled else "disabled"
        logger.info("rest_api_set name=%s enabled=%s", name, enabled)
        return f"[OK] REST API '{name}' {state}."
