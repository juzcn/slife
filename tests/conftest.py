"""Shared test fixtures and mocks for the Slife test suite."""

import os
import sys
import threading
import traceback
from unittest.mock import AsyncMock, MagicMock

import pytest

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage
from slife.agent.message_history import MessageHistory
from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry
from slife.tools._yaml_doc import new_yaml, render


# ── pytest configuration ────────────────────────────────────────────────


def pytest_configure(config):
    """Register markers for VS Code / older pytest compatibility."""
    config.addinivalue_line("markers", "unit: fast, isolated unit test (no I/O, no subprocess)")
    config.addinivalue_line("markers", "integration: test requiring I/O, network, or subprocess")
    config.addinivalue_line("markers", "e2e: end-to-end test requiring a full running system")
    config.addinivalue_line("markers", "slow: mark a test as slow (excluded from quick runs)")
    _track_aiosqlite_creations()


# ── Exit guard: a leaked worker thread must never wedge the suite ────────
#
# aiosqlite's connection thread is NOT a daemon — 0.22.1 builds it with a bare
# ``Thread(target=...)``.  So ONE connection a test forgot to close keeps the
# interpreter alive forever at exit: the suite finishes, prints its results,
# and then blocks with nothing left to say.  It reads as a wedged test, costs
# minutes every time, and the only clue is a process that will not die.
#
# The harness therefore reaps what the session left behind — and NAMES it,
# with the stack that created it, because silently reaping would hide the bug
# rather than fix it.  Every other live non-daemon thread is reported too, so
# a future cause of this symptom arrives with a name attached instead of as
# another silent hang.

#: id(Connection) → the frames that created it.  Populated by the tracking
#: hook below; read only when a leak has to be explained.
_CREATED_AT: dict[int, list[str]] = {}


def _track_aiosqlite_creations() -> None:
    """Record where each aiosqlite connection was opened (leak forensics).

    A leaked connection cannot say who leaked it — the object outlives the
    test that made it, and the thread it left behind has no creator.  One
    frame snapshot per connection (a few hundred per session) buys the exact
    test and line the next time this fires.
    """
    import aiosqlite

    if getattr(aiosqlite.Connection, "_slife_tracked", False):
        return
    original = aiosqlite.Connection.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _CREATED_AT[id(self)] = [
            f"{f.filename}:{f.lineno} in {f.name}"
            for f in traceback.extract_stack()[:-1]
            if "_pytest" not in f.filename and "conftest.py" not in f.filename
        ][-3:]

    aiosqlite.Connection.__init__ = __init__
    aiosqlite.Connection._slife_tracked = True


def _reap_leaked_connections() -> list[str]:
    """Stop every still-running aiosqlite worker; return one line per leak."""
    import gc

    import aiosqlite

    leaks: list[str] = []
    for obj in gc.get_objects():
        if not isinstance(obj, aiosqlite.Connection):
            continue
        if not getattr(obj, "_running", False):
            continue
        origin = _CREATED_AT.get(id(obj), [])
        leaks.append(
            f"  leaked aiosqlite connection — opened at "
            f"{origin[-1] if origin else '<unknown>'}"
            + (f"\n    via {' <- '.join(reversed(origin[:-1]))}" if len(origin) > 1 else "")
        )
        try:
            obj.stop()                      # closes it and breaks the worker loop
            obj._thread.join(timeout=5)     # the part that unblocks interpreter exit
        except Exception as e:  # never let cleanup mask the real failure
            leaks.append(f"    (reap failed: {e})")
    return leaks


