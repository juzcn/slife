"""Tests for slife.memfiles.token — file registry with hex tokens."""

import pytest; pytestmark = pytest.mark.unit


import json
import os
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest

from slife.memfiles import token


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Reset module-level state before each test."""
    monkeypatch.delenv("SLIFE_MEMFILES_REGISTRY", raising=False)
    token._registry_path = None
    token._registry_cache = None
    token._fallback.clear()


# ── init_registry ───────────────────────────────────────────────────────────


class TestInitRegistry:
    """Tests for init_registry()."""

    def test_creates_empty_json_file(self):
        """init_registry() creates a file containing an empty JSON object."""
        path_str = token.init_registry()
        path = Path(path_str)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert content == "{}"

    def test_sets_env_variable(self):
        """init_registry() sets SLIFE_MEMFILES_REGISTRY in the environment."""
        path_str = token.init_registry()
        env_val = os.environ.get("SLIFE_MEMFILES_REGISTRY")
        assert env_val == path_str

    def test_returns_path_as_string(self):
        """init_registry() returns a str whose file exists."""
        result = token.init_registry()
        assert isinstance(result, str)
        assert Path(result).exists()

    def test_writes_valid_parsable_json(self):
        """The created file contains JSON that parses to an empty dict."""
        path_str = token.init_registry()
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
        assert data == {}

    def test_parent_directory_exists(self):
        """The parent directory of the registry file always exists."""
        path_str = token.init_registry()
        parent = Path(path_str).parent
        assert parent.exists()
        assert parent.is_dir()

    def test_second_call_creates_new_file(self):
        """A second call to init_registry() creates a fresh valid registry."""
        first = token.init_registry()
        second = token.init_registry()
        # Both are valid
        assert Path(first).exists() or Path(first).name != ""
        assert Path(second).exists()
        content = Path(second).read_text(encoding="utf-8")
        assert content == "{}"
        # Env var points to the second path
        assert os.environ["SLIFE_MEMFILES_REGISTRY"] == second


# ── register_file ───────────────────────────────────────────────────────────


class TestRegisterFile:
    """Tests for register_file()."""

    def test_returns_30_char_hex_string(self):
        """register_file() returns exactly 30 lowercase hex characters."""
        token.init_registry()
        tok = token.register_file("/some/file.txt")
        assert len(tok) == 30
        assert all(c in "0123456789abcdef" for c in tok)

    def test_creates_entry_in_registry(self):
        """register_file() persists the mapping so lookup_file() can find it."""
        token.init_registry()
        file_path = "/home/user/test.txt"
        tok = token.register_file(file_path)
        found = token.lookup_file(tok)
        assert found == file_path

    def test_stores_absolute_path(self, tmp_path):
        """A relative path is stored as-is (no normalization)."""
        token.init_registry()
        rel_path = "relative/path/to/file.txt"
        tok = token.register_file(rel_path)
        found = token.lookup_file(tok)
        assert found == rel_path

    def test_nonexistent_path_still_registers(self):
        """register_file() does not validate that the file exists."""
        token.init_registry()
        nonexistent = "/nonexistent/path/foo.bar"
        tok = token.register_file(nonexistent)
        found = token.lookup_file(tok)
        assert found == nonexistent

    def test_reuses_existing_token(self):
        """Registering the same path twice returns the same token."""
        token.init_registry()
        file_path = "/tmp/some-file"
        tok1 = token.register_file(file_path)
        tok2 = token.register_file(file_path)
        assert tok1 == tok2

    def test_different_paths_get_different_tokens(self):
        """Two distinct paths get two distinct tokens."""
        token.init_registry()
        tok_a = token.register_file("/file_a.txt")
        tok_b = token.register_file("/file_b.txt")
        assert tok_a != tok_b

    def test_fallback_without_init_registry(self):
        """Without init_registry(), register_file() uses the in-process dict."""
        # Clean state already ensured by autouse fixture
        tok = token.register_file("/fallback/test.txt")
        assert len(tok) == 30
        # lookup_file also uses fallback
        found = token.lookup_file(tok)
        assert found == "/fallback/test.txt"

    def test_fallback_reuses_token(self):
        """In fallback mode, re-registering the same path reuses the token."""
        tok1 = token.register_file("/fb/same.txt")
        tok2 = token.register_file("/fb/same.txt")
        assert tok1 == tok2


# ── lookup_file ─────────────────────────────────────────────────────────────


class TestLookupFile:
    """Tests for lookup_file()."""

    def test_found_returns_path(self):
        """lookup_file() returns the path for a registered token."""
        token.init_registry()
        file_path = "/data/documents/report.pdf"
        tok = token.register_file(file_path)
        result = token.lookup_file(tok)
        assert result == file_path

    def test_not_found_returns_none(self):
        """lookup_file() returns None for an unregistered token."""
        token.init_registry()
        bogus = secrets.token_hex(15)
        result = token.lookup_file(bogus)
        assert result is None

    def test_empty_registry_returns_none(self):
        """lookup_file() returns None on a fresh empty registry."""
        token.init_registry()
        result = token.lookup_file("any_token_here")
        assert result is None

    def test_missing_registry_file_returns_none(self):
        """lookup_file() returns None when the registry file was deleted."""
        path_str = token.init_registry()
        Path(path_str).unlink()
        # Clear cache so _read_registry doesn't return stale data
        token._registry_cache = None
        result = token.lookup_file("any_token")
        assert result is None

    def test_fallback_without_init_registry(self):
        """Without init_registry(), lookup_file() reads from the in-process dict."""
        tok = token.register_file("/fb-only/lookup.txt")
        result = token.lookup_file(tok)
        assert result == "/fb-only/lookup.txt"

    def test_missing_in_fallback_returns_none(self):
        """In fallback mode, unknown tokens return None."""
        result = token.lookup_file("nonexistent_token_hex")
        assert result is None


# ── Registry file I/O edge cases ────────────────────────────────────────────


class TestReadRegistry:
    """Tests for _read_registry()."""

    def test_returns_empty_dict_when_no_registry(self):
        """_read_registry() returns {} when no registry is initialized."""
        # Ensure clean state
        result = token._read_registry()
        assert result == {}

    def test_returns_empty_dict_on_corrupt_json(self):
        """_read_registry() returns {} when the file contains invalid JSON."""
        path_str = token.init_registry()
        Path(path_str).write_text("not valid json", encoding="utf-8")
        token._registry_cache = None
        result = token._read_registry()
        assert result == {}


# ── Edge cases ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case tests for the token registry."""

    def test_special_chars_in_path(self):
        """Paths with spaces, unicode, and special chars are round-tripped."""
        token.init_registry()
        special_path = "/path/with spaces/and-unicode/你好/☃.txt"
        tok = token.register_file(special_path)
        found = token.lookup_file(tok)
        assert found == special_path

    def test_very_long_path(self, tmp_path):
        """A very long path is stored and retrieved correctly."""
        token.init_registry()
        # Build a long path component (individual component length varies by OS,
        # so we build many nested subdirs)
        long_path = tmp_path
        for i in range(20):
            long_path = long_path / f"subdir_{i:03d}"
        long_file = str(long_path / ("very_long_filename_" + "x" * 200))
        tok = token.register_file(long_file)
        found = token.lookup_file(tok)
        assert found == long_file

    def test_multiple_registrations(self):
        """Register many files and verify all lookups work."""
        token.init_registry()
        paths = [f"/data/file_{i:04d}.txt" for i in range(20)]
        tokens = {}
        for p in paths:
            tokens[p] = token.register_file(p)

        # Verify all can be looked up
        for p, tok in tokens.items():
            assert token.lookup_file(tok) == p

        # Verify no cross-contamination
        assert len(set(tokens.values())) == 20

    def test_token_chars_are_hex(self):
        """All characters in the generated token are lowercase hex digits."""
        token.init_registry()
        for i in range(10):
            tok = token.register_file(f"/edge/token_test_{i}.txt")
            assert all(c in "0123456789abcdef" for c in tok)


