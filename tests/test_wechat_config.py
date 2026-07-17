"""Tests for slife.plugins.wechat.config — per-user WeChat config I/O."""

import json5
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from slife.plugins.wechat.config import (
    _config_path,
    load_wechat_config,
    save_wechat_config,
    clear_wechat_config,
    DEFAULT_BASE_URL,
)


# ── Mock credstore so tests don't touch real keyring ──────────


@pytest.fixture(autouse=True)
def _mock_credstore(monkeypatch):
    """Mock credstore to an in-memory dict — prevents real keyring access."""
    data = {}

    def _get(key):
        return data.get(key)

    def _set(key, secret):
        data[key] = secret

    def _delete(key):
        return data.pop(key, None) is not None

    # Patch credstore public API
    import credstore
    monkeypatch.setattr(credstore, "get_credential", _get)
    monkeypatch.setattr(credstore, "delete_credential", _delete)

    # Patch the internal module too
    import credstore._store as sm
    monkeypatch.setattr(sm, "init_store", lambda **kw: None)
    store = sm.CredentialStore()
    store.get = _get
    store.set = _set
    store.delete = _delete
    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)
    monkeypatch.setattr(sm, "get_credential", _get)
    monkeypatch.setattr(sm, "delete_credential", _delete)

    # Patch wechat's internal _credstore_set to use our in-memory dict
    import slife.plugins.wechat.config as wc
    monkeypatch.setattr(wc, "_credstore_set", _set)

    # Mock backend to prevent real keyring init
    import credstore._backend as backend
    monkeypatch.setattr(backend, "init_backend", lambda **kw: None)
    monkeypatch.setattr(backend, "get_system_keyring", lambda: None)
    monkeypatch.setattr(backend, "get_cryptfile", lambda: None)
    monkeypatch.setattr(backend, "has_master_key", lambda: True)



class TestConfigPath:
    """Tests for _config_path helper."""

    def test_default_work_dir_is_cwd(self):
        path = _config_path("testuser")
        assert path.name == "wechat_testuser.json5"

    def test_custom_work_dir(self):
        path = _config_path("alice", work_dir=Path("/tmp/custom"))
        assert path == Path("/tmp/custom/wechat_alice.json5")

    def test_path_is_absolute(self):
        path = _config_path("bob", work_dir=Path("/abs/path"))
        assert path == Path("/abs/path/wechat_bob.json5")

    def test_user_name_with_special_chars(self):
        path = _config_path("user@domain")
        assert path.name == "wechat_user@domain.json5"


class TestLoadWechatConfig:
    """Tests for load_wechat_config()."""

    def test_file_not_found_returns_empty(self, tmp_path):
        result = load_wechat_config("nobody", work_dir=tmp_path)
        assert result == {}

    def test_loads_valid_config(self, tmp_path):
        path = tmp_path / "wechat_test.json5"
        path.write_text(json5.dumps({
            "bot_token": "abc123",
            "base_url": "https://custom.example.com",
            "saved_at": 1718400000.0,
        }), encoding="utf-8")
        result = load_wechat_config("test", work_dir=tmp_path)
        assert result["bot_token"] == "abc123"
        assert result["base_url"] == "https://custom.example.com"
        assert result["saved_at"] == 1718400000.0

    def test_missing_keys_get_defaults(self, tmp_path):
        path = tmp_path / "wechat_test.json5"
        path.write_text(json5.dumps({"bot_token": "token"}), encoding="utf-8")
        result = load_wechat_config("test", work_dir=tmp_path)
        assert result["bot_token"] == "token"
        assert result["base_url"] == DEFAULT_BASE_URL
        assert result["saved_at"] == 0
        assert result["ilink_user_id"] == ""

    def test_malformed_json_returns_empty(self, tmp_path):
        path = tmp_path / "wechat_test.json5"
        path.write_text("{not valid json5}", encoding="utf-8")
        result = load_wechat_config("test", work_dir=tmp_path)
        assert result == {}

    def test_non_dict_json_returns_empty(self, tmp_path):
        path = tmp_path / "wechat_test.json5"
        path.write_text('"just a string"', encoding="utf-8")
        result = load_wechat_config("test", work_dir=tmp_path)
        assert result == {}

    def test_empty_dict_works(self, tmp_path):
        path = tmp_path / "wechat_test.json5"
        path.write_text("{}", encoding="utf-8")
        result = load_wechat_config("test", work_dir=tmp_path)
        assert result["bot_token"] == ""
        assert result["base_url"] == DEFAULT_BASE_URL
        assert result["saved_at"] == 0

    def test_with_ilink_user_id(self, tmp_path):
        path = tmp_path / "wechat_test.json5"
        path.write_text(json5.dumps({
            "bot_token": "tok",
            "ilink_user_id": "wxid_abc123",
        }), encoding="utf-8")
        result = load_wechat_config("test", work_dir=tmp_path)
        assert result["ilink_user_id"] == "wxid_abc123"


