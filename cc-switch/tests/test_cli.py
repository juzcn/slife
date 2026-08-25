"""Tests for the cc-switch CLI (cc_switch.cli)."""

import builtins
import json
import os

import pytest

pytestmark = pytest.mark.unit

from cc_switch import _activate, _api, cli


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def no_real_inject(monkeypatch):
    """Block the real system-env persistence during activate tests."""
    monkeypatch.setattr(_activate, "inject_token", lambda *a, **kw: None)


def feed_inputs(monkeypatch, values: list[str]):
    """Drive interactive ``input()`` calls with the given values."""
    it = iter(values)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))


class TestSet:
    def test_add_provider(self, config_path, monkeypatch, capsys):
        feed_inputs(monkeypatch, ["https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY", "chat,reasoner"])
        assert cli.main(["set", "deepseek"]) == 0
        p = _api.load_config()["providers"]["deepseek"]
        assert p["base_url"] == "https://api.deepseek.com/anthropic"
        assert p["api_key_name"] == "DEEPSEEK_API_KEY"
        assert p["models"] == ["chat", "reasoner"]

    def test_edit_existing_keeps_untouched_fields(self, config_path, monkeypatch):
        _api.add_provider("deepseek", "https://old", "OLD_KEY", ["a"])
        # blank for api_key_name keeps current; new models given
        feed_inputs(monkeypatch, ["https://new", "", "b,c"])
        assert cli.main(["set", "deepseek"]) == 0
        p = _api.load_config()["providers"]["deepseek"]
        assert p["base_url"] == "https://new"
        assert p["api_key_name"] == "OLD_KEY"  # blank kept current
        # 'a' isn't in the input, so it survives the symmetric difference
        assert p["models"] == ["b", "c", "a"]

    def test_required_cannot_be_empty(self, config_path, monkeypatch):
        feed_inputs(monkeypatch, ["", "https://valid", "K", ""])
        assert cli.main(["set", "ds"]) == 0
        p = _api.load_config()["providers"]["ds"]
        assert p["base_url"] == "https://valid"

    def test_models_accept_mixed_separators(self, config_path, monkeypatch):
        feed_inputs(monkeypatch, ["https://x", "K", "a,b c;d"])
        assert cli.main(["set", "ds"]) == 0
        p = _api.load_config()["providers"]["ds"]
        assert p["models"] == ["a", "b", "c", "d"]
        assert len(p["models"]) == 4

    def test_add_provider_models_via_symmetric_difference(self, config_path, monkeypatch):
        # new provider: empty default list -> models are just the input
        feed_inputs(monkeypatch, ["https://x", "K", "a,b"])
        assert cli.main(["set", "ds"]) == 0
        assert _api.load_config()["providers"]["ds"]["models"] == ["a", "b"]

    def test_edit_toggles_models_against_current_list(self, config_path, monkeypatch):
        _api.add_provider("ds", "https://old", "K", ["a", "b", "c"])
        # input {b,c,d} symmetric-diff with stored {a,b,c} -> {d,a}
        feed_inputs(monkeypatch, ["https://new", "", "b,c,d"])
        assert cli.main(["set", "ds"]) == 0
        p = _api.load_config()["providers"]["ds"]
        assert p["base_url"] == "https://new"
        assert p["models"] == ["d", "a"]

    def test_edit_same_models_removes_them(self, config_path, monkeypatch):
        _api.add_provider("ds", "https://x", "K", ["a", "b"])
        # typing the full current list toggles it off -> empty
        feed_inputs(monkeypatch, ["", "", "a,b"])
        assert cli.main(["set", "ds"]) == 0
        assert _api.load_config()["providers"]["ds"]["models"] == []

    def test_edit_blank_models_keeps_current(self, config_path, monkeypatch):
        # blank input = difference with the empty list = current list itself
        _api.add_provider("ds", "https://x", "K", ["a", "b"])
        feed_inputs(monkeypatch, ["https://new", "", ""])
        assert cli.main(["set", "ds"]) == 0
        p = _api.load_config()["providers"]["ds"]
        assert p["base_url"] == "https://new"
        assert p["models"] == ["a", "b"]


class TestRemove:
    def test_remove_existing(self, config_path, monkeypatch, capsys):
        _api.add_provider("ds", "https://x", "K", ["m"])
        assert cli.main(["remove", "ds"]) == 0
        assert _api.list_providers() == []

    def test_remove_missing_returns_1(self, config_path, monkeypatch, capsys):
        assert cli.main(["remove", "nope"]) == 1


