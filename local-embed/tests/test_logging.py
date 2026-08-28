"""Tests for local_embed.logging — log-dir resolution and file setup.

The external-plugin log contract: when slife spawns local-embed it exports
``SLIFE_LOG_DIR`` / ``SLIFE_AGENT_NAME`` / ``SLIFE_PLUGIN_NAME``, so the
per-session log file follows slife's naming ({ts}_{agent}_{service}.log)
in slife's log directory.  Standalone falls back to ~/.local-embed/logs.
"""

import logging

import pytest

pytestmark = pytest.mark.unit

from local_embed import logging as le_logging


class TestResolveLogDir:
    def test_slife_log_dir_env_wins(self, monkeypatch):
        """When slife spawns us, logs land in slife's log directory."""
        monkeypatch.setenv("SLIFE_LOG_DIR", "C:\\slife\\logs")
        assert le_logging.resolve_log_dir().as_posix() == "C:/slife/logs"

    def test_standalone_default_under_home(self, monkeypatch):
        """No SLIFE_LOG_DIR → ~/.local-embed/logs (standalone)."""
        monkeypatch.delenv("SLIFE_LOG_DIR", raising=False)
        result = le_logging.resolve_log_dir()
        assert result.name == "logs"
        assert result.parent.name == ".local-embed"


class TestSetupLogging:
    def _restore(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        root.setLevel(logging.WARNING)

    def test_creates_slife_named_file(self, monkeypatch, tmp_path):
        """A file handler lands at {ts}_{agent}_{service}.log under
        SLIFE_LOG_DIR, using SLIFE_AGENT_NAME / SLIFE_PLUGIN_NAME."""
        monkeypatch.setenv("SLIFE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("SLIFE_AGENT_NAME", "slife")
        monkeypatch.setenv("SLIFE_PLUGIN_NAME", "local-embed")
        try:
            le_logging.setup_logging(service_name="local-embed")
            handlers = [
                h for h in logging.getLogger().handlers
                if isinstance(h, logging.FileHandler)
            ]
            assert len(handlers) == 1
            name = handlers[0].baseFilename.replace("\\", "/").split("/")[-1]
            assert name.endswith("_slife_local-embed.log")
            assert name.startswith("20")
        finally:
            self._restore()

    def test_stderr_handler_kept(self, monkeypatch, tmp_path):
        """The stderr stream handler must remain (the host relays it)."""
        monkeypatch.setenv("SLIFE_LOG_DIR", str(tmp_path))
        try:
            le_logging.setup_logging(service_name="local-embed")
            stream = [
                h for h in logging.getLogger().handlers
                if isinstance(h, logging.StreamHandler)
            ]
            assert len(stream) >= 1
        finally:
            self._restore()

    def test_idempotent(self, monkeypatch, tmp_path):
        """Repeated calls replace handlers rather than stacking files."""
        monkeypatch.setenv("SLIFE_LOG_DIR", str(tmp_path))
        try:
            le_logging.setup_logging(service_name="local-embed")
            le_logging.setup_logging(service_name="local-embed")
            files = [h.baseFilename for h in logging.getLogger().handlers
                     if isinstance(h, logging.FileHandler)]
            assert len(files) == 1
        finally:
            self._restore()
