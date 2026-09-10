"""sharefile.json5 — path resolution, provider selection, and degradation.

A tunnel provider is a *subordinate* dependency: no config state may keep the
plugin from loading, so every failure mode below must degrade to the default
provider instead of raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slife.plugins.sharefile import config as cfgmod
from slife.plugins.sharefile.providers import KNOWN_PROVIDERS

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sharefile.json5"
    p.write_text(text, encoding="utf-8")
    return p


class TestResolveConfigPath:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere.json5"
        monkeypatch.setenv("SHAREFILE_FILE", str(target))
        assert cfgmod.resolve_config_path() == target

    def test_default_follows_the_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHAREFILE_FILE", raising=False)
        monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))
        assert cfgmod.resolve_config_path() == tmp_path / "sharefile.json5"


class TestLoadConfig:
    def test_missing_file_falls_back_to_ngrok(self, tmp_path):
        cfg = cfgmod.load_sharefile_config(tmp_path / "absent.json5")
        assert cfg.active_provider == "ngrok"
        assert cfg.providers == {}

    def test_reads_active_provider_and_options(self, tmp_path):
        p = _write(tmp_path, """
        {
          active_provider: "localhost.run",
          providers: {
            ngrok: {},
            "localhost.run": { user: "nokey", remote_port: 80 },
          },
        }
        """)
        cfg = cfgmod.load_sharefile_config(p)
        assert cfg.active_provider == "localhost.run"
        assert cfg.options_for("localhost.run") == {"user": "nokey", "remote_port": 80}

    def test_options_for_unknown_or_non_dict_provider_is_empty(self, tmp_path):
        cfg = cfgmod.load_sharefile_config(
            _write(tmp_path, '{ providers: { ngrok: {} } }')
        )
        # ngrok is configured but carries no options; "absent" is not listed.
        assert cfg.options_for("ngrok") == {}
        assert cfg.options_for("absent") == {}

    def test_unknown_active_provider_falls_back(self, tmp_path):
        cfg = cfgmod.load_sharefile_config(
            _write(tmp_path, '{ active_provider: "made-up", providers: {} }')
        )
        assert cfg.active_provider == "ngrok"

    def test_broken_config_does_not_raise(self, tmp_path):
        cfg = cfgmod.load_sharefile_config(_write(tmp_path, "{ this is not json5"))
        assert cfg.active_provider == "ngrok"
        assert cfg.providers == {}

    def test_provider_resolves_env_references(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHAREFILE_TEST_SSH", "/opt/openssh/ssh")
        cfg = cfgmod.load_sharefile_config(_write(tmp_path, """
        {
          active_provider: "localhost.run",
          providers: {
            "localhost.run": { ssh: "${SHAREFILE_TEST_SSH}", remote_port: "${UNSET_PORT:-8022}" },
          },
        }
        """))
        assert cfg.options_for("localhost.run") == {
            "ssh": "/opt/openssh/ssh", "remote_port": "8022",
        }

    def test_provider_with_unresolved_env_is_dropped_alone(self, tmp_path):
        cfg = cfgmod.load_sharefile_config(_write(tmp_path, """
        {
          active_provider: "localhost.run",
          providers: {
            "localhost.run": { ssh: "${SHAREFILE_DEFINITELY_UNSET_VAR}" },
            ngrok: {},
          },
        }
        """))
        # The broken provider is dropped with a warning; its sibling survives.
        assert "localhost.run" not in cfg.providers
        assert "ngrok" in cfg.providers

    def test_non_dict_provider_entry_is_skipped(self, tmp_path):
        cfg = cfgmod.load_sharefile_config(
            _write(tmp_path, '{ providers: { ngrok: "oops", "localhost.run": {} } }')
        )
        assert "ngrok" not in cfg.providers
        assert "localhost.run" in cfg.providers


class TestShippedConfig:
    """The git-tracked sharefile.json5 is what every install seeds."""

    def test_parses_and_names_a_known_provider(self):
        root = Path(__file__).resolve().parents[1]
        cfg = cfgmod.load_sharefile_config(root / "sharefile.json5")
        assert cfg.active_provider in KNOWN_PROVIDERS
        # Which provider ships as active is a product decision — the test pins
        # only that it resolves, and that every provider is documented in the
        # shipped file so the alternatives are discoverable.
        for name in KNOWN_PROVIDERS:
            assert name in cfg.providers, f"{name} missing from sharefile.json5"
