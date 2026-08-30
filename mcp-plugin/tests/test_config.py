"""Tests for ``mcp_plugin.config`` — server-entry persistence and loading.

MCP config moved out of slife into this package (slife.config lost
``MCPConfig`` / ``Config.mcp_config`` / ``save_mcp_server`` etc. during the
mcp-plugin extraction).  These cover the module-level persistence API —
:func:`add_server_entry`, :func:`remove_server_entry`,
:func:`set_server_enabled` — plus robustness to malformed config.  The
``_isolate_config_path`` autouse fixture (conftest.py) points every read/write
at a throwaway ``$MCP_PLUGIN_FILE``, and ``read_config`` treats a missing file
as first run (empty dict), so tests are self-contained.
"""

from __future__ import annotations

import json5
from pathlib import Path

import mcp_plugin
import mcp_plugin.config as cfg


def _raw_config() -> dict:
    """Re-read the isolated config file as parsed JSON5."""
    return json5.loads(cfg.current_path().read_text(encoding="utf-8"))


def test_import_config():
    """The config module imports and exposes the version."""
    assert mcp_plugin.__version__


class TestAddServerEntry:
    """add_server_entry — upsert persistence with merge semantics."""

    def test_persists_command_args_env(self):
        cfg.add_server_entry("fs", {
            "command": "npx",
            "args": ["-y", "server-filesystem"],
            "env": {"NODE_ENV": "production"},
        })
        servers = _raw_config()["servers"]
        assert servers["fs"]["command"] == "npx"
        assert servers["fs"]["args"] == ["-y", "server-filesystem"]
        assert servers["fs"]["env"] == {"NODE_ENV": "production"}
        # In-memory view matches the file.
        assert cfg.servers()["fs"] == servers["fs"]

    def test_without_env_leaves_no_env_key(self):
        cfg.add_server_entry("test_srv", {"command": "echo", "args": ["hello"]})
        srv = _raw_config()["servers"]["test_srv"]
        assert "env" not in srv
        assert cfg.get_server("test_srv") == srv

    def test_with_source_stored_verbatim(self):
        """The generic upsert stores source as given — no implicit stamping."""
        cfg.add_server_entry("gh", {
            "command": "npx",
            "args": ["-y", "anyapi-mcp-server", "--spec", "https://example.com/api.yaml"],
            "source": {
                "url": "https://github.com/quiloos39/anyapi-mcp-server",
                "type": "mcp_package",
                "version": "1.2.0",
            },
        })
        source = _raw_config()["servers"]["gh"]["source"]
        assert source["url"] == "https://github.com/quiloos39/anyapi-mcp-server"
        assert source["type"] == "mcp_package"
        assert source["version"] == "1.2.0"
        assert "fetched_at" not in source  # fetched_at is a rest-api concern

    def test_without_source_writes_no_source_key(self):
        cfg.add_server_entry("srv", {"command": "echo", "args": ["hello"]})
        assert "source" not in _raw_config()["servers"]["srv"]

    def test_with_url_and_headers(self):
        cfg.add_server_entry("web", {
            "command": "node",
            "args": ["server.js"],
            "url": "http://localhost:3000",
            "headers": {"Authorization": "Bearer token"},
            "description": "A web server",
        })
        srv = _raw_config()["servers"]["web"]
        assert srv["url"] == "http://localhost:3000"
        assert srv["headers"] == {"Authorization": "Bearer token"}
        assert srv["description"] == "A web server"

    def test_upsert_preserves_unspecified_existing_fields(self):
        cfg.add_server_entry("srv", {"command": "echo", "args": ["a"]})
        cfg.add_server_entry("srv", {"args": ["b"]})
        srv = _raw_config()["servers"]["srv"]
        assert srv["command"] == "echo"  # preserved
        assert srv["args"] == ["b"]       # updated

    def test_none_values_skipped(self):
        cfg.add_server_entry("srv", {"command": "echo", "args": [], "env": None})
        srv = _raw_config()["servers"]["srv"]
        assert srv["command"] == "echo"
        assert "env" not in srv

    def test_enabled_true_clears_stale_false(self):
        cfg.add_server_entry("srv", {"command": "echo", "enabled": False})
        cfg.add_server_entry("srv", {"command": "echo", "enabled": True})
        assert "enabled" not in _raw_config()["servers"]["srv"]


class TestRemoveServerEntry:
    """remove_server_entry — delete one server, keep the rest."""

    def test_removes_only_named_server(self):
        cfg.add_server_entry("to_remove", {"command": "echo", "args": ["bye"]})
        cfg.add_server_entry("to_keep", {"command": "echo", "args": ["hi"]})

        assert cfg.remove_server_entry("to_remove") is True

        servers = _raw_config()["servers"]
        assert "to_remove" not in servers
        assert "to_keep" in servers
        assert "to_remove" not in cfg.servers()

    def test_absent_name_returns_false(self):
        cfg.add_server_entry("keep", {"command": "echo"})
        assert cfg.remove_server_entry("nope") is False
        assert _raw_config()["servers"]["keep"]["command"] == "echo"


