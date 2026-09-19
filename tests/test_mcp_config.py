"""Tests for ``slife.plugins.mcp_gateway.config`` — server-entry persistence and loading.

These cover the module-level persistence API — :func:`add_server_entry`,
:func:`remove_server_entry`, :func:`set_server_enabled` — plus robustness to
malformed config.  The ``_isolate_config_path`` autouse fixture (conftest.py)
points every read/write at a throwaway ``$TOOLS_FILE``, and ``read_config``
treats a missing file as first run (empty dict), so tests are self-contained.
"""

from __future__ import annotations

from pathlib import Path

import slife.plugins.mcp_gateway
import slife.plugins.mcp_gateway.config as cfg
from tests.conftest import load_config_text


def _raw_config() -> dict:
    """Re-read the isolated config file as parsed YAML."""
    return load_config_text(cfg.current_path().read_text(encoding="utf-8"))


def test_import_config():
    """The config module imports and exposes the version."""
    assert slife.plugins.mcp_gateway.__version__


class TestAddServerEntry:
    """add_server_entry — upsert persistence with merge semantics."""

    def test_persists_command_args_env(self):
        cfg.add_server_entry("fs", {
            "command": "npx",
            "args": ["-y", "server-filesystem"],
            "env": {"NODE_ENV": "production"},
        })
        servers = _raw_config()["mcp"]["servers"]
        assert servers["fs"]["command"] == "npx"
        assert servers["fs"]["args"] == ["-y", "server-filesystem"]
        assert servers["fs"]["env"] == {"NODE_ENV": "production"}
        # In-memory view matches the file.
        assert cfg.servers()["fs"] == servers["fs"]

    def test_without_env_leaves_no_env_key(self):
        cfg.add_server_entry("test_srv", {"command": "echo", "args": ["hello"]})
        srv = _raw_config()["mcp"]["servers"]["test_srv"]
        assert "env" not in srv
        assert cfg.get_server("test_srv") == srv

    def test_with_source_stored_verbatim(self):
        """The generic upsert stores source as given — no implicit stamping."""
        cfg.add_server_entry("gh", {
            "command": "uvx",
            "args": ["mcp-openapi-proxy"],
            "source": {
                "url": "https://example.com/api.yaml",
                "type": "mcp_package",
                "version": "0.4.0",
            },
        })
        source = _raw_config()["mcp"]["servers"]["gh"]["source"]
        assert source["url"] == "https://example.com/api.yaml"
        assert source["type"] == "mcp_package"
        assert source["version"] == "0.4.0"
        assert "fetched_at" not in source  # fetched_at is a rest-api concern

    def test_without_source_writes_no_source_key(self):
        cfg.add_server_entry("srv", {"command": "echo", "args": ["hello"]})
        assert "source" not in _raw_config()["mcp"]["servers"]["srv"]

    def test_with_url_and_headers(self):
        cfg.add_server_entry("web", {
            "command": "node",
            "args": ["server.js"],
            "url": "http://localhost:3000",
            "headers": {"Authorization": "Bearer token"},
            "description": "A web server",
        })
        srv = _raw_config()["mcp"]["servers"]["web"]
        assert srv["url"] == "http://localhost:3000"
        assert srv["headers"] == {"Authorization": "Bearer token"}
        assert srv["description"] == "A web server"

    def test_upsert_preserves_unspecified_existing_fields(self):
        cfg.add_server_entry("srv", {"command": "echo", "args": ["a"]})
        cfg.add_server_entry("srv", {"args": ["b"]})
        srv = _raw_config()["mcp"]["servers"]["srv"]
        assert srv["command"] == "echo"  # preserved
        assert srv["args"] == ["b"]       # updated

    def test_none_values_skipped(self):
        cfg.add_server_entry("srv", {"command": "echo", "args": [], "env": None})
        srv = _raw_config()["mcp"]["servers"]["srv"]
        assert srv["command"] == "echo"
        assert "env" not in srv

    def test_enabled_true_clears_stale_false(self):
        cfg.add_server_entry("srv", {"command": "echo", "enabled": False})
        cfg.add_server_entry("srv", {"command": "echo", "enabled": True})
        assert "enabled" not in _raw_config()["mcp"]["servers"]["srv"]


