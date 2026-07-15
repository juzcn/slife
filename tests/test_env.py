"""Tests for Slife.env — environment variable resolution."""

import os

import pytest

from slife.env import resolve_env


# ── String resolution ─────────────────────────────────────────────────


class TestResolveEnvStrings:
    """Tests for resolve_env with string values."""

    def test_plain_string_unchanged(self):
        """Plain strings without env refs pass through unchanged."""
        assert resolve_env("hello world") == "hello world"
        assert resolve_env("no variables here") == "no variables here"
        assert resolve_env("") == ""

    def test_simple_var_resolution(self, monkeypatch):
        """${VAR} resolves from environment."""
        monkeypatch.setenv("MY_VAR", "resolved_value")
        assert resolve_env("${MY_VAR}") == "resolved_value"

    def test_var_in_middle_of_string(self, monkeypatch):
        """Env var embedded in a larger string."""
        monkeypatch.setenv("NAME", "Alice")
        assert resolve_env("Hello, ${NAME}!") == "Hello, Alice!"

    def test_multiple_vars_in_string(self, monkeypatch):
        """Multiple env vars in one string."""
        monkeypatch.setenv("FIRST", "foo")
        monkeypatch.setenv("LAST", "bar")
        assert resolve_env("${FIRST} ${LAST}") == "foo bar"

    def test_default_value_used(self):
        """${VAR:-default} uses default when var unset."""
        assert resolve_env("${MISSING:-fallback}") == "fallback"

    def test_default_value_skipped_when_set(self, monkeypatch):
        """Default is not used when var is set."""
        monkeypatch.setenv("EXISTS", "real")
        assert resolve_env("${EXISTS:-fallback}") == "real"

    def test_default_empty_string(self):
        """${VAR:-} with empty default."""
        assert resolve_env("${MISSING:-}") == ""

    def test_missing_var_raises(self):
        """Missing var without default raises KeyError."""
        with pytest.raises(KeyError) as exc_info:
            resolve_env("${DEFINITELY_NOT_SET_12345}")
        assert "DEFINITELY_NOT_SET_12345" in str(exc_info.value)

    def test_var_with_special_chars(self, monkeypatch):
        """Var name may contain underscores and digits."""
        monkeypatch.setenv("DB_PORT_2", "5432")
        assert resolve_env("${DB_PORT_2}") == "5432"

    def test_repeated_var(self, monkeypatch):
        """Same var referenced multiple times resolves each."""
        monkeypatch.setenv("X", "1")
        assert resolve_env("${X} ${X} ${X}") == "1 1 1"


# ── Dict resolution ───────────────────────────────────────────────────


class TestResolveEnvDicts:
    """Tests for resolve_env with dict values."""

    def test_dict_values_resolved(self, monkeypatch):
        """String values in dicts are resolved."""
        monkeypatch.setenv("KEY", "secret")
        result = resolve_env({"api_key": "${KEY}", "url": "https://example.com"})
        assert result == {"api_key": "secret", "url": "https://example.com"}

    def test_nested_dict_resolved(self, monkeypatch):
        """Nested dicts are recursively resolved."""
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_PORT", "5432")
        result = resolve_env({
            "database": {
                "host": "${DB_HOST}",
                "port": "${DB_PORT}",
            }
        })
        assert result == {"database": {"host": "localhost", "port": "5432"}}

    def test_empty_dict(self):
        """Empty dicts are unchanged."""
        assert resolve_env({}) == {}

    def test_non_string_values_preserved(self):
        """Non-string values in dicts are kept as-is."""
        result = resolve_env({"count": 42, "flag": True, "nested": {"x": 1.5}})
        assert result == {"count": 42, "flag": True, "nested": {"x": 1.5}}


# ── List resolution ───────────────────────────────────────────────────


class TestResolveEnvLists:
    """Tests for resolve_env with list values."""

    def test_list_items_resolved(self, monkeypatch):
        """String items in lists are resolved."""
        monkeypatch.setenv("A", "alpha")
        monkeypatch.setenv("B", "beta")
        result = resolve_env(["${A}", "${B}", "plain"])
        assert result == ["alpha", "beta", "plain"]

    def test_list_of_dicts(self, monkeypatch):
        """Dicts inside lists are recursively resolved."""
        monkeypatch.setenv("TOKEN", "abc123")
        monkeypatch.setenv("URL", "https://api.example.com")
        result = resolve_env([
            {"key": "${TOKEN}", "url": "${URL}"},
            {"key": "static"},
        ])
        assert result == [
            {"key": "abc123", "url": "https://api.example.com"},
            {"key": "static"},
        ]

    def test_empty_list(self):
        """Empty lists are unchanged."""
        assert resolve_env([]) == []


# ── Scalar resolution ─────────────────────────────────────────────────


class TestResolveEnvScalars:
    """Tests for resolve_env with non-string scalar values."""

    def test_int_unchanged(self):
        assert resolve_env(42) == 42

    def test_float_unchanged(self):
        assert resolve_env(3.14) == 3.14

    def test_bool_unchanged(self):
        assert resolve_env(True) is True
        assert resolve_env(False) is False

    def test_none_unchanged(self):
        assert resolve_env(None) is None


# ── Edge cases ────────────────────────────────────────────────────────


class TestResolveEnvEdgeCases:
    """Edge cases for env resolution."""

    def test_default_contains_colons(self, monkeypatch):
        """Default value can contain colons and special chars."""
        result = resolve_env("${MISSING:-http://localhost:8080/path?q=1}")
        assert result == "http://localhost:8080/path?q=1"

    def test_default_contains_braces(self, monkeypatch):
        """Default value can contain braces."""
        result = resolve_env("${MISSING:-{key: value}}")
        assert result == "{key: value}"

    def test_deeply_nested_structure(self, monkeypatch):
        """Deep nesting works correctly."""
        monkeypatch.setenv("VAL", "done")
        result = resolve_env({
            "a": [{"b": [{"c": "${VAL}"}]}]
        })
        assert result == {"a": [{"b": [{"c": "done"}]}]}

    def test_var_with_no_default_no_brace_space(self, monkeypatch):
        """Pattern does not match without closing brace."""
        # This is just a literal string, not a pattern
        assert resolve_env("${NOT_CLOSED") == "${NOT_CLOSED"

    def test_default_colon_brace_edge(self):
        """Default with :-} (closing brace in default value)."""
        result = resolve_env("${MISSING:-}")
        assert result == ""

    def test_bytes_passthrough(self):
        """Bytes values pass through unchanged."""
        assert resolve_env(b"raw \x00 bytes") == b"raw \x00 bytes"
