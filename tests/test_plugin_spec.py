"""Tests for slife.plugins.spec — the central plugin contract table."""

import pytest; pytestmark = pytest.mark.unit

import importlib.util

from slife.plugins.spec import (
    PLUGIN_SPECS,
    SPEC_ORDER,
    health_check_name,
    mcp_child_reserved_names,
    spec_for,
)

#: (public name, module, ctx field) — the full built-in contract.
_EXPECTED = [
    ("mcp-gateway", "slife.plugins.mcp_gateway.server", "mcp_client"),
    ("memdb", "slife.plugins.memdb.server", "memdb_client"),
    ("memfiles", "slife.plugins.memfiles.server", "memfiles_client"),
    ("wechat", "slife.plugins.wechat.server", "wechat_client"),
    ("sharefile", "slife.plugins.sharefile.server", "sharefile_client"),
    ("a2a", "slife.plugins.a2a.server", "a2a_mcp_client"),
    ("media", "slife.plugins.media.server", "media_client"),
    ("job-coding", "slife.plugins.job_coding.server", "job_coding_client"),
]


class TestPluginSpecs:
    def test_all_eight_builtins_present(self):
        assert list(PLUGIN_SPECS) == [n for n, _, _ in _EXPECTED]

    def test_deterministic_order(self):
        assert list(SPEC_ORDER) == [n for n, _, _ in _EXPECTED]

    def test_module_and_ctx_field(self):
        for name, module, ctx in _EXPECTED:
            spec = PLUGIN_SPECS[name]
            assert spec.name == name
            assert spec.module == module
            assert spec.ctx_field == ctx

    def test_modules_resolve(self):
        for name, module, _ in _EXPECTED:
            assert importlib.util.find_spec(module) is not None, (
                f"{name} spec module {module} not importable"
            )

    def test_only_mcp_is_gateway_and_host_params(self):
        for name, spec in PLUGIN_SPECS.items():
            assert spec.gateway == (name == "mcp-gateway")
            assert spec.host_params == (name == "mcp-gateway")

    def test_ctx_field_matches_tool_context(self):
        # The declared ctx fields must exist on ToolContext.
        from slife.tools.context import ToolContext
        fields = {f.name for f in __import__("dataclasses").fields(ToolContext)}
        for name, _, ctx in _EXPECTED:
            assert ctx in fields, f"ctx_field {ctx} missing on ToolContext"

    def test_wechat_a2a_gated(self):
        assert PLUGIN_SPECS["wechat"].enable_method == "_gate_wechat"
        assert PLUGIN_SPECS["a2a"].enable_method == "_gate_a2a"


class TestHealthCheckName:
    def test_underscore_names(self):
        assert health_check_name("memdb") == "check_memdb"
        assert health_check_name("sharefile") == "check_sharefile"

    def test_hyphen_name_normalised(self):
        assert health_check_name("job-coding") == "check_job_coding"


class TestReservedNames:
    def test_reserved_covers_every_builtin(self):
        names = mcp_child_reserved_names()
        assert names == set(PLUGIN_SPECS)
        assert "mcp-gateway" in names         # gateway name is reserved too
        assert "job-coding" in names          # regression: was hand-listed and missed
        assert "mcp" not in names             # the gateway is now mcp-gateway


class TestSpecFor:
    def test_known_returns_canonical(self):
        assert spec_for("memdb") is PLUGIN_SPECS["memdb"]

    def test_unknown_returns_generic_default(self):
        spec = spec_for("local-embed", "slife.plugins.local_embed.server")
        assert spec.name == "local-embed"
        assert spec.module == "slife.plugins.local_embed.server"
        assert spec.ctx_field is None
        assert spec.gateway is False
        assert spec.enable_method is None
        assert spec.health is True

    def test_unknown_without_module_guesses_path(self):
        spec = spec_for("my-plug", None)
        assert spec.module == "slife.plugins.my_plug.server"
