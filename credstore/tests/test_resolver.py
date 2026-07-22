"""Tests for keyring: URI resolution."""

import pytest
from credstore._resolver import (
    is_keyring_uri,
    parse_keyring_uri,
    resolve_uri,
    resolve_uri_recursive,
)


class TestIsKeyringUri:
    def test_valid_uri(self):
        assert is_keyring_uri("keyring:slife/deepseek")
        assert is_keyring_uri("keyring:myapp/api_key")
        assert is_keyring_uri("keyring:svc/nested/key/path")

    def test_invalid_uri(self):
        assert not is_keyring_uri("sk-plaintext-key")
        assert not is_keyring_uri("${DEEPSEEK_API_KEY}")
        assert not is_keyring_uri("<YOUR_KEY>")
        assert not is_keyring_uri("")

    def test_non_string_returns_false(self):
        assert not is_keyring_uri(None)  # type: ignore[arg-type]
        assert not is_keyring_uri(42)  # type: ignore[arg-type]


class TestParseKeyringUri:
    def test_parse_simple(self):
        assert parse_keyring_uri("keyring:slife/deepseek") == ("slife", "deepseek")

    def test_parse_nested_key(self):
        assert parse_keyring_uri("keyring:svc/provider/deepseek") == ("svc", "provider/deepseek")

    def test_parse_invalid_returns_none(self):
        assert parse_keyring_uri("not-a-uri") is None
        assert parse_keyring_uri("") is None

    def test_parse_non_string_returns_none(self):
        assert parse_keyring_uri(None) is None  # type: ignore[arg-type]


class TestResolveUri:
    def test_keyring_uri_resolves(self, mock_credstore):
        mock_credstore["slife/deepseek"] = "sk-test-key"
        assert resolve_uri("keyring:slife/deepseek") == "sk-test-key"

    def test_non_uri_passes_through(self, mock_credstore):
        assert resolve_uri("sk-plaintext-key") == "sk-plaintext-key"

    def test_env_var_passes_through(self, mock_credstore):
        assert resolve_uri("${DEEPSEEK_API_KEY}") == "${DEEPSEEK_API_KEY}"

    def test_not_found_raises_keyerror(self, mock_credstore):
        with pytest.raises(KeyError, match="not-found-key"):
            resolve_uri("keyring:slife/not-found-key")

    def test_non_string_passes_through(self, mock_credstore):
        assert resolve_uri(42) == 42  # type: ignore[arg-type]


class TestResolveUriRecursive:
    def test_dict(self, mock_credstore):
        mock_credstore["slife/deepseek"] = "sk-key"
        result = resolve_uri_recursive({
            "api_key": "keyring:slife/deepseek",
            "name": "plaintext",
        })
        assert result == {"api_key": "sk-key", "name": "plaintext"}

    def test_list(self, mock_credstore):
        mock_credstore["slife/a"] = "resolved"
        assert resolve_uri_recursive(["keyring:slife/a", "plain"]) == ["resolved", "plain"]

    def test_nested(self, mock_credstore):
        mock_credstore["slife/k"] = "v"
        assert resolve_uri_recursive({"outer": {"inner": "keyring:slife/k"}}) == {"outer": {"inner": "v"}}

    def test_scalar_passes_through(self, mock_credstore):
        assert resolve_uri_recursive(42) == 42


# ── fixtures ───────────────────────────────────────────────────


@pytest.fixture
def mock_credstore(monkeypatch):
    """Mock credstore._store to use an in-memory dict."""
    data = {}

    import credstore._store as sm
    monkeypatch.setattr(sm, "init_store", lambda **kw: None)

    store = sm.CredentialStore()
    # Override methods with in-memory versions (simulates dual-write backing)
    # For resolver tests, we only need get() and set() to work
    store.get = data.get  # type: ignore[assignment]
    store.set = data.__setitem__  # type: ignore[assignment]
    store.delete = lambda key: data.pop(key, None) is not None

    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)
    monkeypatch.setattr(sm, "get_credential", data.get)
    monkeypatch.setattr(sm, "set_credential", data.__setitem__)
    monkeypatch.setattr(sm, "delete_credential", lambda key: data.pop(key, None) is not None)

    return data
