"""Tests for local_embed.cmd_set — the ``local-embed set`` subcommand.

Covers cache resolution (flag > env > error), the missing-model hard error,
the idempotent config mutation, the canonical JSON5 write, and the
end-to-end CLI wiring against a temp config path.
"""

import pytest

pytestmark = pytest.mark.unit

from local_embed.cli import main

import local_embed.cli as cli
from local_embed.cmd_set import (
    ENV_CACHE_KEY,
    ENV_OFFLINE_KEY,
    backend_install_hint,
    model_in_cache,
    resolve_cache,
    set_gguf_model,
    set_transformer_model,
)
from local_embed.config import load_config, write_config


class TestResolveCache:
    def test_flag_wins_over_env(self):
        assert resolve_cache("C:\\flag", {ENV_CACHE_KEY: "C:\\env"}) == "C:\\flag"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv(ENV_CACHE_KEY, "C:\\env")
        assert resolve_cache(None) == "C:\\env"

    def test_missing_raises(self):
        with pytest.raises(ValueError, match="not set"):
            resolve_cache(None, {})


class TestSetTransformerModel:
    def test_minimal_fresh_config(self):
        out = set_transformer_model({}, "BAAI/bge-m3", "C:\\hub", 8000)
        assert out == {
            "models": {"BAAI/bge-m3": {"backend": "transformer", "model": "BAAI/bge-m3"}},
            "active_model": "BAAI/bge-m3",
            "port": 8000,
            "env": {ENV_CACHE_KEY: "C:\\hub", ENV_OFFLINE_KEY: "1"},
        }

    def test_offline_flag_defaults_on(self):
        # the server should never silently refetch a missing repo — pin
        # HF_HUB_OFFLINE even when the user only supplied a cache dir
        out = set_transformer_model({}, "BAAI/bge-m3", "C:\\hub", 8000)
        assert out["env"][ENV_OFFLINE_KEY] == "1"

    def test_preserves_existing_models_and_env(self):
        cfg = {
            "models": {"old": {"backend": "gguf", "gguf_path": "/a.gguf"}},
            "active_model": "old",
            "env": {"HF_HUB_OFFLINE": "1"},
        }
        out = set_transformer_model(cfg, "BAAI/bge-m3", "C:\\hub", 8123)
        assert out["models"]["old"] == cfg["models"]["old"]
        assert out["active_model"] == "BAAI/bge-m3"
        assert out["env"] == {"HF_HUB_OFFLINE": "1", ENV_CACHE_KEY: "C:\\hub"}
        assert out["port"] == 8123

    def test_idempotent(self):
        once = set_transformer_model({}, "BAAI/bge-m3", "C:\\hub", 8000)
        twice = set_transformer_model(once, "BAAI/bge-m3", "C:\\hub", 8000)
        assert twice == once


class TestSetGgufModel:
    def test_fresh_config(self):
        out = set_gguf_model({}, "bge-m3", "D:\\m\\bge.gguf", 8000)
        assert out == {
            "models": {"bge-m3": {"backend": "gguf", "gguf_path": "D:\\m\\bge.gguf"}},
            "active_model": "bge-m3",
            "port": 8000,
        }

    def test_preserves_existing_models_and_env(self):
        cfg = {
            "models": {"old": {"backend": "transformer", "model": "BAAI/bge-m3"}},
            "active_model": "old",
            "env": {"HF_HUB_CACHE": "C:\\hub"},
            "port": 8001,
        }
        out = set_gguf_model(cfg, "bge-m3", "/a.gguf", 8000)
        assert out["models"]["old"] == cfg["models"]["old"]
        assert out["env"] == cfg["env"]
        assert out["active_model"] == "bge-m3"
        assert out["port"] == 8000

    def test_idempotent(self):
        once = set_gguf_model({}, "bge-m3", "/a.gguf", 8000)
        assert set_gguf_model(once, "bge-m3", "/a.gguf", 8000) == once


class TestModelInCache:
    def test_snapshot_layout(self, tmp_path):
        (tmp_path / "models--BAAI--bge-m3").mkdir()
        assert model_in_cache(str(tmp_path), "BAAI/bge-m3")

    def test_legacy_layout(self, tmp_path):
        (tmp_path / "BAAI" / "bge-m3").mkdir(parents=True)
        assert model_in_cache(str(tmp_path), "BAAI/bge-m3")

    def test_missing(self, tmp_path):
        assert not model_in_cache(str(tmp_path), "BAAI/bge-m3")


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "local_embed.json5"
    monkeypatch.setenv("LOCAL_EMBED_FILE", str(path))
    return path


class TestBackendInstallHint:
    def test_gguf_linux_macos_plain(self):
        # The backend lands in the venv that runs local-embed (uv pip
        # install --python <this venv>), never a fresh `uv tool install`
        # (that rebuilds a separate standalone tool).
        assert backend_install_hint("gguf", "linux", "/venv/python") == (
            "uv pip install --python /venv/python llama-cpp-python==0.3.34"
        )
        assert backend_install_hint("gguf", "darwin", "/venv/python") == (
            "uv pip install --python /venv/python llama-cpp-python==0.3.34"
        )

    def test_gguf_windows_adds_cpu_index(self):
        assert backend_install_hint("gguf", "win32", "/venv/python") == (
            "uv pip install --python /venv/python "
            "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu "
            "llama-cpp-python==0.3.34"
        )

    def test_transformer_plain(self):
        assert backend_install_hint("transformer", "win32", "/venv/python") == (
            "uv pip install --python /venv/python sentence-transformers"
        )