class TestRemoveServerEntry:
    """remove_server_entry — delete one server, keep the rest."""

    def test_removes_only_named_server(self):
        cfg.add_server_entry("to_remove", {"command": "echo", "args": ["bye"]})
        cfg.add_server_entry("to_keep", {"command": "echo", "args": ["hi"]})

        assert cfg.remove_server_entry("to_remove") is True

        servers = _raw_config()["mcp"]["servers"]
        assert "to_remove" not in servers
        assert "to_keep" in servers
        assert "to_remove" not in cfg.servers()

    def test_absent_name_returns_false(self):
        cfg.add_server_entry("keep", {"command": "echo"})
        assert cfg.remove_server_entry("nope") is False
        assert _raw_config()["mcp"]["servers"]["keep"]["command"] == "echo"


class TestSetServerEnabled:
    """set_server_enabled — persist the enabled flag (True removes it)."""

    def test_disabled_writes_enabled_false(self):
        cfg.add_server_entry("mysrv", {"command": "echo", "args": []})
        assert cfg.set_server_enabled("mysrv", False) is True
        srv = _raw_config()["mcp"]["servers"]["mysrv"]
        assert srv["enabled"] is False
        assert cfg.servers()["mysrv"]["enabled"] is False

    def test_enabled_true_removes_key(self):
        cfg.add_server_entry("mysrv", {"command": "echo", "enabled": False})
        assert cfg.set_server_enabled("mysrv", True) is True
        assert "enabled" not in _raw_config()["mcp"]["servers"]["mysrv"]
        assert "enabled" not in cfg.servers()["mysrv"]

    def test_absent_name_returns_false(self):
        assert cfg.set_server_enabled("nope", False) is False


class TestLegacyServersMigration:
    """Pre-section tools.yaml (top-level ``servers``) reads and migrates.

    The rename lift created tools.yaml files with servers at the top level;
    they keep working, and the first write normalizes them into the sections.
    """

    def _legacy_file(self) -> None:
        path = cfg.current_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "servers:\n  old:\n    command: echo\n", encoding="utf-8"
        )

    def test_legacy_servers_read(self):
        self._legacy_file()
        assert cfg.servers() == {"old": {"command": "echo"}}
        assert cfg.count_servers() == 1

    def test_legacy_servers_normalized_on_write(self):
        self._legacy_file()
        cfg.add_server_entry("new", {"command": "npx"})
        raw = _raw_config()
        assert raw["mcp"]["servers"] == {
            "old": {"command": "echo"},
            "new": {"command": "npx"},
        }
        # The legacy top-level key is gone after the first write.
        assert "servers" not in raw
        assert cfg.servers()["old"]["command"] == "echo"
        assert cfg.servers()["new"]["command"] == "npx"

    def test_legacy_servers_remove_creates_section(self):
        self._legacy_file()
        assert cfg.remove_server_entry("old") is True
        raw = _raw_config()
        assert raw["mcp"]["servers"] == {}
        assert cfg.count_servers() == 0


