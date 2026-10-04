"""Tests for Slife main entry point — mock heavy to avoid Textual init."""

import pytest; pytestmark = [pytest.mark.integration, pytest.mark.slow]


import signal
import pytest
from unittest.mock import MagicMock, call, patch


class TestMainFunction:
    """Tests for Slife.main() — fully mocked."""

    @pytest.fixture
    def mock_config(self):
        from slife.config import Config, ModelConfig
        mc = ModelConfig(
            ref="deepseek/deepseek-v4-flash",
            provider="deepseek",
            api_model="deepseek-v4-flash",
            display_name="DeepSeek V4 Flash",
            api_key="sk-test-key",
        )
        return Config(
            models=[mc],
            active_model_ref="deepseek/deepseek-v4-flash",
            tools=[],
        )

    def test_main_loads_config(self, mock_config):
        """main() loads config from the given path."""
        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                from slife import main
                main("test_config.yaml")

                mock_app.run.assert_called_once()

    def test_main_default_config_path(self, mock_config):
        """main() uses slife.yaml by default."""
        with patch("slife.Config.from_yaml", return_value=mock_config) as mock_from:
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                from slife import main
                main()

                mock_from.assert_called_once()
                mock_app_cls.assert_called_once()
                mock_app.run.assert_called_once()

    def test_main_notes_session_and_clears_it_on_exit(self, mock_config):
        """main() records the session at startup and forgets it in teardown.

        The pair is the whole mechanism: the marker only survives when the
        teardown never runs, which is what a hard kill looks like from the
        next start (bootstrap.previous_session_killed).
        """
        with patch("slife.bootstrap.previous_session_killed", return_value=None) as prev, \
             patch("slife.bootstrap.note_session_start") as note, \
             patch("slife.bootstrap.clear_session_marker") as clear, \
             patch("slife.Config.from_yaml", return_value=mock_config), \
             patch("slife.SlifeApp") as mock_app_cls:
            mock_app_cls.return_value = MagicMock()

            from slife import main
            main()

        prev.assert_called_once()
        note.assert_called_once()
        clear.assert_called_once()

    def test_main_reports_a_killed_previous_session(self, mock_config, capsys):
        """The user is told, on the terminal, that the last session was killed."""
        killed = "the last session (pid 4242) was killed from outside"
        with patch("slife.bootstrap.previous_session_killed", return_value=killed), \
             patch("slife.bootstrap.note_session_start"), \
             patch("slife.bootstrap.clear_session_marker"), \
             patch("slife.Config.from_yaml", return_value=mock_config), \
             patch("slife.SlifeApp") as mock_app_cls:
            mock_app_cls.return_value = MagicMock()

            from slife import main
            main()

        assert "killed from outside" in capsys.readouterr().err

    def test_main_creates_app_with_config(self, mock_config):
        """SlifeApp is created with the loaded config."""
        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                from slife import main
                main()

                mock_app_cls.assert_called_once_with(mock_config)

    def test_main_logs_model_info(self, mock_config):
        """The session log records model info before any host starts.

        Patched on ``slife.bootstrap`` because that is where the boot lives —
        both hosts share ``prepare_session``, so these lines belong to it and
        not to either entry point.
        """
        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                assert any("DeepSeek V4 Flash" in t for t in debug_texts)

    def test_main_thinking_off(self, mock_config):
        """Logs 'thinking: off' when thinking is disabled."""
        mock_config.models[0].thinking_enabled = False

        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                assert any("off" in t for t in debug_texts if "thinking" in t)

    def test_main_logs_tool_count(self, mock_config):
        """Logs the number of loaded tools."""
        mock_config.tools = [{"name": "execute_shell"}, {"name": "run_python_script"}]

        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                assert any("2" in t for t in debug_texts if "tools" in t)

    def test_main_masks_sigint_during_teardown(self, mock_config):
        """Teardown switches SIGINT to SIG_IGN before any cleanup work.

        A Ctrl+C landing during shutdown leaves a pending KeyboardInterrupt
        that CPython raises *during finalization* inside a weakref callback
        (Textual keeps DOMNodes/timers in ``WeakSet``s), printing
        "Exception ignored in: <function WeakSet._remove> KeyboardInterrupt"
        to the terminal at exit.  Masking the signal first makes the late
        interrupt a silent OS-level no-op instead.
        """
        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.logger"):
                    with patch("slife.signal.signal") as mock_signal:
                        from slife import main
                        main()

                        assert call(signal.SIGINT, signal.SIG_IGN) in (
                            mock_signal.call_args_list
                        )

    def test_main_masks_sigint_after_keyboard_interrupt(self, mock_config):
        """Ctrl+C during startup exits quietly and still masks SIGINT."""
        with patch("slife.Config.from_yaml", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock(
                    side_effect=KeyboardInterrupt
                )

                with patch("slife.logger"):
                    with patch("slife.signal.signal") as mock_signal:
                        from slife import main
                        main()  # must not propagate

                        assert call(signal.SIGINT, signal.SIG_IGN) in (
                            mock_signal.call_args_list
                        )


class TestHeadlessDispatch:
    """`--headless` runs the same agent through a host with no terminal.

    main() is the only place that knows which host it is starting, so this is
    where the branch is pinned: the headless host runs, the TUI is never
    built, and neither exit path skips the shared teardown.
    """

    @pytest.fixture
    def mock_config(self):
        from slife.config import Config, ModelConfig
        return Config(
            models=[ModelConfig(
                ref="test/test-model",
                provider="test",
                api_model="test-model",
                display_name="Test Model",
                api_key="sk-test",
            )],
            active_model_ref="test/test-model",
            tools=[],
        )

    def _main_with(self, argv, mock_config, run_headless):
        import sys

        with patch.object(sys, "argv", argv), \
             patch("slife.Config.from_yaml", return_value=mock_config), \
             patch("slife.bootstrap.previous_session_killed", return_value=None), \
             patch("slife.bootstrap.note_session_start"), \
             patch("slife.bootstrap.clear_session_marker"), \
             patch("slife.SlifeApp", side_effect=AssertionError("TUI must not start")), \
             patch("slife.headless.run_headless", run_headless):
            from slife import main
            main()

    def test_headless_runs_the_headless_host_not_the_tui(self, mock_config):
        run = MagicMock(return_value=(MagicMock(), ""))
        self._main_with(["slife", "--headless", "--agent", "jack"], mock_config, run)
        run.assert_called_once_with(mock_config)

    def test_without_the_flag_the_tui_starts(self, mock_config):
        """The other side of the branch — without `--headless` the app is
        built, so the flag is what selects the host and nothing else is."""
        import sys

        with patch.object(sys, "argv", ["slife"]), \
             patch("slife.Config.from_yaml", return_value=mock_config), \
             patch("slife.bootstrap.previous_session_killed", return_value=None), \
             patch("slife.bootstrap.note_session_start"), \
             patch("slife.bootstrap.clear_session_marker"), \
             patch("slife.headless.run_headless") as run, \
             patch("slife.SlifeApp") as app_cls:
            from slife import main
            main()

        run.assert_not_called()
        app_cls.return_value.run.assert_called_once()

    def test_a_fatal_headless_startup_exits_nonzero(self, mock_config):
        """A startup that aborts must reach the shell, not vanish — there is
        no screen to have shown it on."""
        run = MagicMock(return_value=(None, "✗ Required component failed: memdb"))
        with pytest.raises(SystemExit) as exc:
            self._main_with(["slife", "--headless"], mock_config, run)
        assert exc.value.code == 1

    def test_headless_ctrl_c_exits_quietly(self, mock_config):
        run = MagicMock(side_effect=KeyboardInterrupt)
        self._main_with(["slife", "--headless"], mock_config, run)

    def test_headless_host_imports_no_textual(self):
        """`slife.headless` is a host, not a UI: importing it must not pull
        the TUI toolkit in, or `--headless` would pay for a terminal library
        it never draws with.  (``rich`` does come in — ``logfmt``'s console
        handler uses it, and that is the base dependency stack, not a TUI.)

        Checked in a fresh interpreter, because this one has already imported
        the TUI for the other tests.
        """
        import os
        import subprocess
        import sys

        code = (
            "import sys; import slife.headless; "
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] == 'textual' or m.startswith('slife.ui')))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", cwd=os.getcwd(),
        )
        # `slife.ui.i18n` is the one crossing, and it is stdlib-only — the
        # same inversion slife/agent/service.py already documents.
        assert out.stdout.strip() == "['slife.ui', 'slife.ui.i18n']", out.stdout + out.stderr


