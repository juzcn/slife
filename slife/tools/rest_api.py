"""REST API management — register external APIs backed by anyapi-mcp-server.

rest_api_set / rest_api_remove / rest_api_list / rest_api_set_enabled.

Server definitions live in ``mcp-plugin.json5`` (owned by ``mcp-plugin``,
resolved via ``$MCP_PLUGIN_FILE``); REST APIs are ordinary ``command: npx``
server entries tagged ``source.type == "rest_api"``.  This module is the
sLife-side face: it re-points persistence to :mod:`mcp_plugin.config` and
keeps a live ``mcp_set``-style warm-up through the mcp plugin so an API
connects immediately.
"""

import logging
from urllib.parse import urlparse

from mcp_plugin import config as mcp_plugin_config

from slife.tools._config_io import _ConfigPathMixin, format_source_info
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _validate_http_url(url: str, what: str) -> str:
    """Require *url* to be an ``http(s)`` URL with a host.

    ``spec_url`` / ``base_url`` are handed to ``anyapi-mcp-server``, which
    fetches them — an LLM-supplied ``file://`` or internal-host URL would
    otherwise be an SSRF vector.  (Private IPs are intentionally allowed:
    local APIs are a legitimate use.)
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"{what} must be an http(s) URL with a host, got {url!r}"
        )
    return url


def _format_rest_apis(rest_apis: dict) -> str:
    """Format a rest-api entries dict into a human-readable summary."""
    if not rest_apis:
        return "No REST APIs registered."

    lines = []
    for name, cfg in rest_apis.items():
        if not isinstance(cfg, dict):
            continue
        parsed = mcp_plugin_config.parse_anyapi_args(cfg)
        spec = parsed["spec_url"]
        base = parsed["base_url"]
        api_key = parsed["api_key"]
        desc = cfg.get("description", "(no description)")
        source = cfg.get("source")

        line = f"- **{name}**: {desc}\n  spec: `{spec}`\n  base_url: `{base}`"
        if api_key:
            line += f"\n  auth: `${{{api_key}}}`"
        src_str = format_source_info(source)
        if src_str:
            line += f"\n  source: {src_str}"
        lines.append(line)

    return "\n".join(lines)


def get_rest_apis_summary(config_path) -> str:
    """Read rest-api entries from mcp-plugin.json5 (fallback for offline use).

    The config path is resolved by mcp-plugin ($MCP_PLUGIN_FILE), so
    *config_path* is accepted for signature compatibility and ignored.
    """
    return _format_rest_apis(mcp_plugin_config.list_rest_apis())


class RestApiSetTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_set"
    category = "REST API"
    description = (
        "Register/update a REST API from an OpenAPI spec (upsert; generates "
        "typed per-endpoint tools)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name; generated tools are prefixed <name>__.",
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
                "description": "Credential var name for Bearer auth; omit for public APIs.",
            },
            "description": {
                "type": "string",
                "description": "What this API does (in the API's own language).",
            },
        },
        "required": ["name", "spec_url", "base_url"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        # Validate before persisting or spawning anyapi-mcp-server — an
        # LLM-supplied file:// or internal URL would be fetched by the npx
        # child.
        spec_url: str = _validate_http_url(kwargs["spec_url"], "spec_url")
        base_url: str = _validate_http_url(kwargs["base_url"], "base_url")
        api_key: str = kwargs.get("api_key", "")
        description: str = kwargs.get("description", "")

        is_update = name in mcp_plugin_config.list_rest_apis()
        mcp_plugin_config.save_rest_api(
            name=name, spec_url=spec_url, base_url=base_url,
            api_key=api_key, description=description,
        )
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

        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                mcp_result = await mcp.call_tool(  # type: ignore[union-attr]
                    "mcp_set",
                    {
                        "name": name,
                        "command": "npx",
                        "args": mcp_args,
                        "description": description,
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

        if not mcp_plugin_config.remove_rest_api(name):
            return f"REST API '{name}' is not registered."
        logger.info("rest_api_removed name=%s", name)

        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                await mcp.call_tool("mcp_remove", {"name": name})  # type: ignore[union-attr]
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
        return _format_rest_apis(mcp_plugin_config.list_rest_apis())


# ═══════════════════════════════════════════════════════════════════════
# rest_api_set_enabled
# ═══════════════════════════════════════════════════════════════════════


class RestApiSetEnabledTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "rest_api_set_enabled"
    category = "REST API"
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

        if name not in mcp_plugin_config.list_rest_apis():
            return f"'{name}' not found in rest_apis."
        mcp_plugin_config.set_server_enabled(name, enabled)

        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                await mcp.call_tool("mcp_set_enabled", {"name": name, "enabled": enabled})  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("rest_api_set_mcp_failed name=%s err=%s", name, e)

        state = "enabled" if enabled else "disabled"
        logger.info("rest_api_set_enabled name=%s enabled=%s", name, enabled)
        return f"[OK] REST API '{name}' {state}."