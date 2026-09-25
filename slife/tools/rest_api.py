"""REST API management — register external APIs backed by mcp-openapi-proxy.

rest_api_set / rest_api_remove / rest_api_list / rest_api_list_tools /
rest_api_set_enabled.

Server definitions live in ``tools.yaml`` (owned by the mcp gateway,
resolved via ``$TOOLS_FILE``); a REST API is an entry in the ``rest-api``
section — the section is what makes one, and no entry needs to say so itself.
It is registered and reached through an ``mcp-openapi-proxy`` process, which
is how this module executes one, not what one is.  This module is the
sLife-side face: it re-points persistence to :mod:`slife.plugins.mcp_gateway.config`
and keeps a live ``mcp_set``-style warm-up through the mcp plugin so an API
connects immediately.
"""

import json
import logging
from urllib.parse import urlparse

from slife.plugins.mcp_gateway import config as mcp_gateway_config

from slife.tools._config_io import _ConfigPathMixin, format_source_info
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _validate_http_url(url: str, what: str) -> str:
    """Require *url* to be an ``http(s)`` URL with a host.

    ``spec_url`` / ``base_url`` are handed to ``mcp-openapi-proxy`` via its
    env vars, and the child fetches the spec — an LLM-supplied ``file://``
    or internal-host URL would otherwise be an SSRF vector.  (Private IPs
    are intentionally allowed: local APIs are a legitimate use.)
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
        parsed = mcp_gateway_config.parse_rest_api_entry(cfg)
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
        # Validate before persisting or spawning mcp-openapi-proxy — an
        # LLM-supplied file:// or internal URL would be fetched by the proxy
        # child.
        spec_url: str = _validate_http_url(kwargs["spec_url"], "spec_url")
        base_url: str = _validate_http_url(kwargs["base_url"], "base_url")
        api_key: str = kwargs.get("api_key", "")
        description: str = kwargs.get("description", "")

        is_update = name in mcp_gateway_config.list_rest_apis()
        mcp_gateway_config.save_rest_api(
            name=name, spec_url=spec_url, base_url=base_url,
            api_key=api_key, description=description,
        )
        logger.info("rest_api_saved name=%s spec=%s", name, spec_url)

        entry = mcp_gateway_config.build_rest_api_entry(
            spec_url, base_url, api_key, description,
        )

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
                    "__mcp_set",
                    {
                        "name": name,
                        "command": entry["command"],
                        "args": entry["args"],
                        "env": entry["env"],
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

        if not mcp_gateway_config.remove_rest_api(name):
            return f"REST API '{name}' is not registered."
        logger.info("rest_api_removed name=%s", name)

        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                await mcp.call_tool("__mcp_remove", {"name": name})  # type: ignore[union-attr]
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
        return _format_rest_apis(mcp_gateway_config.list_rest_apis())


# ═══════════════════════════════════════════════════════════════════════
# rest_api_list_tools
# ═══════════════════════════════════════════════════════════════════════


def _format_operations(name: str, data: dict) -> str:
    """Render one API's operations as a context-cheap text list.

    The cap is NOT applied here: the gateway owns it (``__mcp_list_tools``'s
    ``limit``), so this only reports what it was given.  A second cap in this
    file would be two places deciding how much context a listing may spend.
    """
    if not data.get("connected", False):
        detail = data.get("note") or data.get("error") or "no tool list"
        return f"[ERROR] REST API '{name}' is not serving a spec — {detail}"

    tools = data.get("tools") or []
    if not tools:
        return f"REST API '{name}' exposes no operations."

    total = data.get("tool_count", len(tools))
    lines = [f"REST API '{name}' — {total} operations:"]
    for tool in tools:
        desc = (tool.get("description") or "").strip().splitlines()
        summary = desc[0] if desc else ""
        lines.append(f"- {tool.get('name', '?')}" + (f" — {summary}" if summary else ""))
    if len(tools) < total:
        lines.append(
            f"\n{total - len(tools)} more operations not listed — "
            "use tool_search to find the one you need."
        )
    return "\n".join(lines)


class RestApiListToolsTool(_ConfigPathMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    """List the operations one REST API exposes — its OpenAPI-derived tools.

    The REST-API-shaped twin of ``mcp_list_tools``: the same live read through
    the gateway, but answering in this family's vocabulary so the model never
    has to know which transport an API rides (DESIGNER NOTES §8.5).  The
    gateway holds it as an ``mcp-openapi-proxy`` process because that is what
    serves an OpenAPI document today — the shared read path follows from the
    implementation, not from what a REST API is.
    """

    name = "rest_api_list_tools"
    category = "REST API"
    description = (
        "List the operations a REST API exposes (from its OpenAPI spec). "
        f"Capped at mcp.tool_list_limit ({mcp_gateway_config.DEFAULT_TOOL_LIST_LIMIT}) "
        "— use tool_search to find a specific one."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "API name, from rest_api_list."},
            "limit": {
                "type": "integer",
                "description": (
                    "Max operations to list (0 = the configured cap, "
                    "mcp.tool_list_limit)."
                ),
            },
        },
        "required": ["name"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        limit: int = kwargs.get("limit") or 0

        if name not in mcp_gateway_config.list_rest_apis():
            return f"'{name}' not found in rest_apis. Use rest_api_list."

        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        if mcp is None:
            return f"[ERROR] Cannot read '{name}'s operations."
        # The gateway applies the cap, so this tool never slices a list: one
        # implementation decides how much context a listing may spend, and it
        # is the same one mcp_list_tools uses.
        cap = mcp_gateway_config.tool_list_limit() if limit <= 0 else limit
        try:
            raw = await mcp.call_tool(  # type: ignore[union-attr]
                "__mcp_list_tools", {"server": name, "limit": cap},
            )
        except Exception as e:
            logger.warning("rest_api_list_tools_failed name=%s err=%s", name, e)
            return f"[ERROR] Could not list '{name}'s operations: {e}"

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return f"[ERROR] Unexpected listing for '{name}': {raw}"
        if not isinstance(data, dict):
            return f"[ERROR] Unexpected listing for '{name}'."

        return _format_operations(name, data)


# ═══════════════════════════════════════════════════════════════════════
# rest_api_set_enabled
# ═══════════════════════════════════════════════════════════════════════


class RestApiSetEnabledTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Enable or disable a REST API — the only lifecycle knob.

    A REST API is a distinct management family from MCP (DESIGNER NOTES §8.5):
    today it rides the mcp-openapi-proxy gateway, but the LLM-facing surface
    stays REST-API-shaped so a proxy-free transport later needs no tool
    change.  There is no connect/disconnect pair here: the modern MCP protocol
    removed the session a "connect" established, so a server is either enabled
    (live, reconnected lazily on use) or disabled (torn down).  The same verb
    re-arms one left in ERROR — the enable path forces a fresh connect.
    """

    name = "rest_api_set_enabled"
    category = "REST API"
    description = "Enable or disable a REST API (enable connects it, disable disconnects it)."
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

        if name not in mcp_gateway_config.list_rest_apis():
            return f"'{name}' not found in rest_apis."
        mcp_gateway_config.set_server_enabled(name, enabled)

        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        if mcp is not None:
            try:
                await mcp.call_tool("__mcp_set_enabled", {"name": name, "enabled": enabled})  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("rest_api_set_mcp_failed name=%s err=%s", name, e)

        state = "enabled" if enabled else "disabled"
        logger.info("rest_api_set_enabled name=%s enabled=%s", name, enabled)
        return f"[OK] REST API '{name}' {state}."