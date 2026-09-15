"""Meta-tool whitelist — the "always loaded, never touched" carve-outs.

Two distinct classes, both always injected into the LLM tool list, never
LRU-evictable, and not unloadable via ``_unload_function_tool``:

- :data:`HARNESS_WHITELIST` — the loop's own auto-invoked tool pairs
  (``_turn_prompt`` / ``_check_new_input`` / ``attach_image``).  These are
  the mechanism, not user-facing meta tools; they are protected so the
  threshold squeeze can never take away the class the loop drives every turn.
- :data:`META_WHITELIST` — the 11 tool-system meta tools (DESIGNER_NOTES
  §8.5 "系统元工具"): server management as TWO separate families — ``mcp_*``
  and ``rest_api_*`` (a rest-api is semantically distinct today even though
  it rides the mcp-openapi-proxy gateway, and may drop it later) — plus the
  search/load surface and the self-service ``_unload_function_tool``.

Everything else — diagnostics (``system_health``, ``list_native_tools``,
async poll…), config management (``cli_*``, ``skill_set``…) — is NOT
whitelisted: it seeds loaded and the agent can reload it via ``tool_load``,
but the LRU squeeze may evict it.  The whitelist is a design constant, not
configurable.
"""

from __future__ import annotations

#: Harness auto-invoked tool pairs — always injected (the loop's mechanism).
HARNESS_WHITELIST: frozenset[str] = frozenset({
    "_turn_prompt",
    "_check_new_input",
    "attach_image",
})

#: The tool-system meta surface — the 11 tools from DESIGNER_NOTES §8.5:
#: server management (mcp AND rest-api as separate families), search/load,
#: and the self-service unloader.
META_WHITELIST: frozenset[str] = frozenset({
    "mcp_search",
    "mcp_connect",
    "mcp_disconnect",
    "mcp_set_enabled",
    "rest_api_connect",
    "rest_api_disconnect",
    "rest_api_set_enabled",
    "tool_search",
    "tool_load",
    "skill_load",
    "_unload_function_tool",
})

#: Everything that must always be injected / never evicted / not unloadable.
ALWAYS_LOADED: frozenset[str] = HARNESS_WHITELIST | META_WHITELIST


def is_meta_tool(name: str) -> bool:
    """True if *name* is protected (harness pair or meta whitelist)."""
    return name in ALWAYS_LOADED


#: New tool-system tools share one category label so ``list_native_tools``
#: groups them coherently (distinct from System / Models / Skills).
TOOL_META_CATEGORY = "ToolSystem"