class TestRunSet:
    def _populated_cache(self, tmp_path):
        cache = tmp_path / "hub"
        (cache / "models--BAAI--bge-m3").mkdir(parents=True)
        return cache

    def test_writes_config_and_exits_zero(self, tmp_path, config_path, capsys):
        cache = self._populated_cache(tmp_path)
        code = main(["set", "BAAI/bge-m3", "--HF_HUB_CACHE", str(cache), "--port", "8123"])
        assert code == 0
        cfg = load_config(config_path)
        assert cfg["active_model"] == "BAAI/bge-m3"
        assert cfg["port"] == 8123
        assert cfg["env"][ENV_CACHE_KEY] == str(cache)
        assert cfg["models"]["BAAI/bge-m3"] == {"backend": "transformer", "model": "BAAI/bge-m3"}
        assert "BAAI/bge-m3" in capsys.readouterr().out

    def test_cache_env_fallback(self, tmp_path, config_path, monkeypatch):
        cache = self._populated_cache(tmp_path)
        monkeypatch.setenv(ENV_CACHE_KEY, str(cache))
        code = main(["set", "BAAI/bge-m3"])
        assert code == 0
        cfg = load_config(config_path)
        assert cfg["env"][ENV_CACHE_KEY] == str(cache)

    def test_missing_cache_arg_is_error(self, config_path, monkeypatch, capsys):
        monkeypatch.delenv(ENV_CACHE_KEY, raising=False)
        code = main(["set", "BAAI/bge-m3"])
        assert code == 2
        assert not config_path.exists()
        assert "not set" in capsys.readouterr().err

    def test_model_not_in_cache_is_error(self, tmp_path, config_path, capsys):
        cache = tmp_path / "hub"
        cache.mkdir()  # exists but holds no weights
        code = main(["set", "BAAI/bge-m3", "--HF_HUB_CACHE", str(cache)])
        assert code == 2
        assert not config_path.exists()
        err = capsys.readouterr().err
        assert "not found in HF cache" in err

    def test_idempotent_across_runs(self, tmp_path, config_path):
        cache = self._populated_cache(tmp_path)
        assert main(["set", "BAAI/bge-m3", "--HF_HUB_CACHE", str(cache)]) == 0
        first = config_path.read_text(encoding="utf-8")
        assert main(["set", "BAAI/bge-m3", "--HF_HUB_CACHE", str(cache)]) == 0
        assert config_path.read_text(encoding="utf-8") == first


class TestRunSetGguf:
    def _gguf_file(self, tmp_path):
        gguf = tmp_path / "bge.gguf"
        gguf.write_text("GGUF", encoding="utf-8")
        return gguf

    def test_writes_config_and_exits_zero(self, tmp_path, config_path, capsys):
        gguf = self._gguf_file(tmp_path)
        code = main(["set-gguf", "bge-m3", "--path", str(gguf), "--port", "8123"])
        assert code == 0
        cfg = load_config(config_path)
        assert cfg["active_model"] == "bge-m3"
        assert cfg["port"] == 8123
        assert cfg["models"]["bge-m3"] == {"backend": "gguf", "gguf_path": str(gguf)}
        assert "bge-m3" in capsys.readouterr().out

    def test_missing_file_is_error(self, tmp_path, config_path, capsys):
        code = main(["set-gguf", "bge-m3", "--path", str(tmp_path / "nope.gguf")])
        assert code == 2
        assert not config_path.exists()
        assert "not found" in capsys.readouterr().err

    def test_missing_path_arg_errors(self, config_path):
        with pytest.raises(SystemExit):
            main(["set-gguf", "bge-m3"])

    def test_idempotent_across_runs(self, tmp_path, config_path):
        gguf = self._gguf_file(tmp_path)
        assert main(["set-gguf", "bge-m3", "--path", str(gguf)]) == 0
        first = config_path.read_text(encoding="utf-8")
        assert main(["set-gguf", "bge-m3", "--path", str(gguf)]) == 0
        assert config_path.read_text(encoding="utf-8") == first


class TestCliCtrlC:
    """Ctrl-C during the active backend check must exit 130, never traceback.

    Regression: the startup validation calls ``resolve_backend_runtime``
    (a REAL import — torch takes seconds), and a Ctrl-C landing mid-import
    used to propagate as a raw KeyboardInterrupt traceback.
    """

    def _write_active_transformer(self, config_path):
        write_config(
            {
                "active_model": "bge-m3-transformer",
                "models": {
                    "bge-m3-transformer": {
                        "backend": "transformer",
                        "model": "BAAI/bge-m3",
                    }
                },
            },
            config_path,
        )

    def test_interrupt_during_active_check_exits_130(self, monkeypatch, config_path, capsys):
        self._write_active_transformer(config_path)

        def interrupt(_backend):
            raise KeyboardInterrupt()

        monkeypatch.setattr(cli, "resolve_backend_runtime", interrupt)
        code = cli.main([])
        assert code == 130
        assert "Interrupted" in capsys.readouterr().err

    def test_non_active_backend_not_resolved(self, monkeypatch, config_path):
        # active = gguf; the non-active transformer must NOT trigger the
        # torch import at startup (it is only checked when actually loaded).
        write_config(
            {
                "active_model": "bge-m3",
                "models": {
                    "bge-m3": {"backend": "gguf", "gguf_path": "/x.gguf"},
                    "bge-m3-transformer": {
                        "backend": "transformer",
                        "model": "BAAI/bge-m3",
                    },
                },
            },
            config_path,
        )
        resolved = []

        def tracking(backend):
            resolved.append(backend)
            return True

        monkeypatch.setattr(cli, "resolve_backend_runtime", tracking)
        # stop after the validation loop instead of serving (serve_standalone
        # is imported lazily inside main, so patch its source module)
        monkeypatch.setattr(
            "local_embed.server.serve_standalone", lambda *a, **k: 0
        )
        code = cli.main([])
        assert code == 0
        assert resolved == ["gguf"]  # only the active backend was imported