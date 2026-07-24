"""Tests for slife.tools.env — env var management tools."""

import os
from pathlib import Path
import pytest

from slife.tools.env import (
    _env_section,
    _PLACEHOLDER_PREFIX,
    ConfigEnvSetTool,
    ConfigEnvGetTool,
    ConfigEnvRemoveTool,
)


# ── helpers ──────────────────────────────────────────────────


def _mock_config(data: dict, monkeypatch):
    """Mock read_config and write_config for a test."""
    import slife.tools.env

    raw = dict(data)
    monkeypatch.setattr(slife.tools.env, "read_config", lambda path: raw)
    written = []
    monkeypatch.setattr(
        slife.tools.env, "write_config",
        lambda path, r: written.append(dict(r)),
    )
    return raw, written


def _mock_credstore(monkeypatch):
    """Mock credstore to an in-memory dict."""
    data = {}

    def _get(key):
        return data.get(key)

    def _delete(key):
        return data.pop(key, None) is not None

    # Patch the credstore package — config_env imports from it locally
    import credstore
    monkeypatch.setattr(credstore, "get_credential", _get)
    monkeypatch.setattr(credstore, "delete_credential", _delete)
    monkeypatch.setattr(credstore, "exists_credential", lambda k: k in data)

    # Also patch config's helper
    import slife.config
    monkeypatch.setattr(slife.config, "_try_credstore_lookup", _get)

    return data


# ── _env_section ─────────────────────────────────────────────


class TestEnvSection:
    def test_creates_env_section_if_missing(self):
        raw = {}
        env = _env_section(raw)
        assert env == {}
        assert "env" in raw

    def test_returns_existing_env(self):
        existing = {"A": "1"}
        raw = {"env": existing}
        env = _env_section(raw)
        assert env is existing

    def test_converts_non_dict_to_dict(self):
        raw = {"env": "not a dict"}
        env = _env_section(raw)
        assert env == {}
        assert raw["env"] == {}


# ── ConfigEnvSetTool ─────────────────────────────────────────


