"""Tests for keyring: URI resolution."""

import pytest

pytestmark = pytest.mark.unit

from credstore._resolver import (
    is_keyring_uri,
    parse_keyring_uri,
    resolve_uri,
)


class TestIsKeyringUri:
    @pytest.mark.parametrize("uri", [
        "keyring:slife/deepseek",
        "keyring:myapp/api_key",
        "keyring:svc/nested/key/path",
    ])
    def test_valid_uri(self, uri):
        assert is_keyring_uri(uri)

    @pytest.mark.parametrize("uri", [
        "sk-plaintext-key",
        "${DEEPSEEK_API_KEY}",
        "<YOUR_KEY>",
        "",
    ])
    def test_invalid_uri(self, uri):
        assert not is_keyring_uri(uri)

    @pytest.mark.parametrize("value", [None, 42])
    def test_non_string_returns_false(self, value):
        assert not is_keyring_uri(value)  # type: ignore[arg-type]


class TestParseKeyringUri:
    def test_parse_simple(self):
        assert parse_keyring_uri("keyring:slife/deepseek") == ("slife", "deepseek")

    def test_parse_nested_key(self):
        assert parse_keyring_uri("keyring:svc/provider/deepseek") == ("svc", "provider/deepseek")

    @pytest.mark.parametrize("value", ["not-a-uri", "", None])
    def test_parse_invalid_returns_none(self, value):
        assert parse_keyring_uri(value) is None  # type: ignore[arg-type]


@pytest.mark.usefixtures("cli_store")
class TestResolveUri:
    def test_keyring_uri_resolves(self, in_mem_store):
        in_mem_store["slife/deepseek"] = "sk-test-key"
        assert resolve_uri("keyring:slife/deepseek") == "sk-test-key"

    def test_non_uri_passes_through(self):
        assert resolve_uri("sk-plaintext-key") == "sk-plaintext-key"

    def test_env_var_passes_through(self):
        assert resolve_uri("${DEEPSEEK_API_KEY}") == "${DEEPSEEK_API_KEY}"

    def test_not_found_raises_keyerror(self):
        with pytest.raises(KeyError, match="not-found-key"):
            resolve_uri("keyring:slife/not-found-key")

    def test_non_string_passes_through(self):
        assert resolve_uri(42) == 42  # type: ignore[arg-type]
