"""Tests for credstore._config — cryptfile path resolution."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from credstore._config import get_cryptfile_path

HOME = Path("/mock/home")


@pytest.fixture(autouse=True)
def _fixed_home(monkeypatch):
    """Pin ``Path.home()`` — resolution is platform- and env-independent."""
    monkeypatch.delenv("CREDSTORE_FILE", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: HOME))


class TestGetCryptfilePath:
    """Tests for get_cryptfile_path resolution."""

    def test_env_var_highest_priority(self):
        with patch.dict(os.environ, {"CREDSTORE_FILE": "/env/path/credentials.crypt"}):
            result = get_cryptfile_path()
            assert result.replace("\\", "/") == "/env/path/credentials.crypt"

    def test_default_is_home_credstore(self):
        assert get_cryptfile_path() == str(HOME / ".credstore" / "credentials.crypt")

    def test_slife_source_tree_cwd_does_not_divert(self, monkeypatch, tmp_path):
        """A Slife checkout in the CWD must not move the cryptfile.

        Regression guard: resolution used to fall back to ``./credentials.crypt``
        whenever the CWD's ``pyproject.toml`` named the project ``slife``,
        which forked the backup and wrote credentials into the source tree.
        """
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "slife"\n', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        assert get_cryptfile_path() == str(HOME / ".credstore" / "credentials.crypt")