class TestList:
    def test_empty(self, config_path, capsys):
        assert cli.main([]) == 0
        out = capsys.readouterr().out
        assert "No providers configured" in out

    def test_lists_provider_model(self, config_path, capsys):
        _api.add_provider("ds", "https://x", "K", ["a", "b"])
        _api.add_provider("sc", "https://y", "SK", ["c"])
        assert cli.main([]) == 0
        out = capsys.readouterr().out
        assert "ds/a" in out and "ds/b" in out and "sc/c" in out


class TestActivate:
    def test_single_model_auto_select(self, config_path, settings_path, monkeypatch, capsys, no_real_inject):
        _api.add_provider("ds", "https://x", "DEEPSEEK_API_KEY", ["only-model"])
        monkeypatch.setattr(_activate, "resolve_secret", lambda _n: "sk-secret")
        monkeypatch.setattr(_activate, "SETTINGS_PATH", settings_path)

        assert cli.main(["activate", "ds/only-model"]) == 0
        parsed = json.load(open(settings_path, encoding="utf-8"))
        assert parsed["env"]["ANTHROPIC_MODEL"] == "only-model"
        out = capsys.readouterr().out
        assert "Injected ANTHROPIC_AUTH_TOKEN" in out
        raw = open(settings_path, encoding="utf-8").read()
        assert "sk-secret" not in raw

    def test_unknown_provider(self, config_path, settings_path, monkeypatch, capsys):
        assert cli.main(["activate", "nope/m"]) == 1

    def test_missing_model_prompts_pick(self, config_path, settings_path, monkeypatch, capsys, no_real_inject):
        _api.add_provider("ds", "https://x", "K", ["a", "b"])
        monkeypatch.setattr(_activate, "resolve_secret", lambda _n: "s")
        monkeypatch.setattr(_activate, "SETTINGS_PATH", settings_path)
        feed_inputs(monkeypatch, ["2"])  # pick second model
        assert cli.main(["activate", "ds"]) == 0
        parsed = json.load(open(settings_path, encoding="utf-8"))
        assert parsed["env"]["ANTHROPIC_MODEL"] == "b"

    def test_missing_secret_fails_and_keeps_settings(self, config_path, settings_path, monkeypatch, capsys):
        _api.add_provider("ds", "https://x", "MISSING_KEY", ["m1"])
        monkeypatch.setattr(_activate, "resolve_secret", lambda _n: None)
        monkeypatch.setattr(_activate, "SETTINGS_PATH", settings_path)
        assert cli.main(["activate", "ds/m1"]) == 1
        # settings.json still written (shape in place) but env NOT injected
        assert os.path.exists(settings_path)
        assert "Injected ANTHROPIC_AUTH_TOKEN" not in capsys.readouterr().out

    def test_custom_applies_overrides(self, config_path, settings_path, monkeypatch, capsys, no_real_inject):
        _api.add_provider("ds", "https://x", "K", ["m1"])
        monkeypatch.setattr(_activate, "resolve_secret", lambda _n: "s")
        monkeypatch.setattr(_activate, "SETTINGS_PATH", settings_path)
        # The 5 override prompts (ANTHROPIC_MODEL is NOT a prompt):
        # HAIKU="", SONNET="sonnet-x", OPUS="", SUBAGENT="", EFFORT="max"
        feed_inputs(monkeypatch, ["", "sonnet-x", "", "", "max"])
        assert cli.main(["activate", "ds/m1", "--custom"]) == 0
        parsed = json.load(open(settings_path, encoding="utf-8"))
        env = parsed["env"]
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "sonnet-x"
        assert env["ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL"] == "max"
        assert env["ANTHROPIC_MODEL"] == "m1"

    def test_custom_dash_clears_field(self, config_path, settings_path, monkeypatch, capsys, no_real_inject):
        _api.add_provider("ds", "https://x", "K", ["m1"])
        monkeypatch.setattr(_activate, "resolve_secret", lambda _n: "s")
        monkeypatch.setattr(_activate, "SETTINGS_PATH", settings_path)
        feed_inputs(monkeypatch, ["-", "", "", "", ""])  # clear HAIKU
        assert cli.main(["activate", "ds/m1", "--custom"]) == 0
        env = json.load(open(settings_path, encoding="utf-8"))["env"]
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == ""