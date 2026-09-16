"""tools.json5 category-section parsing — every section carries the per-entry
``enabled`` / ``autoload`` flags, plus the ``tool_load`` threshold knob."""

from pathlib import Path

from slife.config import Config


def _cfg(tmp_path, tools_raw: str) -> Config:
    tools = tmp_path / "tools.json5"
    tools.write_text(tools_raw, encoding="utf-8")
    slife = tmp_path / "slife.json5"
    slife.write_text(
        "{ models: [{ ref: 'm', provider: 'p', model: 'm' }], active_model: 'm' }",
        encoding="utf-8",
    )
    cfg = Config.from_json5(slife, agent_name="slife")
    # Point the test at the throwaway tools.json5 (from_json5 resolves the
    # sibling in the SAME data dir, so this is already right).
    assert cfg._tools_path == tools
    return cfg


def test_tool_load_threshold(tmp_path):
    cfg = _cfg(tmp_path, "{ tool_load: { threshold: 3 }, cli: {} }\n")
    assert cfg.tool_load_threshold == 3


def test_autoload_is_a_per_entry_flag(tmp_path):
    """``autoload`` sits on the entry, next to ``enabled`` — every section
    that names a tool can set it."""
    cfg = _cfg(
        tmp_path,
        """
        {
          builtin: [{ name: 'execute_shell', autoload: true },
                    { name: 'run_python_script' }],
          job: [{ name: 'translate', autoload: true, enabled: true }],
          cli: {},
        }
        """,
    )
    assert cfg.autoload_tools == frozenset({"execute_shell", "translate"})


def test_autoload_on_a_server_covers_its_tools(tmp_path):
    """An external tool's name is unknown until its server connects, so mcp /
    rest-api mark the SERVER — its whole tool set is born loaded."""
    cfg = _cfg(
        tmp_path,
        """
        {
          mcp: { servers: {
            ondemand: { command: 'npx' },
            eager: { command: 'npx', autoload: true }
          }},
          'rest-api': { weather: { command: 'uvx', autoload: true } },
          cli: {},
        }
        """,
    )
    assert cfg.autoload_servers == frozenset({"eager", "weather"})


def test_autoload_on_skill_or_cli_is_inert(tmp_path):
    """A skill/cli row has no load state, so the flag is accepted and dropped."""
    cfg = _cfg(
        tmp_path,
        """
        {
          skill: [{ name: 'deploy', autoload: true }],
          cli: { gh: { command: 'gh', description: 'GitHub CLI', autoload: true } },
        }
        """,
    )
    assert cfg.autoload_tools == frozenset()
    assert cfg.autoload_servers == frozenset()


def test_tool_load_threshold_invalid_defaults(tmp_path):
    cfg = _cfg(
        tmp_path,
        "{ tool_load: { threshold: 'nope' }, cli: {} }\n",
    )
    assert cfg.tool_load_threshold == 100


def test_job_and_skill_disabled_overlays(tmp_path):
    cfg = _cfg(
        tmp_path,
        """
        {
          job: [{ name: 'job-a' }, { name: 'job-b', enabled: false }],
          skill: [{ name: 'skill-x', enabled: false }],
          cli: {}
        }
        """,
    )
    assert cfg.disabled_jobs == frozenset({"job-b"})
    assert cfg.disabled_skills == frozenset({"skill-x"})


def test_cli_section_still_parsed(tmp_path):
    cfg = _cfg(
        tmp_path,
        "{ cli: { mycmd: { command: 'echo hi', description: 'hi' } } }\n",
    )
    assert cfg.cli_tools["mycmd"]["command"] == "echo hi"


def test_bundled_seed_tools_json5_is_coherent():
    """The installers seed the REPO tools.json5 — it must be valid json5 and
    carry every §8.5 category section (builtin/mcp/rest-api/cli/job/skill)
    plus the tool_load knob, so a fresh install is self-consistent."""
    import json5

    seed = Path(__file__).resolve().parents[1] / "tools.json5"
    assert seed.exists()
    raw = json5.loads(seed.read_text(encoding="utf-8"))

    assert isinstance(raw.get("builtin"), list)
    assert isinstance(raw.get("mcp", {}).get("servers", {}), dict)
    assert isinstance(raw.get("cli", {}), dict)
    assert raw.get("job") == {}
    assert raw.get("skill") == {}
    # rest-api section (quoted key with a dash)
    rest_api = raw.get("rest-api")
    assert isinstance(rest_api, dict) or isinstance(raw.get("rest_api"), dict)
    # the one unified tool-load knob (autoload is per entry, not a knob here)
    assert raw.get("tool_load", {}).get("threshold") == 100
    assert "preload" not in raw.get("tool_load", {})