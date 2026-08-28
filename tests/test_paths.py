"""Tests for slife.paths — canonical filesystem paths."""

import pytest; pytestmark = pytest.mark.unit


from pathlib import Path

import pytest

from slife import paths


# ── is_dev ──────────────────────────────────────────────────────────────


class TestIsDev:
    """Tests for the is_dev helper — dev = the CWD is the project root AND the
    loaded slife package is that checkout's source ``slife/`` subdir.

    Either condition alone is ambiguous: a production wheel run from inside a
    checkout sees the checkout's ``pyproject.toml``, and a uv-tool install run
    from home sits under the home dir.  Both must hold, so site-packages copies
    are never dev regardless of where the CWD is.
    """

    def _fake_slife(self, monkeypatch, module_file: str | None):
        import sys
        import types

        if module_file is None:
            monkeypatch.setitem(sys.modules, "slife", None)
        else:
            monkeypatch.setitem(
                sys.modules, "slife",
                types.SimpleNamespace(__file__=module_file),
            )

    def _write_project_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "slife"\n', encoding="utf-8"
        )

    def test_true_source_tree_under_cwd(self, tmp_path, monkeypatch):
        """CWD is the project root and the package is its ``slife/`` subdir."""
        self._write_project_toml(tmp_path)
        pkg = tmp_path / "slife" / "__init__.py"
        pkg.parent.mkdir()
        pkg.touch()
        monkeypatch.chdir(tmp_path)
        self._fake_slife(monkeypatch, str(pkg))
        assert paths.is_dev() is True

    def test_false_when_package_in_site_packages(self, tmp_path, monkeypatch):
        """A production wheel run from inside a checkout stays production:
        the checkout's pyproject.toml is in the CWD, but the loaded package
        lives in site-packages."""
        self._write_project_toml(tmp_path)
        monkeypatch.chdir(tmp_path)
        self._fake_slife(
            monkeypatch, "/opt/venv/site-packages/slife/__init__.py",
        )
        assert paths.is_dev() is False

    def test_false_when_no_slife_module(self, tmp_path, monkeypatch):
        """No loaded slife module at all means production."""
        monkeypatch.chdir(tmp_path)
        self._fake_slife(monkeypatch, None)
        assert paths.is_dev() is False

    def test_false_uv_tool_under_home(self, tmp_path, monkeypatch):
        """A uv-tool install under the home dir launched from home stays
        production: the package is under the CWD but the CWD has no
        pyproject.toml (home is not the project root)."""
        pkg = tmp_path / "site-packages" / "slife" / "__init__.py"
        pkg.parent.mkdir(parents=True)
        pkg.touch()
        monkeypatch.chdir(tmp_path)
        self._fake_slife(monkeypatch, str(pkg))
        assert paths.is_dev() is False

    def test_false_nested_site_packages_in_checkout(self, tmp_path, monkeypatch):
        """Even inside a project root, a package under ``site-packages/`` is
        an installed copy, not the checkout's ``slife/`` subdir."""
        self._write_project_toml(tmp_path)
        pkg = tmp_path / "site-packages" / "slife" / "__init__.py"
        pkg.parent.mkdir(parents=True)
        pkg.touch()
        monkeypatch.chdir(tmp_path)
        self._fake_slife(monkeypatch, str(pkg))
        assert paths.is_dev() is False


# ── get_data_dir ─────────────────────────────────────────────────────────


class TestGetDataDir:
    """Tests for get_data_dir."""

    def test_env_var_takes_priority(self, monkeypatch):
        """SLIFE_DATA_DIR env var overrides everything."""
        monkeypatch.setenv("SLIFE_DATA_DIR", "/custom/slife/data")
        assert paths.get_data_dir() == Path("/custom/slife/data")

    def test_dev_mode_returns_cwd(self, monkeypatch):
        """In dev mode the project root (CWD) is the data dir."""
        monkeypatch.delenv("SLIFE_DATA_DIR", raising=False)
        monkeypatch.setattr(paths, "is_dev", lambda: True)
        assert paths.get_data_dir() == Path.cwd()

    def test_production_returns_dot_slife_in_home(self, monkeypatch):
        """In production, data lives under ~/.slife/."""
        monkeypatch.delenv("SLIFE_DATA_DIR", raising=False)
        monkeypatch.setattr(paths, "is_dev", lambda: False)
        assert paths.get_data_dir() == Path.home() / ".slife"


# ── get_config_path ──────────────────────────────────────────────────────


class TestGetConfigPath:
    """Tests for get_config_path."""

    def test_returns_slife_json5_in_data_dir(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        assert paths.get_config_path() == Path("/data/slife.json5")


# ── get_logs_dir ─────────────────────────────────────────────────────────


class TestGetLogsDir:
    """Tests for get_logs_dir."""

    def test_returns_logs_subdir_in_data_dir(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        # test_main.py's slife.main() exports SLIFE_LOG_DIR into the process
        # env; delenv so this test exercises the data-dir fallback.
        monkeypatch.delenv("SLIFE_LOG_DIR", raising=False)
        assert paths.get_logs_dir() == Path("/data/logs")

    def test_slife_log_dir_env_wins(self, monkeypatch):
        """SLIFE_LOG_DIR overrides the data-dir-derived path (the main
        process exports it so plugin children resolve the same directory)."""
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.setenv("SLIFE_LOG_DIR", "/custom/logs")
        assert paths.get_logs_dir() == Path("/custom/logs")


# ── get_db_path ──────────────────────────────────────────────────────────


class TestGetDbPath:
    """Tests for get_db_path."""

    def test_default_agent_name_uses_slife(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.delenv("SLIFE_AGENT_NAME", raising=False)
        assert paths.get_db_path() == Path("/data/slife.db")

    def test_custom_agent_name_in_filename(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.delenv("SLIFE_AGENT_NAME", raising=False)
        assert paths.get_db_path("my-agent") == Path("/data/my-agent.db")

    def test_agent_env_var_used_when_no_arg(self, monkeypatch):
        """SLIFE_AGENT_NAME is honored when no agent_name is passed.

        Regression: health tools (check_memdb/system_health) omit the
        agent and must resolve the current agent's database, not always
        ``slife.db`` (the Jack-agent confusion).
        """
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.setenv("SLIFE_AGENT_NAME", "Jack")
        assert paths.get_db_path() == Path("/data/Jack.db")


# ── get_skills_dir ───────────────────────────────────────────────────────


class TestGetSkillsDir:
    """Tests for get_skills_dir — always returns data_dir/skills."""

    def test_returns_data_dir_skills(self, monkeypatch, tmp_path):
        """Skills live under the data directory."""
        monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path / "data"))
        assert paths.get_skills_dir() == tmp_path / "data" / "skills"

    def test_env_var_flows_through(self, monkeypatch):
        """When SLIFE_DATA_DIR is set, skills path uses it."""
        monkeypatch.setenv("SLIFE_DATA_DIR", "/env-data")
        assert paths.get_skills_dir() == Path("/env-data/skills")