class TestRestAPI:
    """save_rest_api — uvx mcp-openapi-proxy entry in the ``rest-api`` section.
    Spec/base/key ride the proxy's env vars (Low-Level Mode default).

    The section is the whole fact: no ``source.type`` tag on the entry and no
    ``fetched_at`` (nothing is fetched here — the proxy reads the spec itself,
    at every start)."""

    def test_save_rest_api_writes_the_section_and_nothing_redundant(self):
        cfg.save_rest_api("gh", spec_url="https://example.com/api.yaml")
        entry = _raw_config()["rest-api"]["gh"]
        assert entry["command"] == "uvx"
        assert entry["args"] == ["mcp-openapi-proxy"]
        assert entry["env"]["OPENAPI_SPEC_URL"] == "https://example.com/api.yaml"
        assert "API_KEY" not in entry["env"]  # public API — no auth env
        assert "source" not in entry          # the section says it
        assert "url" not in entry             # stdio — no url to write, empty or not

    def test_a_sectioned_entry_is_a_rest_api_without_any_tag(self):
        """Placement decides, and nothing is tagged for it: this entry matches
        NEITHER entry-level rule (no ``source`` tag, not an openapi-proxy
        shape), so only the section can make it a REST API — and its twin
        under ``mcp.servers`` must stay an MCP server."""
        cfg.add_server_entry(
            "handmade", {"command": "npx", "args": ["-y", "some-mcp"]},
            section="rest-api",
        )
        cfg.add_server_entry(
            "twinshape", {"command": "npx", "args": ["-y", "some-mcp"]},
            section="mcp",
        )
        assert "handmade" in cfg.list_rest_apis()
        assert "twinshape" not in cfg.list_rest_apis()
        assert cfg.is_rest_api("handmade") and not cfg.is_rest_api("twinshape")
        # …and nothing was written into either entry to carry the answer.
        raw = _raw_config()
        assert "source" not in raw["rest-api"]["handmade"]
        assert "source" not in raw["mcp"]["servers"]["twinshape"]

    def test_source_provenance_is_never_overwritten(self):
        """``source`` records where a definition came from (its ``type`` is the
        download source — github / registry / hand), so placement must not
        touch it: a REST API installed from the registry keeps its origin."""
        cfg.add_server_entry(
            "from_registry",
            {"command": "uvx", "args": ["mcp-openapi-proxy"],
             "env": {"OPENAPI_SPEC_URL": "https://x.example/openapi.json"},
             "source": {"type": "github", "url": "https://github.com/x/y",
                        "version": "latest"}},
            section="rest-api",
        )
        entry = _raw_config()["rest-api"]["from_registry"]
        assert entry["source"] == {"type": "github", "url": "https://github.com/x/y",
                                   "version": "latest"}
        # The merged view is untouched too — the category comes from placement.
        assert cfg._servers_dict(_raw_config())["from_registry"]["source"]["type"] == "github"
        assert cfg.is_rest_api("from_registry")

    def test_save_rest_api_with_api_key_ref(self):
        cfg.save_rest_api("e", spec_url="https://x.example/swagger.json", api_key="KEY")
        entry = _raw_config()["rest-api"]["e"]
        assert entry["env"]["API_KEY"] == "${KEY}"


class TestResolveConfigPath:
    """resolve_config_path — $TOOLS_FILE > slife data-dir default.

    The mcp gateway is a built-in slife plugin: the default config path is
    ``<slife data dir>/tools.yaml`` (``slife.paths.get_data_dir``), not a
    ``~/.mcp-gateway/`` standalone location.
    """

    def test_data_dir_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOOLS_FILE", raising=False)
        monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path / "data"))
        assert cfg.resolve_config_path() == (
            tmp_path / "data" / "tools.yaml"
        )

    def test_production_default_under_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOOLS_FILE", raising=False)
        monkeypatch.delenv("SLIFE_DATA_DIR", raising=False)
        monkeypatch.chdir(tmp_path)  # not the slife checkout → ~/.slife
        assert cfg.resolve_config_path() == (
            Path.home() / ".slife" / "tools.yaml"
        )

    def test_env_wins_over_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOOLS_FILE", str(tmp_path / "tools.yaml"))
        monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path / "other"))
        assert cfg.resolve_config_path() == tmp_path / "tools.yaml"


class TestServersReading:
    """servers() robustness — no config file / malformed section."""

    def test_no_config_returns_empty(self):
        assert cfg.servers() == {}
        assert cfg.count_servers() == 0

    def test_non_dict_servers_returns_empty(self):
        # Write a config whose servers section is malformed (a list).
        path = cfg.current_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("servers: [a, b]\n", encoding="utf-8")
        assert cfg.servers() == {}
        assert cfg.count_servers() == 0