"""Tests for activation (cc_config._activate): settings generation + env injection.

The credstore secret is mocked at ``resolve_secret`` so the tests never
touch a real keyring.
"""

import io
import json
import os

import pytest

pytestmark = pytest.mark.unit

import cc_config._activate as act
from cc_config._defaults import DEFAULT_TEMPLATE


PROVIDER = {
    "base_url": "https://api.deepseek.com/anthropic",
    "api_key_name": "DEEPSEEK_API_KEY",
    "models": ["deepseek-chat", "deepseek-reasoner"],
    "extra_env": {},
}


class TestBuildEnv:
    def test_default_slots_filled(self):
        env = act.build_env(PROVIDER, "deepseek-chat")
        assert env["ANTHROPIC_BASE_URL"] == PROVIDER["base_url"]
        assert env["ANTHROPIC_MODEL"] == "deepseek-chat"
        # every default slot present, base value untouched
        for key in ("ANTHROPIC_DEFAULT_HAIKU_MODEL",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_CLAUDE_CODE_SUBAGENT_MODEL",
                    "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL"):
            assert key in env
            assert env[key] == DEFAULT_TEMPLATE[key]

    def test_overrides_applied(self):
        env = act.build_env(PROVIDER, "m", overrides={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku-x"})
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "haiku-x"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == DEFAULT_TEMPLATE["ANTHROPIC_DEFAULT_SONNET_MODEL"]

    def test_override_clear_with_empty_string(self):
        env = act.build_env(PROVIDER, "m", overrides={"ANTHROPIC_DEFAULT_HAIKU_MODEL": ""})
        # explicit empty override clears the slot
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == ""

    def test_main_model_not_overridable_by_custom(self):
        # ANTHROPIC_MODEL comes from the CLI model argument and must win
        env = act.build_env(PROVIDER, "cli-model", overrides={"ANTHROPIC_MODEL": "sneaky"})
        assert env["ANTHROPIC_MODEL"] == "cli-model"

    def test_extra_env_overrides_slot(self):
        provider = dict(PROVIDER, extra_env={"ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL": "max"})
        env = act.build_env(provider, "m")
        assert env["ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL"] == "max"

    def test_no_secret_in_env(self):
        env = act.build_env(PROVIDER, "m")
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        values = " ".join(env.values())
        assert "sk-" not in values


class TestBuildSettings:
    def test_contains_env_and_auto_updates(self):
        settings = act.build_settings(PROVIDER, "m")
        assert settings["env"]["ANTHROPIC_MODEL"] == "m"
        assert settings["env"]["ANTHROPIC_BASE_URL"] == PROVIDER["base_url"]
        assert settings["autoUpdatesChannel"] == "latest"

    def test_written_file_has_no_credential(self, settings_path):
        s = act.build_settings(PROVIDER, "deepseek-chat")
        act.write_settings(s)
        raw = open(act.SETTINGS_PATH, encoding="utf-8").read()
        assert "ANTHROPIC_AUTH_TOKEN" not in raw
        assert "sk-" not in raw
        parsed = json.loads(raw)
        assert parsed["env"]["ANTHROPIC_MODEL"] == "deepseek-chat"


class TestInjectToken:
    def test_persists_to_system_env(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            "credstore._shell.persist_key",
            lambda key, value, shell="auto": calls.update({"key": key, "value": value, "shell": shell}),
        )
        out = io.StringIO()
        act.inject_token("sk-secret", shell="powershell", output=out)
        assert calls["key"] == "ANTHROPIC_AUTH_TOKEN"
        assert calls["value"] == "sk-secret"
        assert calls["shell"] == "powershell"

    def test_piped_output_emits_export_line(self, monkeypatch):
        monkeypatch.setattr("credstore._shell.persist_key", lambda *a, **kw: None)
        out = io.StringIO()
        act.inject_token("sk-secret", shell="bash", output=out)
        assert "ANTHROPIC_AUTH_TOKEN" in out.getvalue()
        assert "sk-secret" in out.getvalue()

    def test_tty_output_prints_hint_without_secret(self, monkeypatch):
        monkeypatch.setattr("credstore._shell.persist_key", lambda *a, **kw: None)
        out = io.StringIO()
        out.isatty = lambda: True  # type: ignore[assignment]
        act.inject_token("sk-secret", shell="bash", output=out)
        text = out.getvalue()
        assert "ANTHROPIC_AUTH_TOKEN" in text
        assert "sk-secret" not in text


class TestActivate:
    def test_writes_settings_and_injects_env(self, settings_path, monkeypatch):
        monkeypatch.setattr(act, "resolve_secret", lambda _name: "sk-test-secret")
        monkeypatch.setattr(act, "inject_token", lambda *a, **kw: None)

        result = act.activate(PROVIDER, "deepseek-chat")

        # settings file clean
        raw = open(act.SETTINGS_PATH, encoding="utf-8").read()
        assert "sk-test-secret" not in raw
        assert result["env"]["ANTHROPIC_MODEL"] == "deepseek-chat"

    def test_inject_token_receives_secret(self, settings_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(act, "resolve_secret", lambda _name: "sk-test-secret")
        monkeypatch.setattr(act, "inject_token", lambda *a, **kw: captured.update({"args": a}))

        act.activate(PROVIDER, "deepseek-chat")
        assert captured["args"][0] == "sk-test-secret"

    def test_missing_secret_raises_and_writes_settings(self, settings_path, monkeypatch):
        monkeypatch.setattr(act, "resolve_secret", lambda _name: None)

        with pytest.raises(act.SecretNotFoundError):
            act.activate(PROVIDER, "deepseek-chat")
        # settings written before the secret lookup, so the shape is in place
        assert os.path.exists(act.SETTINGS_PATH)

    def test_credstore_unavailable_raises_secret_not_found(self, settings_path, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "credstore":
                raise ImportError("not installed")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(act.SecretNotFoundError):
            act.activate(PROVIDER, "deepseek-chat")
        assert os.path.exists(act.SETTINGS_PATH)