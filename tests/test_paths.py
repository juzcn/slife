"""Tests for slife.paths — canonical filesystem paths."""

from pathlib import Path

import pytest

from slife import paths


# ── _is_dev ──────────────────────────────────────────────────────────────


class TestIsDev:
    """Tests for the _is_dev helper."""

    def test_returns_true_when_project_name_is_slife(self, tmp_path, monkeypatch):
        """A pyproject.toml with project.name == 'slife' means dev mode."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[project]\nname = "slife"\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert paths._is_dev() is True

    def test_returns_false_when_project_name_differs(self, tmp_path, monkeypatch):
        """A pyproject.toml with a different project.name is NOT dev mode."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[project]\nname = "other-package"\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert paths._is_dev() is False

    def test_returns_false_when_toml_missing(self, tmp_path, monkeypatch):
        """No pyproject.toml at all means production."""
        monkeypatch.chdir(tmp_path)
        assert paths._is_dev() is False

    def test_returns_false_when_toml_is_invalid(self, tmp_path, monkeypatch):
        """A malformed pyproject.toml is treated as non-dev."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text("not valid toml {{{", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert paths._is_dev() is False


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
        monkeypatch.setattr(paths, "_is_dev", lambda: True)
        assert paths.get_data_dir() == Path.cwd()

    def test_production_returns_dot_slife_in_home(self, monkeypatch):
        """In production, data lives under ~/.slife/."""
        monkeypatch.delenv("SLIFE_DATA_DIR", raising=False)
        monkeypatch.setattr(paths, "_is_dev", lambda: False)
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
        assert paths.get_logs_dir() == Path("/data/logs")


# ── get_db_path ──────────────────────────────────────────────────────────


class TestGetDbPath:
    """Tests for get_db_path."""

    def test_default_agent_id_uses_slife(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        assert paths.get_db_path() == Path("/data/slife.db")

    def test_custom_agent_id_in_filename(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        assert paths.get_db_path("my-agent") == Path("/data/my-agent.db")


# ── get_skills_dir ───────────────────────────────────────────────────────


class TestGetSkillsDir:
    """Tests for get_skills_dir."""

    def test_package_skills_dir_exists(self, monkeypatch, tmp_path):
        """When the skills dir exists next to paths.py, return it."""
        pkg_dir = tmp_path / "slife"
        pkg_dir.mkdir()
        skills_dir = pkg_dir / "skills"
        skills_dir.mkdir()
        dummy_paths = pkg_dir / "paths.py"
        dummy_paths.write_text("")
        monkeypatch.setattr(paths, "__file__", str(dummy_paths))
        result = paths.get_skills_dir()
        assert result.resolve() == skills_dir.resolve()

    def test_falls_back_to_data_dir_skills(self, monkeypatch, tmp_path):
        """When the package skills dir doesn't exist, use data_dir/skills."""
        monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path / "data"))
        # Ensure the package skills dir check fails
        monkeypatch.setattr(
            paths, "__file__",
            str(tmp_path / "slife" / "paths.py"),
        )
        result = paths.get_skills_dir()
        assert result == tmp_path / "data" / "skills"

    def test_env_var_flows_through(self, monkeypatch):
        """When SLIFE_DATA_DIR is set, the fallback uses it."""
        monkeypatch.setenv("SLIFE_DATA_DIR", "/env-data")
        result = paths.get_skills_dir()
        assert result == Path("/env-data/skills")
