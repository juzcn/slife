"""tools.json5 category-section parsing — every section carries ``enabled``
plus the ``tool_load`` knobs (threshold / preload)."""

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


def test_tool_load_threshold_and_preload(tmp_path):
    cfg = _cfg(
        tmp_path,
        "{ tool_load: { threshold: 3, preload: ['foo', 'bar'] }, cli: {} }\n",
    )
    assert cfg.tool_load_threshold == 3
    assert cfg.tool_load_preload == frozenset({"foo", "bar"})


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
    # the unified tool-load knob
    assert raw.get("tool_load", {}).get("threshold") == 100
    assert "preload" in raw.get("tool_load", {})