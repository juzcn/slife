"""The tool whitelist is a design contract — lock the exact tool sets.

If this test needs editing, the whitelist changed deliberately (DESIGNER
NOTES §8.5, minus the retired connect/disconnect pairs): the 5 tool-system
meta tools + the 3 harness auto-invoke pairs + the 2 pinned tools, and
nothing else.
"""

from slife.tools.whitelist import (
    ALWAYS_LOADED,
    HARNESS_WHITELIST,
    META_WHITELIST,
    PINNED_WHITELIST,
    is_meta_tool,
)


def test_meta_whitelist_is_exactly_the_5_tools():
    assert META_WHITELIST == frozenset({
        "mcp_set_enabled",
        "rest_api_set_enabled",
        "tool_search",
        "func-tool-load",
        "_unload_func_tool",
    })


def test_harness_whitelist_is_the_loop_pairs():
    assert HARNESS_WHITELIST == frozenset({
        "_turn_prompt",
        "_check_new_input",
        "attach_image",
    })


def test_pinned_whitelist_is_the_two_workflow_tools():
    assert PINNED_WHITELIST == frozenset({
        "skill_use",
        "system_health",
    })


def test_disjoint_and_covered():
    assert not (META_WHITELIST & HARNESS_WHITELIST)
    assert not (PINNED_WHITELIST & (HARNESS_WHITELIST | META_WHITELIST))
    assert ALWAYS_LOADED == HARNESS_WHITELIST | META_WHITELIST | PINNED_WHITELIST
    assert len(ALWAYS_LOADED) == 10


def test_is_meta_tool_covers_every_class():
    assert is_meta_tool("func-tool-load")
    assert is_meta_tool("mcp_set_enabled")
    assert is_meta_tool("rest_api_set_enabled")
    assert is_meta_tool("_turn_prompt")
    assert is_meta_tool("skill_use")
    assert is_meta_tool("system_health")
    # NOT whitelisted — evictable/config-management/other-diagnostic tools.
    assert not is_meta_tool("system_tools_list")
    assert not is_meta_tool("cli_set")
    assert not is_meta_tool("mcp_set")
    assert not is_meta_tool("execute_shell")