class TestSetServerEnabled:
    """set_server_enabled — persist the enabled flag (True removes it)."""

    def test_disabled_writes_enabled_false(self):
        cfg.add_server_entry("mysrv", {"command": "echo", "args": []})
        assert cfg.set_server_enabled("mysrv", False) is True
        srv = _raw_config()["servers"]["mysrv"]
        assert srv["enabled"] is False
        assert cfg.servers()["mysrv"]["enabled"] is False

    def test_enabled_true_removes_key(self):
        cfg.add_server_entry("mysrv", {"command": "echo", "enabled": False})
        assert cfg.set_server_enabled("mysrv", True) is True
        assert "enabled" not in _raw_config()["servers"]["mysrv"]
        assert "enabled" not in cfg.servers()["mysrv"]

    def test_absent_name_returns_false(self):
        assert cfg.set_server_enabled("nope", False) is False


class TestSetEmbeddings:
    """set_embeddings — top-level embeddings section upsert (merge semantics)."""

    def test_creates_section_when_absent(self):
        cfg.set_embeddings({"base_url": "http://127.0.0.1:17347/v1", "model": "bge-m3"})
        emb = _raw_config()["embeddings"]
        assert emb["base_url"] == "http://127.0.0.1:17347/v1"
        assert emb["model"] == "bge-m3"

    def test_preserves_unpassed_fields(self):
        cfg.set_embeddings({
            "base_url": "http://a/v1", "model": "m1", "api_key": "key-a",
        })
        cfg.set_embeddings({"base_url": "http://b/v1"})
        emb = _raw_config()["embeddings"]
        assert emb == {"base_url": "http://b/v1", "model": "m1", "api_key": "key-a"}

    def test_empty_string_overwrites_existing(self):
        cfg.set_embeddings({"base_url": "http://a/v1", "api_key": "key-a", "model": "m1"})
        cfg.set_embeddings({"api_key": ""})
        emb = _raw_config()["embeddings"]
        assert emb["api_key"] == ""   # explicit '' clears auth…
        assert emb["model"] == "m1"   # …while untouched fields survive

    def test_rest_api_api_key_header_refers_to_env_var(self):
        """A ${VAR} api_key is stored verbatim, not resolved at save time."""
        cfg.set_embeddings({"base_url": "http://a/v1", "api_key": "${EMBED_KEY}"})
        emb = _raw_config()["embeddings"]
        assert emb["api_key"] == "${EMBED_KEY}"


class TestRestAPI:
    """save_rest_api — npx anyapi entry with a fetched_at-stamped source."""

    def test_save_rest_api_stamps_fetched_at(self):
        cfg.save_rest_api("gh", spec_url="https://example.com/api.yaml")
        entry = _raw_config()["servers"]["gh"]
        assert entry["command"] == "npx"
        assert "anyapi-mcp-server" in entry["args"]
        source = entry["source"]
        assert source["type"] == "rest_api"
        assert "fetched_at" in source

    def test_save_rest_api_with_api_key_header(self):
        cfg.save_rest_api("e", spec_url="https://x.example/swagger.json", api_key="KEY")
        entry = _raw_config()["servers"]["e"]
        assert "--header" in entry["args"]
        assert "Authorization: Bearer ${KEY}" in entry["args"]


class TestIsSlifeDev:
    """CWD-based slife detection — mirrors credstore's ``is_slife_dev``."""

    def test_slife_project_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "slife"\n', encoding="utf-8"
        )
        assert cfg.is_slife_dev() is True

    def test_other_project_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "other"\n', encoding="utf-8"
        )
        assert cfg.is_slife_dev() is False

    def test_missing_pyproject_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cfg.is_slife_dev() is False

    def test_malformed_pyproject_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[project\nname = 'slife'", encoding="utf-8"
        )
        assert cfg.is_slife_dev() is False


class TestResolveConfigPath:
    """resolve_config_path — env > dev default > home default (credstore-style)."""

    def test_dev_default_is_cwd_relative(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_PLUGIN_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "slife"\n', encoding="utf-8"
        )
        # Dev default is CWD-relative, exactly like credstore's
        # ``./credentials.crypt``.
        assert cfg.resolve_config_path() == Path("mcp-plugin.json5")

    def test_production_default_uses_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_PLUGIN_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        assert cfg.resolve_config_path() == (
            Path.home() / ".mcp-plugin" / "mcp-plugin.json5"
        )

    def test_env_wins_over_dev(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_PLUGIN_FILE", str(tmp_path / "mcp-plugin.json5"))
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "slife"\n', encoding="utf-8"
        )
        assert cfg.resolve_config_path() == tmp_path / "mcp-plugin.json5"


class TestServersReading:
    """servers() robustness — no config file / malformed section."""

    def test_no_config_returns_empty(self):
        assert cfg.servers() == {}
        assert cfg.count_servers() == 0

    def test_non_dict_servers_returns_empty(self):
        # Write a config whose servers section is malformed (a list).
        path = cfg.current_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"servers": ["a", "b"]}', encoding="utf-8")
        assert cfg.servers() == {}
        assert cfg.count_servers() == 0