class TestCliHelp:
    """`--help` answers on the command line, before anything heavy starts."""

    def test_help_prints_usage_and_returns(self, capsys, monkeypatch):
        """main() prints the usage and returns without loading the app."""
        import sys

        from slife import main
        monkeypatch.setattr(sys, "argv", ["slife", "--help"])
        with patch("slife.Config", side_effect=AssertionError("must not load config")):
            main()  # returns; never reaches the config/TUI imports
        out = capsys.readouterr().out
        assert "Usage: slife [options] [config-path]" in out
        for flag in ("--agent", "--headless", "--lang", "-h, --help"):
            assert flag in out

    def test_help_short_flag(self, capsys, monkeypatch):
        import sys

        from slife import main
        monkeypatch.setattr(sys, "argv", ["slife", "-h"])
        main()
        assert "Usage: slife" in capsys.readouterr().out

    def test_help_imports_no_heavy_module(self, monkeypatch):
        """The help path must not pay for Textual, the loop or the plugins."""
        import sys

        from slife import main
        monkeypatch.setattr(sys, "argv", ["slife", "--help"])
        before = set(sys.modules)
        main()
        heavy = {m for m in set(sys.modules) - before
                 if m.split(".")[0] in ("textual", "rich") or m.startswith("slife.ui")}
        assert not heavy, heavy

    def test_worker_help_does_not_start_the_worker(self, capsys):
        """`python -m slife.subagent.worker --help` would otherwise sit on
        stdin waiting for a parent that is never coming."""
        from slife.subagent import worker
        with patch.object(worker.asyncio, "run",
                          side_effect=AssertionError("worker loop must not start")):
            worker.main(["prog", "--help"])
        assert "Usage: slife" in capsys.readouterr().out


