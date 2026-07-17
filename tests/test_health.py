"""Tests for Slife.health — startup health collector."""

import pytest

from slife.health import record, get_report, clear


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
