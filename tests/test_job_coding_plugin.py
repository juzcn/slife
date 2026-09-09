"""In-process tests for the job-coding plugin.

Follows the existing plugin-test convention (``test_memfiles_plugin.py``,
``test_media_plugin.py``): server tool functions are called directly with
module globals monkeypatched; no child process is spawned.
"""

import json
import typing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest; pytestmark = pytest.mark.unit

from slife.plugins.job_coding import registry, runner
from slife.plugins.job_coding import server


# ── Helpers ─────────────────────────────────────────────────────────


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _make_fn(src: str, name: str):
    ns = {"__name__": "test_jobs"}
    exec(compile(src, f"<job:{name}>", "exec"), ns)  # noqa: S102 — test fixture code
    return ns[name]


class _FakeStreamChunk:
    """minimal StreamChunk stand-in (content only)."""

    def __init__(self, content: str):
        self.content = content
        self.thinking = None
        self.tool_deltas = None
        self.usage = None


class _FakeClient:
    """LLMClient stand-in recording the messages it received.

    Mirrors the real streaming contract (jobs accumulate `chat_stream`).
    """

    def __init__(self, text: str = "ok", usage=None):
        self.text = text
        self.calls: list = []

    async def chat_stream(self, messages, cancel_event=None):
        self.calls.append(messages)
        for part in self.text.split("|"):
            if part:
                yield _FakeStreamChunk(part)


@pytest.fixture
def srv(monkeypatch, tmp_path):
    """Isolated server under test: fresh FastMCP + temp jobs dir."""
    from fastmcp import FastMCP

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(server, "mcp", FastMCP("job-test"))
    monkeypatch.setattr(server, "_jobs_dir", jobs)
    server._registry.clear()
    server._llm_client = None
    server._llm_model_ref = "?"
    yield server
    server._registry.clear()


# ── Registry (scan / load / collect) ────────────────────────────────