class TestSaveWechatConfig:
    """Tests for save_wechat_config()."""

    def test_saves_to_file(self, tmp_path):
        session = {"bot_token": "new_token", "saved_at": 1234567890.0}
        result_path = save_wechat_config("testuser", session, work_dir=tmp_path)
        assert result_path.exists()
        assert result_path.name == "wechat_testuser.json5"

    def test_saved_content_roundtrips(self, tmp_path):
        session = {
            "bot_token": "my-bot-token",
            "base_url": "https://custom.example.com",
            "saved_at": 1718400000.0,
        }
        saved = save_wechat_config("user", session, work_dir=tmp_path)
        loaded = load_wechat_config("user", work_dir=tmp_path)
        assert loaded["bot_token"] == "my-bot-token"
        assert loaded["base_url"] == "https://custom.example.com"
        assert loaded["saved_at"] == 1718400000.0

    def test_defaults_applied_for_missing_keys(self, tmp_path):
        session = {"bot_token": "token_only"}
        saved = save_wechat_config("user", session, work_dir=tmp_path)
        loaded = load_wechat_config("user", work_dir=tmp_path)
        assert loaded["bot_token"] == "token_only"
        assert loaded["base_url"] == DEFAULT_BASE_URL
        assert loaded["saved_at"] == 0
        assert loaded["ilink_user_id"] == ""

    def test_saves_ilink_user_id(self, tmp_path):
        session = {
            "bot_token": "tok",
            "ilink_user_id": "wxid_xyz",
            "saved_at": 1000.0,
        }
        saved = save_wechat_config("user", session, work_dir=tmp_path)
        loaded = load_wechat_config("user", work_dir=tmp_path)
        assert loaded["ilink_user_id"] == "wxid_xyz"

    def test_ilink_user_id_empty_not_written(self, tmp_path):
        session = {"bot_token": "tok", "saved_at": 0}
        path = save_wechat_config("user", session, work_dir=tmp_path)
        content = path.read_text(encoding="utf-8")
        # ilink_user_id should not appear when empty
        assert "ilink_user_id" not in content

    def test_returns_path_object(self, tmp_path):
        session = {"bot_token": "a", "saved_at": 0}
        result = save_wechat_config("x", session, work_dir=tmp_path)
        assert isinstance(result, Path)


class TestClearWechatConfig:
    """Tests for clear_wechat_config()."""

    def test_clears_existing_file(self, tmp_path):
        session = {"bot_token": "token", "saved_at": 0}
        path = save_wechat_config("user", session, work_dir=tmp_path)
        assert path.exists()
        result = clear_wechat_config("user", work_dir=tmp_path)
        assert result is True
        assert not path.exists()

    def test_clear_nonexistent_file(self, tmp_path):
        result = clear_wechat_config("nobody", work_dir=tmp_path)
        assert result is False

    def test_clear_twice_second_returns_false(self, tmp_path):
        session = {"bot_token": "token", "saved_at": 0}
        save_wechat_config("user", session, work_dir=tmp_path)
        clear_wechat_config("user", work_dir=tmp_path)
        result = clear_wechat_config("user", work_dir=tmp_path)
        assert result is False


class TestIntegration:
    """Round-trip integration tests."""

    def test_save_load_clear_flow(self, tmp_path):
        # Save
        session = {"bot_token": "flow_token", "saved_at": 5000.0}
        save_wechat_config("flow", session, work_dir=tmp_path)

        # Load
        loaded = load_wechat_config("flow", work_dir=tmp_path)
        assert loaded["bot_token"] == "flow_token"

        # Clear
        assert clear_wechat_config("flow", work_dir=tmp_path) is True
        assert load_wechat_config("flow", work_dir=tmp_path) == {}

    def test_config_per_user_isolation(self, tmp_path):
        save_wechat_config("alice", {"bot_token": "alice_token", "saved_at": 1},
                           work_dir=tmp_path)
        save_wechat_config("bob", {"bot_token": "bob_token", "saved_at": 2},
                           work_dir=tmp_path)

        alice = load_wechat_config("alice", work_dir=tmp_path)
        bob = load_wechat_config("bob", work_dir=tmp_path)
        assert alice["bot_token"] == "alice_token"
        assert bob["bot_token"] == "bob_token"