class TestMainModule:
    """Tests for python -m Slife (__main__.py)."""

    def test_main_module_import(self):
        """__main__.py module-level code executes main() with patches."""
        from slife.config import Config, ModelConfig
        mc = ModelConfig(
            ref="deepseek/ds",
            provider="deepseek",
            api_model="ds",
            display_name="DS",
            api_key="k",
        )
        cfg = Config(models=[mc], active_model_ref="deepseek/ds", tools=[])

        with patch("slife.Config.from_yaml", return_value=cfg), \
             patch("slife.SlifeApp") as mock_app_cls, \
             patch("slife.logger"):
            mock_app = MagicMock()
            mock_app_cls.return_value = mock_app

            import slife.__main__
            assert hasattr(slife.__main__, 'main')


class TestMainEnvLogging:
    """Tests for env var logging in main() — key masking."""

    @staticmethod
    def _make_config(**kwargs):
        """Build a minimal config for env logging tests."""
        from slife.config import Config, ModelConfig
        mc = ModelConfig(
            ref="deepseek/ds",
            provider="deepseek",
            api_model="ds",
            display_name="DS",
            api_key="k",
        )
        return Config(models=[mc], active_model_ref="deepseek/ds", tools=[], **kwargs)

    def test_env_key_masked(self):
        """API keys are masked in log output."""
        cfg = self._make_config(env={"DEEPSEEK_KEY": "sk-1234567890abcdef"})

        with patch("slife.Config.from_yaml", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "DEEPSEEK_KEY" in t]
                assert len(env_line) == 1
                # Should be masked — not contain the full key
                assert "sk-1234567890abcdef" not in env_line[0]

    def test_env_secret_short_value_masked(self):
        """Short secret values (<8 chars) get fully masked."""
        cfg = self._make_config(env={"API_SECRET": "abc"})

        with patch("slife.Config.from_yaml", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "API_SECRET" in t]
                assert len(env_line) == 1
                assert "***" in env_line[0]

    def test_env_non_secret_logged_plain(self):
        """Non-secret env vars are logged without masking."""
        cfg = self._make_config(env={"MY_VAR": "hello_world"})

        with patch("slife.Config.from_yaml", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "MY_VAR" in t]
                assert len(env_line) == 1
                assert "hello_world" in env_line[0]

    def test_env_token_masked(self):
        """TOKEN in key name triggers masking."""
        cfg = self._make_config(env={"GITHUB_TOKEN": "ghp_1234567890abcdefgh"})

        with patch("slife.Config.from_yaml", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "GITHUB_TOKEN" in t]
                assert len(env_line) == 1
                assert "ghp_1234567890abcdefgh" not in env_line[0]

    def test_env_password_masked(self):
        """PASSWORD in key name triggers masking."""
        cfg = self._make_config(env={"DB_PASSWORD": "supersecret123"})

        with patch("slife.Config.from_yaml", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "DB_PASSWORD" in t]
                assert len(env_line) == 1
                assert "supersecret123" not in env_line[0]

    def test_no_env_vars_silent(self):
        """When config.env is empty, no env log lines are emitted."""
        cfg = self._make_config(env={})

        with patch("slife.Config.from_yaml", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.bootstrap.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_lines = [t for t in debug_texts if "env " in t]
                assert len(env_lines) == 0