def _live_non_daemon_threads() -> list[str]:
    """Non-daemon threads other than MainThread — each one blocks exit."""
    return [
        f"  {t.name} ({'alive' if t.is_alive() else 'dead'})"
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon
    ]


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Reap leaked resources and report them — the run must always exit."""
    leaks = _reap_leaked_connections()
    stragglers = _live_non_daemon_threads()
    if not leaks and not stragglers:
        return
    report = []
    if leaks:
        report.append(
            f"{len(leaks)} aiosqlite connection(s) left open by the session "
            "— closed now, but they would have blocked exit forever:"
        )
        report.extend(leaks)
    if stragglers:
        report.append(
            "non-daemon thread(s) still alive at session end (these block "
            "interpreter exit):"
        )
        report.extend(stragglers)
    print("\n\n" + "=" * 70 + "\nTEST SESSION LEAK REPORT\n" + "=" * 70,
          file=sys.stderr, flush=True)
    print("\n".join(report), file=sys.stderr, flush=True)


# ── UI language pinning ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_ui_language():
    """Force the TUI i18n language to English for every test.

    ``slife.ui.i18n`` resolves the language from the OS locale at import
    time.  On a Chinese Windows dev machine that's ``zh``, which would flip
    every TUI string to Chinese and break the suite's English assertions
    (``"Arguments"``, ``"Thinking"``, ``"Switched"``, ``"running"`` …).
    Pinning to ``en`` keeps those assertions valid and matches the
    "tests are written in English" convention.  Tests that exercise the
    i18n layer itself call ``set_language`` explicitly on top of this.
    """
    from slife.ui.i18n import set_language
    set_language("en")
    yield
    set_language("en")


# ── tools catalog isolation ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_tools_db(tmp_path):
    """Point the unified tool catalog at a throwaway db for every test.

    ``AgentService.start_inbox`` → ``_init_catalog`` opens the catalog db;
    without this override, dev-mode tests resolve it to ``<CWD>/tools.db``
    in the repo root (dev-mode data dir = CWD).  Per-test function scope: a
    shared file across many open writable WAL connections would stall writers
    on the 30s busy_timeout.
    """
    path = tmp_path / "tools.db"
    os.environ["SLIFE_TOOLS_DB"] = str(path)
    yield path
    os.environ.pop("SLIFE_TOOLS_DB", None)


# ── A2A inbound-state isolation ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_a2a_inbound_state(tmp_path):
    """Point the A2A inbound-task store at a throwaway file for every test.

    ``A2AMesh`` opens the store on construction, and in dev mode its default
    path resolves to ``<CWD>/a2a_inbound.yaml`` — the repo root.  Without this
    override a test would read (and then rewrite) the developer's real state,
    making the stale-task assertions depend on whatever the last run left
    behind.
    """
    path = tmp_path / "a2a_inbound.yaml"
    os.environ["A2A_INBOUND_FILE"] = str(path)
    yield path
    os.environ.pop("A2A_INBOUND_FILE", None)


# ── tools config isolation ──────────────────────────────────────────────


#: Test modules that exercise ``slife.plugins.mcp_gateway`` config persistence
#: (they lived under ``tests/mcp/`` before the folders were flattened).
#: They must never read/write a real ``tools.yaml`` — the dev data
#: dir (repo root) holds the git-tracked file — so every access is pointed
#: at a throwaway file.
_MCP_ISOLATED_MODULES = frozenset({
    "test_mcp_config",
    "test_embeddings",
    "test_logging",
    "test_mcp_client",
    "test_mcp_connection",
    "test_mcp_oauth",
    "test_mcp_process",
    "test_mcp_server",
    "test_schema_flatten",
    "test_tool_catalog",
})


@pytest.fixture(autouse=True)
def _isolate_mcp_config_path(request, tmp_path, monkeypatch):
    """Point gateway config reads/writes at a throwaway file per test.

    Scoped to the gateway test modules (previously ``tests/mcp/*``) so
    the rest of the suite keeps resolving the real data-dir config.
    """
    if request.node.fspath.purebasename not in _MCP_ISOLATED_MODULES:
        return

    monkeypatch.setenv("TOOLS_FILE", str(tmp_path / "tools.yaml"))

    def _reset():
        # Import lazily so it never runs against a half-built package.
        try:
            import slife.plugins.mcp_gateway.config as _cfg
            _cfg._CURRENT_PATH = None
        except ImportError:
            pass

    request.addfinalizer(_reset)


# ── Model config fixtures ─────────────────────────────────────────────


@pytest.fixture(scope="session")
def sample_model_config():
    """A typical ModelConfig for testing."""
    return ModelConfig(
        ref="deepseek/deepseek-v4-flash",
        provider="deepseek",
        api_model="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        api_key="sk-test-key",
        base_url="https://api.deepseek.com",
        api="openai-completions",
        supports_vision=False,
        max_tokens=4096,
        context_window=131072,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
        reasoning_effort=None,
    )


@pytest.fixture(scope="session")
def thinking_model_config():
    """Model config with thinking enabled."""
    return ModelConfig(
        ref="deepseek/deepseek-v4-pro",
        provider="deepseek",
        api_model="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro",
        api_key="sk-pro-key",
        base_url="https://api.deepseek.com",
        api="openai-completions",
        supports_vision=True,
        max_tokens=8192,
        context_window=131072,
        temperature=0.6,
        top_p=0.9,
        thinking_enabled=True,
        reasoning_effort="high",
    )


@pytest.fixture(scope="session")
def openai_model_config():
    """Model config for a non-DeepSeek provider (OpenAI)."""
    return ModelConfig(
        ref="openai/gpt-4o",
        provider="openai",
        api_model="gpt-4o",
        display_name="GPT-4o",
        api_key="sk-openai-key",
        base_url="https://api.openai.com/v1",
        api="openai-completions",
        supports_vision=True,
        max_tokens=4096,
        context_window=128000,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
        reasoning_effort=None,
    )


# ── Config fixtures ───────────────────────────────────────────────────


@pytest.fixture
def sample_config(sample_model_config):
    """A typical Config with one model and shell tool."""
    return Config(
        models=[sample_model_config],
        active_model_ref="deepseek/deepseek-v4-flash",
        tools=[
            {"name": "execute_shell", "timeout": 30},
        ],
        max_iterations=10,
    )


# ── MessageHistory fixture ──────────────────────────────────────────────


@pytest.fixture
def history():
    """Fresh history with a system prompt."""
    return MessageHistory(system_prompt="You are a helpful assistant.")


@pytest.fixture
def empty_history():
    """Fresh history without system prompt."""
    return MessageHistory()


# ── Token usage fixtures ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def zero_usage():
    """Empty token usage."""
    return TokenUsage()


# ── Tool registry fixtures ────────────────────────────────────────────


class _EchoTool(Tool):
    """Test tool that echoes its arguments."""
    name = "echo"
    description = "Echoes back the input."
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message to echo."}
        },
        "required": ["message"],
    }

    async def execute(self, message: str = "") -> str:
        return f"Echo: {message}"


class _FailingTool(Tool):
    """Test tool that always raises."""
    name = "failer"
    description = "Always fails."
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Reason for failure."}
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        reason = kwargs.get("reason", "unknown")
        raise RuntimeError(f"Intentional failure: {reason}")


@pytest.fixture(scope="session")
def echo_tool():
    """An echo test tool instance."""
    return _EchoTool()


@pytest.fixture(scope="session")
def failing_tool():
    """A failing test tool instance."""
    return _FailingTool()


@pytest.fixture(scope="session")
def tool_registry(echo_tool, failing_tool):
    """Registry with both echo and failing tools registered (session-scoped, read-only)."""
    registry = ToolRegistry()
    registry.register(echo_tool)
    registry.register(failing_tool)
    return registry


@pytest.fixture(scope="session")
def empty_registry():
    """An empty tool registry."""
    return ToolRegistry()


# ── LLM response mocks ────────────────────────────────────────────────


class _MockChoice:
    """Mock for openai choice object."""
    __slots__ = ("delta",)

    def __init__(self, delta):
        self.delta = delta


class _MockStreamEvent:
    """Mock for a streaming API event."""
    __slots__ = ("choices", "usage")

    def __init__(self, delta=None, usage=None):
        self.choices = [_MockChoice(delta)] if delta else []
        self.usage = usage


class _MockDelta:
    """Mock delta with optional content, reasoning, tool_calls."""
    __slots__ = ("content", "reasoning_content", "tool_calls")

    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _MockToolCallDelta:
    """Mock for a single tool call delta."""
    __slots__ = ("index", "id", "function")

    def __init__(self, index=0, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


class _MockFunctionDelta:
    """Mock for function delta in tool call."""
    __slots__ = ("name", "arguments")

    def __init__(self, name=None, arguments=""):
        self.name = name
        self.arguments = arguments


class _MockUsage:
    """Mock for API usage response."""
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")

    def __init__(self, prompt_tokens=100, completion_tokens=50, total_tokens=150):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


# ── Async helpers ─────────────────────────────────────────────────────


def async_return(value):
    """Create a coroutine that returns the given value."""
    async def _inner():
        return value
    return _inner()


def make_async_iter(items):
    """Create an async iterator from a list of items."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


# ── Config text helpers ───────────────────────────────────────────────


def dump_config(data) -> str:
    """Serialize a config dict to YAML text — the write side of a fixture."""
    return render(data)


def load_config_text(text: str) -> dict:
    """Parse config YAML text back to a dict — the read side of a fixture."""
    return new_yaml().load(text)


# ── Config builders ───────────────────────────────────────────────────


def build_yaml_config(models=None, active_model=None, tools=None, agent=None):
    """Build a minimal YAML-serializable config dict for testing."""
    cfg = {
        "models": models or {
            "providers": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com",
                    "api_key": "sk-test",
                    "models": [
                        {
                            "model": "deepseek-v4-flash",
                            "name": "DeepSeek V4 Flash",
                        }
                    ],
                }
            }
        },
        "active_model": active_model or "deepseek/deepseek-v4-flash",
        "tools": tools or [],
    }
    if agent is not None:
        cfg["agent"] = agent
    return cfg
