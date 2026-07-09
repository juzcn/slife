"""Tests for slife main entry point — mock heavy to avoid Textual init."""

import pytest
from unittest.mock import MagicMock, patch


class TestMainFunction:
    """Tests for slife.main() — fully mocked."""

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

                info_texts = [str(c) for c in mock_logger.info.call_args_list]
                assert any("DeepSeek V4 Flash" in t for t in info_texts)

    def test_main_thinking_off(self, mock_config):
        """Logs 'thinking: off' when thinking is disabled."""
        mock_config.models[0].thinking_enabled = False

        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                info_texts = [str(c) for c in mock_logger.info.call_args_list]
                assert any("off" in t for t in info_texts if "Thinking" in t)

    def test_main_logs_tool_count(self, mock_config):
        """Logs the number of loaded tools."""
        mock_config.tools = [{"type": "shell"}, {"type": "serper", "api_key": "k"}]

        with patch("slife.Config.from_json5", return_value=mock_config):
            with patch("slife.SlifeApp") as mock_app_cls:
                mock_app_cls.return_value.run = MagicMock()

                with patch("slife.logger") as mock_logger:
                    from slife import main
                    main()

                info_texts = [str(c) for c in mock_logger.info.call_args_list]
                assert any("2" in t for t in info_texts if "Tools" in t)


class TestMainModule:
    """Tests for python -m slife (__main__.py)."""

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
