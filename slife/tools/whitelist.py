"""Tool whitelist — the "always loaded, never touched" carve-outs.

This is the SYSTEM-level autoload layer: `tools.yaml` marks a tool or server
``autoload: true`` when the user wants it injected from session start, and the
names here are injected the same way without being configurable at all — no
entry can turn them off, and nothing can evict them.  Both sources seed a row
`loaded` and are carved out of LRU eviction; this one exists so the harness
keeps its own tools whatever the config says.

Three classes, all always injected into the LLM tool list, none LRU-evictable,
none unloadable via ``_func_tool_unload``:

- :data:`HARNESS_WHITELIST` — the loop's own auto-invoked tool pairs
  (``_turn_prompt`` / ``_check_new_input`` / ``attach_image``).  These are
  the mechanism, not user-facing meta tools; they are protected so the
  threshold squeeze can never take away the class the loop drives every turn.
- :data:`META_WHITELIST` — the 5 tool-system meta tools (DESIGNER_NOTES
  §8.5 "系统元工具"): server management as TWO separate families — ``mcp_*``
  and ``rest_api_*`` (a rest-api is semantically distinct today even though
  it rides the mcp-openapi-proxy gateway, and may drop it later) — plus the
  search/load surface and the ``_func_tool_unload`` unloader.
- :data:`PINNED_WHITELIST` — tools outside the tool system that are pinned
  always-loaded because the agent's own workflow keeps needing them.

Everything else — the other diagnostics (``system_tools_list``, async
poll…), config management (``cli_*``, ``skill_set``…) — is NOT whitelisted:
it seeds loaded and the agent can reload it via ``func_tool_load``, but the
LRU squeeze may evict it.  The whitelist is a design constant, not
configurable.
"""

from __future__ import annotations

#: Harness auto-invoked tool pairs — always injected (the loop's mechanism).
HARNESS_WHITELIST: frozenset[str] = frozenset({
    "_turn_prompt",
    "_check_new_input",
    "attach_image",
})

#: The tool-system meta surface — DESIGNER_NOTES §8.5, minus the retired
#: connect/disconnect pairs and `mcp_search`: server management (mcp AND
#: rest-api as separate families) is one on/off switch per family, plus
#: search/load and the unloader.  A "connect" had nothing left to
#: establish once the modern protocol removed the session, and a server-level
#: search lost its subject once the server registry went away — see
#: docs/TOOL-SYSTEM.md.
META_WHITELIST: frozenset[str] = frozenset({
    "mcp_set_enabled",
    "rest_api_set_enabled",
    "tool_search",
    "func_tool_load",
    "_func_tool_unload",
})

#: Pinned always-loaded — not tool-system machinery, but one call each for two
#: things every session does: ``skill_use`` reads the playbook a skill exists
#: to carry (the read half of the Skills family — discovery is ``skill_list``),
#: and ``system_health`` is the single health report over every subsystem
#: check.  Losing either to the threshold squeeze costs a search+load round
#: trip to re-learn what the session always needs.
PINNED_WHITELIST: frozenset[str] = frozenset({
    "skill_use",
    "system_health",
})

#: Everything that must always be injected / never evicted / not unloadable.
ALWAYS_LOADED: frozenset[str] = (
    HARNESS_WHITELIST | META_WHITELIST | PINNED_WHITELIST
)


def is_meta_tool(name: str) -> bool:
    """True if *name* is protected — harness pair, meta surface, or pinned.

    The predicate every load-state gate shares (injection, eviction, unload),
    so "is it whitelisted" is asked in exactly one place.  Execution is not one
    of them: a call is gated on having an execution instance, never on load
    state, so nothing here needs to exempt a whitelisted tool from a call.
    """
    return name in ALWAYS_LOADED


#: New tool-system tools share one category label so ``system_tools_list``
#: groups them coherently (distinct from System / Models / Skills).
TOOL_META_CATEGORY = "ToolSystem"