"""Tests for Slife.health — startup health collector."""

import pytest; pytestmark = pytest.mark.unit


import pytest

from unittest.mock import patch

from slife.health import (
    clear,
    get_report,
    record,
    record_active_model,
    record_host_facts,
)


class TestRecord:
    """Tests for record() function."""

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    def test_record_minimal(self):
        record("test", "ok")
        entries = get_report()
        assert len(entries) == 1
        assert entries[0]["component"] == "test"
        assert entries[0]["level"] == "ok"
        assert "key" not in entries[0]
        assert "value" not in entries[0]
        assert "hint" not in entries[0]

    def test_record_with_all_fields(self):
        record("embeddings", "warning", key="backend", value="gguf",
               hint="llama-cpp-python not installed")
        entries = get_report()
        assert len(entries) == 1
        e = entries[0]
        assert e["component"] == "embeddings"
        assert e["level"] == "warning"
        assert e["key"] == "backend"
        assert e["value"] == "gguf"
        assert e["hint"] == "llama-cpp-python not installed"

    def test_record_with_key_only(self):
        record("mcp", "error", key="connection")
        entries = get_report()
        assert len(entries) == 1
        e = entries[0]
        assert e["key"] == "connection"
        assert "value" not in e
        assert "hint" not in e

    def test_record_with_value_only(self):
        record("config", "warning", value="missing")
        entries = get_report()
        assert len(entries) == 1
        e = entries[0]
        assert "key" not in e
        assert e["value"] == "missing"

    def test_record_with_hint_only(self):
        record("config", "ok", hint="check logs")
        entries = get_report()
        assert len(entries) == 1
        e = entries[0]
        assert e["hint"] == "check logs"

    def test_record_multiple_entries_ordered(self):
        record("first", "ok")
        record("second", "warning")
        record("third", "error")
        entries = get_report()
        assert len(entries) == 3
        assert entries[0]["component"] == "first"
        assert entries[1]["component"] == "second"
        assert entries[2]["component"] == "third"


class TestGetReport:
    """Tests for get_report() function."""

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    def test_empty_report(self):
        entries = get_report()
        assert entries == []

    def test_returns_copy_not_reference(self):
        record("test", "ok")
        entries = get_report()
        entries.append({"component": "extra", "level": "ok"})
        # Original internal list should be unchanged
        assert len(get_report()) == 1

    def test_report_sorted_by_insertion(self):
        record("c", "ok")
        record("a", "ok")
        record("b", "warning")
        entries = get_report()
        assert [e["component"] for e in entries] == ["c", "a", "b"]


class TestRecordActiveModel:
    """Tests for record_active_model() — the one definition of the model fact
    both producers write (startup and a live model switch)."""

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    @staticmethod
    def _model(**overrides):
        from slife.config import ModelConfig
        fields = {
            "ref": "deepseek/deepseek-flash",
            "provider": "deepseek",
            "api_model": "deepseek-flash",
            "display_name": "DeepSeek V4.1 Flash",
            "api_key": "sk-x",
            "supports_vision": True,
            "thinking_enabled": True,
            "context_window": 1000000,
        }
        fields.update(overrides)
        return ModelConfig(**fields)

    def test_records_the_facts_the_report_prints(self):
        record_active_model(self._model())
        e = get_report()[-1]
        assert e["component"] == "model" and e["key"] == "active"
        assert e["level"] == "ok"
        assert e["value"] == (
            "deepseek/deepseek-flash (thinking=on, vision=on, ctx 1000000)"
        )
        # The value/hint rule: a healthy fact carries no remedy.
        assert "hint" not in e

    def test_a_capability_off_is_stated(self):
        """`off` is a fact, not an absence — the report says so explicitly."""
        record_active_model(self._model(supports_vision=False, thinking_enabled=False))
        assert get_report()[-1]["value"] == (
            "deepseek/deepseek-flash (thinking=off, vision=off, ctx 1000000)"
        )

    def test_a_later_record_supersedes_the_earlier_one(self):
        record_active_model(self._model())
        record_active_model(self._model(ref="deepseek/deepseek-v4-pro"))
        entries = get_report()
        assert len(entries) == 1
        assert entries[0]["value"].startswith("deepseek/deepseek-v4-pro ")


class TestRecordHostFacts:
    """The facts that are about the HOST, not this process — one recorder for
    both entry points (the TUI and a headless subagent worker)."""

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    @staticmethod
    def _config():
        from slife.config import Config, ModelConfig
        return Config(
            models=[ModelConfig(
                ref="deepseek/deepseek-flash", provider="deepseek",
                api_model="deepseek-flash", display_name="Flash",
                api_key="sk-x", context_window=1000,
            )],
            active_model_ref="deepseek/deepseek-flash",
            tools=[],
        )

    def test_records_the_component_set_every_process_reports(self):
        """`config` and `model` here; the toolchain (node/npm/bun/uv) from the
        probe this starts.  The whole set is what a worker used to be missing:
        it reported 14 components against its parent's 20, and nothing in
        either report said which facts were absent."""
        with patch("slife.health.check_external_deps") as probe:
            record_host_facts(self._config(), source="/tmp/slife.yaml")

        components = {e["component"] for e in get_report()}
        assert components == {"config", "model"}
        assert probe.call_count == 1          # the toolchain half is started
        cfg = next(e for e in get_report() if e["component"] == "config")
        assert cfg["key"] == "path"
        assert cfg["value"].startswith("/tmp/slife.yaml (")

    def test_the_source_names_where_this_process_got_its_config(self):
        """A worker's config is inherited, not a file it can point at — the
        fact says so rather than printing a path nobody can open."""
        with patch("slife.health.check_external_deps"):
            record_host_facts(self._config(), source="inherited from the main agent")
        cfg = next(e for e in get_report() if e["component"] == "config")
        assert cfg["value"].startswith("inherited from the main agent (")


class TestClear:
    """Tests for clear() function."""

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    def test_clear_empties_entries(self):
        record("test1", "ok")
        record("test2", "error")
        assert len(get_report()) == 2
        clear()
        assert get_report() == []

    def test_clear_on_empty_is_noop(self):
        assert get_report() == []
        clear()
        assert get_report() == []
