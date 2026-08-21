"""Tests for provider config storage (cc_config._api)."""

import pytest

pytestmark = pytest.mark.unit

import cc_config._api as api


def make_provider(**overrides):
    base = {
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key_name": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "extra_env": {},
    }
    base.update(overrides)
    return base


class TestLoadConfig:
    def test_missing_file_yields_empty(self, config_path):
        assert api.load_config() == {"providers": {}}

    def test_unparseable_file_yields_empty(self, config_path):
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        assert api.load_config() == {"providers": {}}

    def test_wrong_shape_yields_empty(self, config_path):
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write('{"foo": [1,2]}')
        assert api.load_config() == {"providers": {}}


class TestAddProvider:
    def test_add_new(self, config_path):
        api.add_provider("deepseek", "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY", ["a", "b"])
        data = api.load_config()
        assert data["providers"]["deepseek"] == {
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key_name": "DEEPSEEK_API_KEY",
            "models": ["a", "b"],
            "extra_env": {},
        }

    def test_add_with_extra_env(self, config_path):
        api.add_provider("ds", "https://x", "K", ["m"], extra_env={"ANTHROPIC_MODEL": "x"})
        assert api.load_config()["providers"]["ds"]["extra_env"] == {"ANTHROPIC_MODEL": "x"}

    def test_add_overwrites_existing(self, config_path):
        api.add_provider("ds", "https://one", "K1", ["a"])
        api.add_provider("ds", "https://two", "K2", ["b", "c"])
        p = api.load_config()["providers"]["ds"]
        assert p["base_url"] == "https://two"
        assert p["api_key_name"] == "K2"
        assert p["models"] == ["b", "c"]


class TestUpdateProvider:
    def test_update_core_fields(self, config_path):
        api.add_provider("ds", "https://one", "K1", ["a"])
        api.update_provider("ds", base_url="https://two", models=["x", "y"])
        p = api.load_config()["providers"]["ds"]
        assert p["base_url"] == "https://two"
        assert p["api_key_name"] == "K1"  # untouched
        assert p["models"] == ["x", "y"]

    def test_update_unknown_raises(self, config_path):
        with pytest.raises(KeyError):
            api.update_provider("missing", base_url="https://x")


class TestRemoveProvider:
    def test_remove_existing(self, config_path):
        api.add_provider("ds", "https://x", "K", ["a"])
        assert api.remove_provider("ds") is True
        assert api.list_providers() == []

    def test_remove_missing(self, config_path):
        assert api.remove_provider("nonexistent") is False


class TestListProviders:
    def test_sorted(self, config_path):
        api.add_provider("zeta", "https://z", "Z", [])
        api.add_provider("alpha", "https://a", "A", ["m"])
        api.add_provider("mid", "https://m", "M", [])
        assert api.list_providers() == ["alpha", "mid", "zeta"]

    def test_empty(self, config_path):
        assert api.list_providers() == []