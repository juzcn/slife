"""Tests for entry points (slife.__init__.py and slife.__main__.py)."""

from unittest.mock import MagicMock, patch


class TestMainModule:
    """Tests for slife/__main__.py."""

    def test_main_module_imports_main(self):
        """__main__.py imports main from slife."""
        from slife.__main__ import main
        assert callable(main)


class TestInitExports:
    """Tests for slife/__init__.py module-level attributes."""

    def test_main_is_importable(self):
        """main is importable from slife."""
        from slife import main
        assert callable(main)

    def test_logger_exists(self):
        """Module-level logger is configured."""
        from slife import logger
        import logging
        assert isinstance(logger, logging.Logger)
        assert logger.name == "slife"