def test_scan_collects_public_functions(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _write(
        jobs / "translate.py",
        "from slife.plugins.job_coding import llm\n"
        "async def translate(text: str, lang: str = 'zh') -> str:\n"
        "    '''Translate text into a target language.'''\n"
        "    return await llm.chat(user=f'{lang}')\n",
    )
    found = registry.scan_jobs_dir(jobs)
    assert [j.name for j in found] == ["translate"]
    tr = found[0]
    assert "Translate text" in tr.description
    assert tr.path.name == "translate.py"


def test_scan_skips_private_and_imported(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _write(
        jobs / "misc.py",
        "import re\n"
        "from textwrap import dedent\n"
        "def _private(x):\n    return x\n"
        "def main(text: str) -> str:\n    return dedent(text)\n",
    )
    found = registry.scan_jobs_dir(jobs)
    assert [j.name for j in found] == ["main"]


def test_load_module_evicts_previous_version(tmp_path):
    path = tmp_path / "v.py"
    _write(path, "def a():\n    return 1\n")
    mod1 = registry.load_module(path)
    _write(path, "def b():\n    return 2\n")
    mod2 = registry.load_module(path)
    assert "b" in vars(mod2)
    assert "a" not in vars(mod2)


def test_load_module_reports_broken_source(tmp_path):
    path = tmp_path / "bad.py"
    _write(path, "def broken(:\n")
    with pytest.raises(registry.JobLoadError):
        registry.load_module(path)


# ── Runner (llm handle, model resolution, wrapper) ──────────────────


@pytest.mark.asyncio
async def test_wrap_runs_sync_job():
    fn = _make_fn("def echo(text, times=2):\n    return (text * times).upper()", "echo")
    out = await runner.wrap(fn, _FakeClient())(text="ab", times=2)
    assert out == "ABAB"


@pytest.mark.asyncio
async def test_wrap_runs_async_job_binds_llm():
    fn = _make_fn(
        "from slife.plugins.job_coding import llm\n"
        "async def shout(msg):\n"
        "    return await llm.chat(system='S', user=msg)",
        "shout",
    )
    fake = _FakeClient("LOUD")
    out = await runner.wrap(fn, fake)(msg="hello")
    assert out == "LOUD"
    # Exactly one chat; messages built from the job's arguments only.
    assert fake.calls == [
        [{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}]
    ]


@pytest.mark.asyncio
async def test_llm_chat_streams_and_accumulates():
    """`llm.chat` must STREAM (bailian/anthropic proxies refuse non-streaming
    long requests) and accumulate the streamed text across chunks."""
    fn = _make_fn(
        "from slife.plugins.job_coding import llm\n"
        "async def trans(text):\n"
        "    return await llm.chat(user=text)",
        "trans",
    )
    fake = _FakeClient("The| quick| fox")  # 3 stream chunks
    out = await runner.wrap(fn, fake)(text="x")
    assert out == "The quick fox"


@pytest.mark.asyncio
async def test_wrap_captures_errors_as_result():
    fn = _make_fn("def boom():\n    raise ValueError('kaboom')", "boom")
    out = await runner.wrap(fn, None)()
    assert out.startswith("Error: ValueError: kaboom")


def _config_with_ref(ref: str) -> str:
    """A minimal but parseable slife.json5 with one provider/model."""
    return json.dumps({
        "job_coding_model": ref,
        "models": {"providers": {
            "dp": {
                "base_url": "https://api.deepseek.com", "api_key": "sk-t",
                "api": "openai-completions",
                "models": [{"model": "dp-flash", "name": "DP Flash"}],
            },
        }},
    })


def test_resolve_job_model_uses_job_coding_model(monkeypatch, tmp_path):
    cfg = tmp_path / "slife.json5"
    _write(cfg, _config_with_ref("dp/dp-flash"))
    monkeypatch.setattr(runner, "get_config_path", lambda: cfg)
    monkeypatch.setattr(runner, "_config", None)
    model = runner.resolve_job_model()
    assert model is not None
    assert model.ref == "dp/dp-flash"


def test_resolve_job_model_falls_back_to_active_model(monkeypatch, tmp_path):
    cfg = tmp_path / "slife.json5"
    _write(cfg, "{}")
    monkeypatch.setattr(runner, "get_config_path", lambda: cfg)
    monkeypatch.setattr(runner, "_config", None)

    import slife.config as sc
    stub = SimpleNamespace(active_model=SimpleNamespace(ref="x/y"))
    monkeypatch.setattr(
        sc.Config, "from_json5",
        classmethod(lambda cls, *a, **k: stub),
    )
    model = runner.resolve_job_model()
    assert model.ref == "x/y"


@pytest.mark.asyncio
async def test_llm_chat_model_param_resolves(monkeypatch):
    resolved = SimpleNamespace(ref="dp/m2")
    monkeypatch.setattr(runner, "resolve_model_ref", lambda ref: resolved)
    built = {}

    class _FakeLLM:
        def __init__(self, model):
            built["model"] = model
        async def chat_stream(self, messages, cancel_event=None):
            yield _FakeStreamChunk("do")
            yield _FakeStreamChunk("ne")

    import slife.agent.llm_client as lc
    monkeypatch.setattr(lc, "LLMClient", _FakeLLM)
    out = await runner.llm.chat(user="hi", model="dp/m2")
    assert out == "done"  # two-stream-chunk accumulation
    assert built["model"].ref == "dp/m2"


def test_resolve_model_ref_unknown(monkeypatch, tmp_path):
    cfg = tmp_path / "slife.json5"
    _write(cfg, json.dumps({
        "models": {"providers": {
            "dp": {"base_url": "https://x", "api_key": "sk", "api": "openai-completions",
                   "models": [{"model": "dp-flash"}]},
        }},
    }))
    monkeypatch.setattr(runner, "get_config_path", lambda: cfg)
    monkeypatch.setattr(runner, "_config", None)
    with pytest.raises(ValueError):
        runner.resolve_model_ref("nope/nope")


def test_resolve_model_ref_accepts_bare_id(monkeypatch, tmp_path):
    cfg = tmp_path / "slife.json5"
    _write(cfg, json.dumps({
        "models": {"providers": {
            "dp": {"base_url": "https://x", "api_key": "sk", "api": "openai-completions",
                   "models": [{"model": "dp-flash"}]},
        }},
    }))
    monkeypatch.setattr(runner, "get_config_path", lambda: cfg)
    monkeypatch.setattr(runner, "_config", None)
    model = runner.resolve_model_ref("dp-flash")
    assert model.api_model == "dp-flash"


# ── Model-management tools (server) ─────────────────────────────────


@pytest.mark.asyncio
async def test_job_list_empty(srv):
    out = await srv.job_list()
    assert json.loads(out)["count"] == 0


@pytest.mark.asyncio
async def test_job_write_creates_registers_tool_and_persists(srv, tmp_path):
    out = await srv.job_write(
        name="shout",
        code="def shout(msg: str) -> str:\n    '''Uppercase a message.'''\n    return msg.upper()",
    )
    assert "created" in out
    tools = {t.name for t in await srv.mcp.list_tools()}
    assert "shout" in tools
    assert (tmp_path / "jobs" / "shout.py").exists()


@pytest.mark.asyncio
async def test_job_write_writes_code_verbatim_no_llm_scaffold(srv, tmp_path):
    """Pure jobs are written untouched — no llm import is auto-injected."""
    code = (
        "def pick(items: str) -> str:\n"
        "    '''Pick the first item of a comma-separated list.'''\n"
        "    return items.split(',')[0]"
    )
    out = await srv.job_write(name="pick", code=code)
    assert "created" in out
    written = (tmp_path / "jobs" / "pick.py").read_text(encoding="utf-8")
    assert written == code + "\n"  # verbatim, plus the trailing-newline normal
    assert "job_coding import llm" not in written


@pytest.mark.asyncio
async def test_job_write_requires_matching_function_name(srv):
    out = await srv.job_write(
        name="shout",
        code="def other(msg):\n    return msg",
    )
    assert "must define a public function named 'shout'" in out
    assert "shout" not in srv._registry


@pytest.mark.asyncio
async def test_job_write_rejects_invalid_names(srv):
    for bad in ("bad-name", "job-write", "_priv"):
        out = await srv.job_write(name=bad, code="def x():\n    return 1")
        assert "Error" in out


@pytest.mark.asyncio
async def test_job_run_executes(srv):
    await srv.job_write(
        name="echo",
        code="def echo(text: str, times: int = 2) -> str:\n    return (text * times).upper()",
    )
    out = await srv.job_run(job="echo", params='{"text": "ab", "times": 3}')
    assert out == "ABABAB"


@pytest.mark.asyncio
async def test_job_run_unknown(srv):
    out = await srv.job_run(job="nope")
    assert "unknown job 'nope'" in out


@pytest.mark.asyncio
async def test_job_run_bad_params_json(srv):
    await srv.job_write(
        name="echo",
        code="def echo(text: str) -> str:\n    return text",
    )
    out = await srv.job_run(job="echo", params="not-json{")
    assert "not valid JSON" in out


@pytest.mark.asyncio
async def test_job_write_updates_and_rolls_back(srv):
    await srv.job_write(
        name="shout",
        code="def shout(msg: str) -> str:\n    return msg.upper()",
    )
    assert await srv.job_run(job="shout", params='{"msg": "hi"}') == "HI"

    out = await srv.job_write(
        name="shout",
        code="def shout(msg: str) -> str:\n    return f'[{msg.upper()}]'",
    )
    assert "updated" in out
    assert await srv.job_run(job="shout", params='{"msg": "hi"}') == "[HI]"

    # Broken write rolls back to the previous working code.
    out = await srv.job_write(name="shout", code="def broken(:\n")
    assert "previous code restored" in out
    assert await srv.job_run(job="shout", params='{"msg": "hi"}') == "[HI]"


@pytest.mark.asyncio
async def test_job_remove_unregisters(srv, tmp_path):
    await srv.job_write(
        name="shout",
        code="def shout(msg: str) -> str:\n    return msg.upper()",
    )
    out = await srv.job_remove(name="shout")
    assert "removed" in out
    assert "shout" not in srv._registry
    assert not (tmp_path / "jobs" / "shout.py").exists()
    names = {t.name for t in await srv.mcp.list_tools()}
    assert "shout" not in names


@pytest.mark.asyncio
async def test_check_reports_facts(srv):
    await srv.job_write(
        name="shout",
        code="def shout(msg: str) -> str:\n    return msg.upper()",
    )
    data = json.loads(await srv.__check())
    assert data["jobs"] == 1
    assert data["job_names"] == ["shout"]


# ── Harness rescan helper (unit) ────────────────────────────────────


@pytest.mark.asyncio
async def test_rescan_registers_and_unregisters(sample_config):
    from slife.agent.service import AgentService
    from slife.agent.plugins import PluginLifecycle

    service = AgentService(sample_config)
    lifecycle = PluginLifecycle("job-coding", service)
    client = AsyncMock()
    client.is_connected = True
    client.list_tools = AsyncMock(return_value=[
        {"server": "job-coding", "name": "translate", "description": "",
         "inputSchema": {"type": "object", "properties": {}}},
    ])
    lifecycle.client = client
    lifecycle.registered_tools = {"gone"}
    service._plugins["job-coding"] = lifecycle
    service.tool_registry.register(SimpleNamespace(name="gone"))

    await service._rescan_plugin_tools("job-coding")

    names = {t.name for t in service.tool_registry.list_tools()}
    assert "translate" in names
    assert "gone" not in names


# ── mcp handle (bare MCP via the gateway) ─────────────────────────────


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    """Isolate the module-level mcp-handle state across in-process tests.

    ``runner.mcp`` keeps its gateway port/client as module globals; any test
    touching it must start (and end) clean so a connected fake client from
    one test never leaks into the next.
    """
    runner._gateway_port = None
    runner._gateway_port_source = ""
    runner._gateway_client = None
    runner._gateway_lock = None
    yield
    runner._gateway_port = None
    runner._gateway_port_source = ""
    runner._gateway_client = None
    runner._gateway_lock = None


class _FakeGateClient:
    """MCPClient stand-in for the gateway connection.

    Records connect / disconnect / call_tool.  ``result`` may be a str to
    return or an exception to raise — mirrors MCPClient.call_tool's
    never-raise contract at the proxy boundary.
    """

    def __init__(self, result="RESULT"):
        self.result = result
        self.calls: list = []
        self.urls: list[str] = []
        self.disconnects = 0
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, url: str) -> None:
        self.urls.append(url)
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self._connected = False

    async def call_tool(self, name: str, args: dict | None = None) -> str:
        self.calls.append((name, args))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_mcp_call_forwards_bare_call(monkeypatch):
    client = _FakeGateClient("R")
    monkeypatch.setattr(runner.mcp, "_client", AsyncMock(return_value=client))
    out = await runner.mcp.call("github", "search_code", {"q": "abc"})
    assert out == "R"
    assert client.calls == [(
        "__mcp_call_tool",
        {"server": "github", "tool_name": "search_code",
         "arguments": json.dumps({"q": "abc"}, ensure_ascii=False)},
    )]


@pytest.mark.asyncio
async def test_mcp_call_defaults_args_to_empty_json(monkeypatch):
    client = _FakeGateClient("R")
    monkeypatch.setattr(runner.mcp, "_client", AsyncMock(return_value=client))
    await runner.mcp.call("fs", "list_directory")
    assert client.calls == [(
        "__mcp_call_tool",
        {"server": "fs", "tool_name": "list_directory", "arguments": "{}"},
    )]


@pytest.mark.asyncio
async def test_mcp_call_no_gateway_returns_error(monkeypatch):
    monkeypatch.setattr(runner.mcp, "_client", AsyncMock(return_value=None))
    out = await runner.mcp.call("github", "x")
    assert out.startswith("Error: mcp.call")


@pytest.mark.asyncio
async def test_mcp_call_surfaces_client_failure(monkeypatch):
    client = _FakeGateClient(RuntimeError("boom"))
    monkeypatch.setattr(runner.mcp, "_client", AsyncMock(return_value=client))
    out = await runner.mcp.call("github", "x")
    assert out.startswith("Error: mcp.call('github', 'x')")
    assert "boom" in out


@pytest.mark.asyncio
async def test_mcp_client_opens_on_pushed_port(monkeypatch):
    import slife.plugins.mcp_gateway.client as gw_client_mod

    fake = _FakeGateClient()
    monkeypatch.setattr(gw_client_mod, "MCPClient", lambda: fake)
    monkeypatch.setenv("SLIFE_MCP_GATEWAY_PORT", "9999")  # push must win
    runner._gateway_port = "1234"
    runner._gateway_port_source = "push"

    client = await runner.mcp._client()

    assert client is fake
    assert fake.urls == ["http://127.0.0.1:1234/mcp"]
    assert runner._gateway_port == "1234"
    assert runner._gateway_port_source == "push"


@pytest.mark.asyncio
async def test_mcp_client_falls_back_to_env_port(monkeypatch):
    import slife.plugins.mcp_gateway.client as gw_client_mod

    fake = _FakeGateClient()
    monkeypatch.setattr(gw_client_mod, "MCPClient", lambda: fake)
    monkeypatch.setenv("SLIFE_MCP_GATEWAY_PORT", "7777")

    client = await runner.mcp._client()

    assert client is fake
    assert fake.urls == ["http://127.0.0.1:7777/mcp"]
    assert runner._gateway_port == "7777"
    assert runner._gateway_port_source == "env"


@pytest.mark.asyncio
async def test_mcp_client_no_port_returns_none(monkeypatch):
    import slife.plugins.mcp_gateway.client as gw_client_mod

    fake = _FakeGateClient()
    monkeypatch.setattr(gw_client_mod, "MCPClient", lambda: fake)
    monkeypatch.delenv("SLIFE_MCP_GATEWAY_PORT", raising=False)

    out = await runner.mcp._client()

    assert out is None
    assert fake.urls == []


@pytest.mark.asyncio
async def test_mcp_client_connect_failure_returns_none(monkeypatch):
    import slife.plugins.mcp_gateway.client as gw_client_mod

    class _Boom:
        async def connect(self, url: str) -> None:
            raise ConnectionError("refused")

    monkeypatch.setattr(gw_client_mod, "MCPClient", lambda: _Boom())
    monkeypatch.setenv("SLIFE_MCP_GATEWAY_PORT", "7777")

    out = await runner.mcp._client()

    assert out is None
    assert runner._gateway_client is None


@pytest.mark.asyncio
async def test_mcp_set_port_repoints_and_drops_client():
    old = _FakeGateClient()
    old._connected = True
    runner._gateway_client = old
    runner._gateway_port = "1111"
    runner._gateway_port_source = "push"

    await runner.mcp.set_port(2222)

    assert old.disconnects == 1
    assert old.is_connected is False
    assert runner._gateway_client is None
    assert runner._gateway_port == "2222"
    assert runner._gateway_port_source == "push"


@pytest.mark.asyncio
async def test_mcp_set_port_same_port_noop():
    old = _FakeGateClient()
    old._connected = True
    runner._gateway_client = old
    runner._gateway_port = "1111"
    runner._gateway_port_source = "push"

    await runner.mcp.set_port(1111)

    assert old.disconnects == 0
    assert runner._gateway_client is old


@pytest.mark.asyncio
async def test_mcp_set_port_none_clears():
    await runner.mcp.set_port(None)
    assert runner._gateway_port is None
    assert runner._gateway_port_source == ""


@pytest.mark.asyncio
async def test_server_sets_gateway_port(srv):
    out = json.loads(await srv.__set_mcp_gateway_port(port=5555))
    assert out == {"port": "5555", "source": "push"}
    assert runner._gateway_port == "5555"
    assert runner._gateway_port_source == "push"


@pytest.mark.asyncio
async def test_check_reflects_opened_gateway(srv, monkeypatch):
    import slife.plugins.mcp_gateway.client as gw_client_mod

    fake = _FakeGateClient()
    monkeypatch.setattr(gw_client_mod, "MCPClient", lambda: fake)
    monkeypatch.setenv("SLIFE_MCP_GATEWAY_PORT", "7777")
    await runner.mcp._client()

    data = json.loads(await srv.__check())

    assert data["mcp_gateway"] == {"port": "7777", "source": "env", "connected": True}


# ── Host port push (AgentService) ────────────────────────────────────


@pytest.mark.asyncio
async def test_push_gateway_port_to_jobs(sample_config):
    from slife.agent.service import AgentService
    from slife.agent.plugins import PluginLifecycle

    service = AgentService(sample_config)
    jobs = PluginLifecycle("job-coding", service)
    client = AsyncMock()
    client.is_connected = True
    client.call_tool = AsyncMock(return_value='{"port": "9999"}')
    jobs.client = client
    service._plugins["job-coding"] = jobs

    await service._push_gateway_port_to_jobs(SimpleNamespace(port=9999))

    client.call_tool.assert_awaited_once_with("__set_mcp_gateway_port", {"port": 9999})


@pytest.mark.asyncio
async def test_push_gateway_port_skips_when_jobs_down(sample_config):
    from slife.agent.service import AgentService
    from slife.agent.plugins import PluginLifecycle

    service = AgentService(sample_config)
    jobs = PluginLifecycle("job-coding", service)
    jobs.client = None
    service._plugins["job-coding"] = jobs

    await service._push_gateway_port_to_jobs(SimpleNamespace(port=9999))
    # no assertion beyond "didn't raise"


@pytest.mark.asyncio
async def test_wire_mcp_glue_pushes_port_to_jobs(sample_config, monkeypatch):
    from slife.agent.service import AgentService
    from slife.agent.plugins import PluginLifecycle

    service = AgentService(sample_config)
    gw = PluginLifecycle("mcp-gateway", service)
    gw.port = 12345
    gw_client = AsyncMock()
    gw_client.is_connected = True
    gw.client = gw_client
    jobs = PluginLifecycle("job-coding", service)
    jobs_client = AsyncMock()
    jobs_client.is_connected = True
    jobs_client.call_tool = AsyncMock(return_value='{"port": "12345"}')
    jobs.client = jobs_client
    service._plugins["mcp-gateway"] = gw
    service._plugins["job-coding"] = jobs
    service._tool_ctx.mcp_client = None
    monkeypatch.setattr(service, "_sync_mcp_proxies", AsyncMock())

    await service._wire_mcp_glue()

    jobs_client.call_tool.assert_awaited_once_with("__set_mcp_gateway_port", {"port": 12345})