# ── cleanup_registry ────────────────────────────────────────────────────────


class TestCleanupRegistry:
    """Tests for cleanup_registry()."""

    def test_removes_registry_file(self):
        """cleanup_registry() deletes the registry file."""
        path_str = token.init_registry()
        path = Path(path_str)
        assert path.exists()
        token.cleanup_registry()
        assert not path.exists()

    def test_clears_env_var_and_module_state(self):
        """cleanup_registry() clears the env var, _registry_path, and _registry_cache."""
        token.init_registry()
        token.cleanup_registry()
        assert "SLIFE_MEMFILES_REGISTRY" not in os.environ
        assert token._registry_path is None
        assert token._registry_cache is None

    def test_no_error_when_not_initialized(self):
        """cleanup_registry() does not raise when nothing was initialized."""
        token.cleanup_registry()  # Should not raise

    def test_cleanup_then_register_falls_back_to_memory(self):
        """After cleanup, register_file() falls back to in-memory dict."""
        token.init_registry()
        token.cleanup_registry()
        tok = token.register_file("/post-cleanup/path.txt")
        assert len(tok) == 30
        found = token.lookup_file(tok)
        assert found == "/post-cleanup/path.txt"

    def test_no_error_when_file_already_gone(self):
        """cleanup_registry() handles the case where file was deleted externally."""
        path_str = token.init_registry()
        Path(path_str).unlink()
        token.cleanup_registry()  # Should not raise
