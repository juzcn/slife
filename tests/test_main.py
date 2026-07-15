"""Tests for Slife main entry point — mock heavy to avoid Textual init."""

import pytest
from unittest.mock import MagicMock, patch


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
        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                from slife import main
                main("test_config.json5")

                mock_app.run.assert_called_once()

    def test_main_default_config_path(self, mock_config):
        """main() uses slife.json5 by default."""
        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                from slife import main
                main()

    def test_main_creates_app_with_config(self, mock_config):
        """SlifeApp is created with the loaded config."""
        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                from slife import main
                main()

                mock_app_cls.assert_called_once_with(mock_config)

    def test_main_logs_model_info(self, mock_config):
        """main() logs model info before starting TUI."""
        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                assert any("DeepSeek V4 Flash" in t for t in debug_texts)

    def test_main_thinking_off(self, mock_config):
        """Logs 'thinking: off' when thinking is disabled."""
        mock_config.models[0].thinking_enabled = False

        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                assert any("off" in t for t in debug_texts if "thinking" in t)

    def test_main_logs_tool_count(self, mock_config):
        """Logs the number of loaded tools."""
        mock_config.tools = [{"name": "execute_shell"}, {"name": "run_python_script"}]

        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                assert any("2" in t for t in debug_texts if "tools" in t)


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

        with patch("slife.Config.from_json5", return_value=cfg), \
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

        with patch("slife.Config.from_json5", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
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

        with patch("slife.Config.from_json5", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "API_SECRET" in t]
                assert len(env_line) == 1
                assert "***" in env_line[0]

    def test_env_non_secret_logged_plain(self):
        """Non-secret env vars are logged without masking."""
        cfg = self._make_config(env={"MY_VAR": "hello_world"})

        with patch("slife.Config.from_json5", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "MY_VAR" in t]
                assert len(env_line) == 1
                assert "hello_world" in env_line[0]

    def test_env_token_masked(self):
        """TOKEN in key name triggers masking."""
        cfg = self._make_config(env={"GITHUB_TOKEN": "ghp_1234567890abcdefgh"})

        with patch("slife.Config.from_json5", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "GITHUB_TOKEN" in t]
                assert len(env_line) == 1
                assert "ghp_1234567890abcdefgh" not in env_line[0]

    def test_env_password_masked(self):
        """PASSWORD in key name triggers masking."""
        cfg = self._make_config(env={"DB_PASSWORD": "supersecret123"})

        with patch("slife.Config.from_json5", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_line = [t for t in debug_texts if "DB_PASSWORD" in t]
                assert len(env_line) == 1
                assert "supersecret123" not in env_line[0]

    def test_no_env_vars_silent(self):
        """When config.env is empty, no env log lines are emitted."""
        cfg = self._make_config(env={})

        with patch("slife.Config.from_json5", return_value=cfg):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app = MagicMock()
                mock_app_cls.return_value = mock_app

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                debug_texts = [str(c) for c in mock_logger.debug.call_args_list]
                env_lines = [t for t in debug_texts if "env " in t]
                assert len(env_lines) == 0
