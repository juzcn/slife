"""Tests for the credstore CLI."""

import pytest
from credstore.__main__ import main


class TestCliStatus:
    def test_status_returns_zero(self, mock_backend):
        assert main(["status"]) == 0


class TestCliGet:
    def test_get_not_found(self, mock_backend, monkeypatch):
        # User skips cryptfile fallback (empty input)
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "")
        assert main(["get", "nonexistent/key"]) == 1

    def test_get_found_shows_masked(self, capsys, mock_backend, in_mem_store):
        in_mem_store["service/key"] = "sk-secret-test-value-long"
        assert main(["get", "service/key"]) == 0
        out = capsys.readouterr().out
        assert "sk-s…long" in out
        assert "sk-secret-test-value-long" not in out


class TestCliDelete:
    def test_delete_not_found(self, mock_backend, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "test-master-pw")
        assert main(["delete", "nonexistent"]) == 1

    def test_delete_found(self, mock_backend, in_mem_store, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "test-master-pw")
        in_mem_store["test/key"] = "secret"
        assert main(["delete", "test/key"]) == 0
        assert "test/key" not in in_mem_store


class TestCliSet:
    def test_set_empty_secret(self, mock_backend, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "")
        assert main(["set", "test/key"]) == 1


# ── fixtures ───────────────────────────────────────────────────


@pytest.fixture
def in_mem_store():
    """In-memory dict shared between mock get/set/delete."""
    return {}


@pytest.fixture
def mock_backend(monkeypatch, in_mem_store):
    """Mock credstore backend — no real keyring access."""
    import credstore._backend as backend
    monkeypatch.setattr(backend, "init_backend", lambda **kw: None)

    # Mock the store
    import credstore._store as sm
    monkeypatch.setattr(sm, "init_store", lambda **kw: None)
    store = sm.CredentialStore()
    store.get = in_mem_store.get
    store.set = in_mem_store.__setitem__
    store.delete = lambda key: in_mem_store.pop(key, None) is not None
    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)
    monkeypatch.setattr(sm, "get_credential", in_mem_store.get)
    monkeypatch.setattr(sm, "set_credential", in_mem_store.__setitem__)
    monkeypatch.setattr(sm, "delete_credential", lambda k: in_mem_store.pop(k, None) is not None)

    # Gate: cryptfile must appear ready for commands to work
    monkeypatch.setattr(backend, "has_master_key", lambda: True)

    # Mock backend info for status command
    monkeypatch.setattr(backend, "get_active_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": True,
    })
    monkeypatch.setattr(sm, "get_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": True,
    })