class TestConfigEnvSetTool:
    @pytest.mark.asyncio
    async def test_non_secret_key_writes_value(self, monkeypatch):
        """Non-secret keys still write directly."""
        raw, written = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="EDITOR", value="vim")

        assert raw["env"]["EDITOR"] == "vim"
        assert os.environ["EDITOR"] == "vim"
        assert "[OK]" in result

    @pytest.mark.asyncio
    async def test_non_secret_placeholder(self, monkeypatch):
        raw, written = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_SETTING")

        assert raw["env"]["MY_SETTING"] == "<YOUR_MY_SETTING>"
        assert "placeholder" in result

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, monkeypatch):
        raw, written = _mock_config({"env": {"EDITOR": "nano"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="EDITOR", value="vim")
        assert raw["env"]["EDITOR"] == "vim"


# ── ConfigEnvGetTool ─────────────────────────────────────────


class TestConfigEnvGetTool:
    @pytest.mark.asyncio
    async def test_get_from_config_fallback(self, monkeypatch):
        """When not in environ or credstore, falls back to config value."""
        raw, _ = _mock_config({"env": {"MY_SETTING": "config_val"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_SETTING")
        assert "MY_SETTING" in result
        assert "config_val" in result
        assert "[slife.json5]" in result

    @pytest.mark.asyncio
    async def test_get_from_credstore(self, monkeypatch):
        """credential_check handles keyring, not config_env_get."""
        # Import the test target
        from slife.tools.env import CredentialCheckTool

        cred = _mock_credstore(monkeypatch)
        cred["DEEPSEEK_API_KEY"] = "sk-secret-key-long"

        monkeypatch.setattr(
            "slife.tools.env.read_config",
            lambda path: {},
        )

        tool = CredentialCheckTool(config_path=Path("test.json5"))
        result = await tool.execute(key="DEEPSEEK_API_KEY")

        assert "DEEPSEEK_API_KEY" in result
        assert "[credstore" in result
        # Value must be masked
        assert "sk-secret-key-long" not in result

    @pytest.mark.asyncio
    async def test_get_from_shell_takes_priority(self, monkeypatch):
        """os.environ takes priority in config_env_get."""
        raw, _ = _mock_config({"env": {"MY_VAR": "from_config"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        os.environ["MY_VAR"] = "from_shell"

        try:
            tool = ConfigEnvGetTool(config_path=Path("test.json5"))
            result = await tool.execute(key="MY_VAR")
            assert "[shell" in result
            assert "from_shell" in result
        finally:
            os.environ.pop("MY_VAR", None)

    @pytest.mark.asyncio
    async def test_get_missing_key(self, monkeypatch):
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="NOT_THERE")
        assert "not set" in result

    @pytest.mark.asyncio
    async def test_list_all_vars(self, monkeypatch):
        raw, _ = _mock_config({"env": {"A": "val_a", "B": "val_b"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "A" in result
        assert "val_a" in result
        assert "B" in result
        assert "val_b" in result

    @pytest.mark.asyncio
    async def test_credential_check_masked_value(self, monkeypatch):
        """credential_check shows masked value from credstore."""
        from slife.tools.env import CredentialCheckTool

        cred = _mock_credstore(monkeypatch)
        cred["DEEPSEEK_API_KEY"] = "sk-secret-12345678"

        monkeypatch.setattr(
            "slife.tools.env.read_config",
            lambda path: {},
        )

        tool = CredentialCheckTool(config_path=Path("test.json5"))
        result = await tool.execute(key="DEEPSEEK_API_KEY")

        assert "[credstore" in result
        # Must be masked
        assert "sk-secret-12345678" not in result

    @pytest.mark.asyncio
    async def test_list_includes_mcp_env_sections(self, monkeypatch):
        """List-all shows env vars from mcp.servers.<name>.env sections."""
        raw, _ = _mock_config({
            "env": {"EDITOR": "code"},
            "mcp": {"servers": {
                "github": {"command": "npx", "args": [], "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
                "serper": {"command": "npx", "args": [], "env": {"SERPER_API_KEY": "${SERPER_API_KEY}"}},
            }},
        }, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "env:" in result
        assert "EDITOR" in result
        assert "mcp/github:" in result
        assert "GITHUB_TOKEN" in result
        assert "mcp/serper:" in result
        assert "SERPER_API_KEY" in result

    @pytest.mark.asyncio
    async def test_lookup_finds_mcp_env(self, monkeypatch):
        """Single-key lookup finds value in MCP server env."""
        raw, _ = _mock_config({
            "env": {},
            "mcp": {"servers": {
                "github": {"command": "npx", "args": [], "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
            }},
        }, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="GITHUB_TOKEN")
        assert "GITHUB_TOKEN" in result
        assert "mcp/github" in result
        assert "${GITHUB_TOKEN}" in result

    @pytest.mark.asyncio
    async def test_list_empty(self, monkeypatch):
        raw, _ = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))
        result = await tool.execute()
        assert "env:" in result


# ── ConfigEnvRemoveTool ──────────────────────────────────────


class TestConfigEnvRemoveTool:
    @pytest.mark.asyncio
    async def test_remove_existing_var(self, monkeypatch):
        raw, written = _mock_config({"env": {"TO_REMOVE": "bye"}}, monkeypatch)
        os.environ["TO_REMOVE"] = "bye"
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="TO_REMOVE")
        assert "TO_REMOVE" not in raw["env"]
        assert "TO_REMOVE" in os.environ  # NOT touched — only removes from config
        assert "[OK]" in result
        assert len(written) == 1

    @pytest.mark.asyncio
    async def test_remove_missing_var(self, monkeypatch):
        raw, written = _mock_config({"env": {}}, monkeypatch)
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="NOT_THERE")
        assert "nothing to remove" in result

    @pytest.mark.asyncio
    async def test_remove_does_not_touch_credstore(self, monkeypatch):
        raw, _ = _mock_config({"env": {"SECRET_KEY": "${SECRET_KEY}"}}, monkeypatch)
        cred = _mock_credstore(monkeypatch)
        cred["SECRET_KEY"] = "some-secret"
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="SECRET_KEY")
        assert "[OK]" in result
        assert "SECRET_KEY" not in raw["env"]
        assert "SECRET_KEY" in cred  # NOT touched — only removes from config

    @pytest.mark.asyncio
    async def test_remove_not_in_config_is_noop(self, monkeypatch):
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        os.environ["ONLY_IN_ENV"] = "temp"
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="ONLY_IN_ENV")
        assert "nothing to remove" in result
        assert "ONLY_IN_ENV" in os.environ  # NOT touched


# ── config_env_get: secret masking ────────────────────────────────


class TestConfigEnvGetMasking:
    """config_env_get passes values through as-is — harness sanitize_secrets() handles masking."""

    @pytest.mark.asyncio
    async def test_single_key_secret_shown_as_is(self, monkeypatch):
        """Values are shown in full — the harness masks them before LLM sees them."""
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-longsecret12345678")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="DEEPSEEK_API_KEY")
        assert "[shell]" in result
        assert "sk-deepseek-longsecret12345678" in result

    @pytest.mark.asyncio
    async def test_single_key_non_secret_shown_plaintext(self, monkeypatch):
        """Non-secret values from shell env are shown in full."""
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("EDITOR", "vim")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="EDITOR")
        assert "[shell]" in result
        assert "vim" in result

    @pytest.mark.asyncio
    async def test_list_all_shows_full_values(self, monkeypatch):
        """List-all shows full values — harness masks secrets in output."""
        raw, _ = _mock_config({
            "env": {
                "EDITOR": "code",
                "DEEPSEEK_API_KEY": "${DEEPSEEK_API_KEY}",
            },
        }, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-very-secret-key-here-98765")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "EDITOR" in result
        assert "sk-very-secret-key-here-98765" in result

    @pytest.mark.asyncio
    async def test_list_all_non_secrets_shown(self, monkeypatch):
        """List-all shows non-secrets normally."""
        raw, _ = _mock_config({
            "env": {"EDITOR": "vim", "LOG_LEVEL": "${LOG_LEVEL}"},
        }, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("LOG_LEVEL", "debug")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "debug" in result

    @pytest.mark.asyncio
    async def test_token_key_shown_as_is(self, monkeypatch):
        """Values are passed through — no tool-level masking."""
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_notarealtokenatall12345678")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="GITHUB_TOKEN")
        assert "ghp_notarealtokenatall12345678" in result

    @pytest.mark.asyncio
    async def test_password_key_shown_as_is(self, monkeypatch):
        """Values are passed through — no tool-level masking."""
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("DB_PASSWORD", "super-secret-db-password-longstr")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="DB_PASSWORD")
        assert "super-secret-db-password-longstr" in result

    @pytest.mark.asyncio
    async def test_auth_key_shown_as_is(self, monkeypatch):
        """Values are passed through — no tool-level masking."""
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        monkeypatch.setenv("AUTH_TOKEN", "abcdef1234567890abcdef1234567890ab")
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="AUTH_TOKEN")
        assert "abcdef1234567890abcdef1234567890ab" in result
