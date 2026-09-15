"""The meta whitelist is a design contract — lock the exact tool sets.

If this test needs editing, the whitelist changed deliberately (DESIGNER
NOTES §8.5, minus the retired connect/disconnect pairs): the 6 tool-system
meta tools + the 3 harness auto-invoke pairs, and nothing else.
"""

from slife.tools.whitelist import (
    ALWAYS_LOADED,
    HARNESS_WHITELIST,
    META_WHITELIST,
    is_meta_tool,
)


def test_meta_whitelist_is_exactly_the_6_tools():
    assert META_WHITELIST == frozenset({
        "mcp_set_enabled",
        "rest_api_set_enabled",
        "tool_search",
        "tool_load",
        "skill_load",
        "_unload_function_tool",
    })


def test_harness_whitelist_is_the_loop_pairs():
    assert HARNESS_WHITELIST == frozenset({
        "_turn_prompt",
        "_check_new_input",
        "attach_image",
    })


def test_disjoint_and_covered():
    assert not (META_WHITELIST & HARNESS_WHITELIST)
    assert ALWAYS_LOADED == META_WHITELIST | HARNESS_WHITELIST
    assert len(ALWAYS_LOADED) == 9


def test_is_meta_tool_covers_both_classes():
    assert is_meta_tool("tool_load")
    assert is_meta_tool("mcp_set_enabled")
    assert is_meta_tool("rest_api_set_enabled")
    assert is_meta_tool("_turn_prompt")
    # NOT meta — evictable/diagnostic/config-management tools.
    assert not is_meta_tool("system_health")
    assert not is_meta_tool("cli_set")
    assert not is_meta_tool("mcp_set")
    assert not is_meta_tool("execute_shell")