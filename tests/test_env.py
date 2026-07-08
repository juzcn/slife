"""Tests for environment variable resolution (slife.env)."""

import os

import pytest

from slife.env import resolve_env


# ── Plain values (no env refs) ───────────────────────────────────────


def test_resolve_plain_string():
    """Strings without env refs pass through unchanged."""
    assert resolve_env("hello world") == "hello world"


def test_resolve_empty_string():
    """Empty string passes through."""
    assert resolve_env("") == ""


def test_resolve_int():
    """Integer values pass through unchanged."""
    assert resolve_env(42) == 42


def test_resolve_float():
    """Float values pass through unchanged."""
    assert resolve_env(3.14) == 3.14


def test_resolve_bool():
    """Boolean values pass through unchanged."""
    assert resolve_env(True) is True
    assert resolve_env(False) is False


def test_resolve_none():
    """None passes through unchanged."""
    assert resolve_env(None) is None


def test_resolve_bytes():
    """Bytes values pass through unchanged."""
    assert resolve_env(b"hello") == b"hello"


# ── ${VAR} resolution ────────────────────────────────────────────────


def test_resolve_env_var(monkeypatch):
    """${VAR} is replaced with the env var value."""
    monkeypatch.setenv("MY_VAR", "resolved_value")
    assert resolve_env("prefix_${MY_VAR}_suffix") == "prefix_resolved_value_suffix"


def test_resolve_multiple_env_vars(monkeypatch):
    """Multiple ${VAR} references are all resolved."""
    monkeypatch.setenv("A", "alpha")
    monkeypatch.setenv("B", "beta")
    assert resolve_env("${A} and ${B}") == "alpha and beta"


def test_resolve_full_string_is_var(monkeypatch):
    """When entire string is a var reference, it resolves."""
    monkeypatch.setenv("X", "full_value")
    assert resolve_env("${X}") == "full_value"


def test_resolve_missing_env_var_raises_keyerror(monkeypatch):
    """Missing env var without default raises KeyError."""
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(KeyError, match="Environment variable 'MISSING_VAR' is not set"):
        resolve_env("hello ${MISSING_VAR} world")


def test_resolve_env_var_prefix_suffix_no_var(monkeypatch):
    """String with $ but no valid ${} pattern passes through."""
    # $ without braces is not a valid pattern
    assert resolve_env("$HOME") == "$HOME"


# ── ${VAR:-default} resolution ───────────────────────────────────────


def test_resolve_with_default_when_var_set(monkeypatch):
    """Default is ignored when var is set."""
    monkeypatch.setenv("PORT", "8080")
    assert resolve_env("${PORT:-3000}") == "8080"


def test_resolve_with_default_when_var_missing(monkeypatch):
    """Default is used when var is not set."""
    monkeypatch.delenv("PORT", raising=False)
    assert resolve_env("${PORT:-3000}") == "3000"


def test_resolve_with_empty_default(monkeypatch):
    """Empty string default is valid."""
    monkeypatch.delenv("X", raising=False)
    assert resolve_env("${X:-}") == ""


def test_resolve_with_default_containing_special_chars(monkeypatch):
    """Default value can contain special chars."""
    monkeypatch.delenv("URL", raising=False)
    assert resolve_env("${URL:-https://example.com/path?q=1}") == "https://example.com/path?q=1"


def test_resolve_with_default_mid_string(monkeypatch):
    """Default syntax works when mid-string."""
    monkeypatch.delenv("NAME", raising=False)
    result = resolve_env("Hello ${NAME:-World}!")
    assert result == "Hello World!"


def test_resolve_env_var_empty_string_value(monkeypatch):
    """Env var set to empty string is treated as set (not missing)."""
    monkeypatch.setenv("EMPTY_VAR", "")
    assert resolve_env("${EMPTY_VAR:-fallback}") == ""


# ── Nested structures ────────────────────────────────────────────────


def test_resolve_dict(monkeypatch):
    """resolve_env recurses into dict values."""
    monkeypatch.setenv("KEY", "secret")
    result = resolve_env({
        "api_key": "${KEY}",
        "timeout": 30,
        "nested": {"url": "${KEY:-default}"},
    })
    assert result == {
        "api_key": "secret",
        "timeout": 30,
        "nested": {"url": "secret"},
    }


def test_resolve_list(monkeypatch):
    """resolve_env recurses into list items."""
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    result = resolve_env(["${A}", "${B}", "static", 42])
    assert result == ["1", "2", "static", 42]


def test_resolve_nested_mixed(monkeypatch):
    """resolve_env handles deeply nested mixed structures."""
    monkeypatch.setenv("X", "val")
    data = {
        "servers": [
            {"host": "${X}", "ports": ["${X:-80}", 443]},
            {"host": "${X:-backup}", "ports": [8080]},
        ]
    }
    result = resolve_env(data)
    assert result == {
        "servers": [
            {"host": "val", "ports": ["val", 443]},
            {"host": "val", "ports": [8080]},
        ]
    }


def test_resolve_empty_dict():
    """Empty dict passes through."""
    assert resolve_env({}) == {}


def test_resolve_empty_list():
    """Empty list passes through."""
    assert resolve_env([]) == []


# ── Edge cases ───────────────────────────────────────────────────────


def test_resolve_unclosed_brace():
    """Unclosed ${ passes through as literal text."""
    assert resolve_env("${UNCLOSED") == "${UNCLOSED"


def test_resolve_dollar_without_brace():
    """$ sign without { passes through."""
    assert resolve_env("Cost: $50.00") == "Cost: $50.00"


def test_resolve_double_braces(monkeypatch):
    """Pattern with }} in string content — just a single var."""
    monkeypatch.setenv("VAR", "x")
    # The pattern is ${VAR} followed by literal }
    assert resolve_env("${VAR}}") == "x}"


def test_resolve_var_with_underscores_and_digits(monkeypatch):
    """Environment variable names can contain underscores and digits."""
    monkeypatch.setenv("MY_VAR_2", "ok")
    assert resolve_env("${MY_VAR_2}") == "ok"


# ── Pattern boundary tests ───────────────────────────────────────────


def test_resolve_var_at_start(monkeypatch):
    """Var at the start of string."""
    monkeypatch.setenv("PREFIX", ">>>")
    assert resolve_env("${PREFIX} content") == ">>> content"


def test_resolve_var_at_end(monkeypatch):
    """Var at the end of string."""
    monkeypatch.setenv("SUFFIX", "<<<")
    assert resolve_env("content ${SUFFIX}") == "content <<<"


def test_resolve_adjacent_vars(monkeypatch):
    """Two vars adjacent to each other."""
    monkeypatch.setenv("FIRST", "hello")
    monkeypatch.setenv("SECOND", "world")
    assert resolve_env("${FIRST}${SECOND}") == "helloworld"


def test_resolve_var_with_colon_but_no_default(monkeypatch):
    """Colon without dash is NOT a valid default separator — passes through."""
    # The pattern requires ${VAR} or ${VAR:-default}. ${FOO:BAR} doesn't match
    # either (colon without dash), so it passes through as literal text.
    monkeypatch.setenv("FOO:BAR", "value")
    assert resolve_env("${FOO:BAR}") == "${FOO:BAR}"
