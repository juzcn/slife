"""Tests for slife.paths — canonical filesystem paths."""

import pytest; pytestmark = pytest.mark.unit


from pathlib import Path

import pytest

from slife import paths


# ── is_dev ──────────────────────────────────────────────────────────────


class TestIsDev:
    """Tests for the is_dev helper."""

    def test_returns_true_when_project_name_is_slife(self, tmp_path, monkeypatch):
        """A pyproject.toml with project.name == 'slife' means dev mode."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[project]\nname = "slife"\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert paths.is_dev() is True

    def test_returns_false_when_project_name_differs(self, tmp_path, monkeypatch):
        """A pyproject.toml with a different project.name is NOT dev mode."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[project]\nname = "other-package"\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert paths.is_dev() is False

    def test_returns_false_when_toml_missing(self, tmp_path, monkeypatch):
        """No pyproject.toml at all means production."""
        monkeypatch.chdir(tmp_path)
        assert paths.is_dev() is False

    def test_returns_false_when_toml_is_invalid(self, tmp_path, monkeypatch):
        """A malformed pyproject.toml is treated as non-dev."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text("not valid toml {{{", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
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
        assert paths.get_logs_dir() == Path("/data/logs")


# ── get_db_path ──────────────────────────────────────────────────────────


class TestGetDbPath:
    """Tests for get_db_path."""

    def test_default_agent_id_uses_slife(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.delenv("SLIFE_AGENT_ID", raising=False)
        assert paths.get_db_path() == Path("/data/slife.db")

    def test_custom_agent_id_in_filename(self, monkeypatch):
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.delenv("SLIFE_AGENT_ID", raising=False)
        assert paths.get_db_path("my-agent") == Path("/data/my-agent.db")

    def test_agent_env_var_used_when_no_arg(self, monkeypatch):
        """SLIFE_AGENT_ID is honored when no agent_id is passed.

        Regression: health tools (check_memdb/system_health) omit the
        agent and must resolve the current agent's database, not always
        ``slife.db`` (the Jack-agent confusion).
        """
        monkeypatch.setenv("SLIFE_DATA_DIR", "/data")
        monkeypatch.setenv("SLIFE_AGENT_ID", "Jack")
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
