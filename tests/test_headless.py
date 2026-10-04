"""Tests for slife.headless — the same agent, with no terminal attached.

The host's whole implementation is what it does NOT install, so the tests
assert absences as much as calls: no handler factory, no TUI callbacks, and
nothing on stdout.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest; pytestmark = pytest.mark.unit

from slife.agent.plugins import PluginStartStatus
from slife.agent.service import MemoryDatabaseError

_A2A = SimpleNamespace(enabled=True, broker_host="localhost", broker_port=1883)

_PLUGINS = [("a2a", "slife.plugins.a2a.server")]
_MEMDB = [("memdb", "slife.plugins.memdb.server")]


@pytest.fixture(autouse=True)
def _headless_marker(monkeypatch):
    """``run_headless`` marks the process headless for real — that IS the
    mechanism — so the marker must not outlive the test that produced it: it
    is read by every later prompt render in the suite."""
    monkeypatch.delenv("SLIFE_HEADLESS", raising=False)
    monkeypatch.delenv("SLIFE_SUBAGENT_NAME", raising=False)


def _config(*, required=(), a2a: SimpleNamespace | None = _A2A):
    return SimpleNamespace(
        agent_name="jack",
        plugins_required=set(required),
        a2a_config=a2a,
    )


def _service(*, turns=(), status=PluginStartStatus.STARTED, raises=None):
    """A service stub whose plugin spawn returns *status* (or raises *raises*)."""
    service = MagicMock()
    service.start_inbox = AsyncMock()
    service.get_exit_context_turns = AsyncMock(return_value=list(turns))
    service.start_subagent = AsyncMock()
    service.wait_startup_settled = AsyncMock()
    service.message_history = MagicMock()
    service.inbox = MagicMock()
    if raises is not None:
        service.start_plugin_server = AsyncMock(side_effect=raises)
    else:
        service.start_plugin_server = AsyncMock(return_value=status)
    return service


async def _boot(service, config, plugins=tuple(_PLUGINS)):
    from slife.headless import _boot

    with patch("slife.plugins.discover_plugins", return_value=list(plugins)):
        return await _boot(service, config)


class TestBoot:
    pytestmark = pytest.mark.asyncio

    async def test_starts_the_inbox_and_settles(self):
        service, config = _service(), _config()
        assert await _boot(service, config) == ""
        service.start_inbox.assert_awaited_once()
        service.start_subagent.assert_awaited_once()
        service.wait_startup_settled.assert_awaited_once()
        service.start_plugin_server.assert_awaited_once_with(
            "a2a", "slife.plugins.a2a.server",
        )

    async def test_restores_the_exit_time_context(self):
        """A restart mid-conversation picks up where it left off — the same
        continuity the TUI has, and the reason a peer's task can span turns."""
        turns = [{"created_at": "2026-10-01T10:00:00", "channel": "a2a"}]
        service = _service(turns=turns)
        with patch("slife.headless.restore_context") as restore:
            assert await _boot(service, _config()) == ""
        restore.assert_called_once()
        assert restore.call_args.args[1] == turns

    async def test_a_broken_memory_db_is_fatal(self):
        """Memory is core: a session that cannot read its own history is not
        a session, so startup aborts instead of running memory-less."""
        service = _service()
        service.get_exit_context_turns = AsyncMock(
            side_effect=MemoryDatabaseError("no such table"),
        )
        fatal = await _boot(service, _config())
        assert "memdb" in fatal.lower() or "Memory" in fatal

    async def test_a_failed_restore_still_runs(self):
        """Not every restore failure is fatal — a session that cannot rebuild
        its context runs on what it has, as the TUI's does."""
        service = _service(turns=[{"created_at": "x", "channel": "human"}])
        with patch("slife.headless.restore_context", side_effect=RuntimeError("boom")):
            assert await _boot(service, _config()) == ""

    async def test_installs_no_handler_factory(self):
        """Silence is the absence of the TUI's factory, not a no-op handler:
        with no factory the loop takes its handler-less path — no output, and
        ``_approve`` auto-approves because nobody can answer it."""
        service, config = _service(), _config()
        await _boot(service, config)
        service.inbox._histories.set_default_handler_factory.assert_not_called()

    async def test_boot_registers_no_callbacks(self):
        """The TUI's callbacks exist to draw, and there is nothing to draw on.
        The one that is not a surface — ``on_memory_broken`` — is registered by
        the host loop, not here, because it ends the process."""
        service, config = _service(), _config()
        await _boot(service, config)
        service.on_memory_broken.assert_not_called()
        service.on_activity.assert_not_called()
        service.on_autonomous.assert_not_called()


class TestFatalPaths:
    pytestmark = pytest.mark.asyncio

    async def test_required_plugin_not_discovered_is_fatal(self):
        service = _service()
        fatal = await _boot(service, _config(required=["memdb"]), plugins=[])
        assert "memdb" in fatal
        service.start_plugin_server.assert_not_awaited()

    async def test_required_plugin_that_never_became_ready_is_fatal(self):
        service = _service(status=PluginStartStatus.FAILED)
        fatal = await _boot(
            service, _config(required=["memdb"]), plugins=_PLUGINS + _MEMDB,
        )
        assert "memdb" in fatal
        assert "never became ready" in fatal

    async def test_required_plugin_that_raises_is_fatal(self):
        service = _service(raises=TimeoutError())
        fatal = await _boot(
            service, _config(required=["memdb"]), plugins=_PLUGINS + _MEMDB,
        )
        assert "memdb" in fatal
        # The empty TimeoutError carries no reason — the hang-guard budget is
        # named instead, or the line says nothing at all about why.
        assert "timed out" in fatal

    async def test_optional_plugin_failure_is_not_fatal(self):
        service = _service(status=PluginStartStatus.FAILED)
        assert await _boot(service, _config()) == ""


class TestMeshWarning:
    """The mesh is this process's only input, so its absence is announced —
    but never fatal (the broker may come up later under the watchdog)."""

    pytestmark = pytest.mark.asyncio

    async def test_started_mesh_is_silent(self, capsys):
        service = _service(status=PluginStartStatus.STARTED)
        await _boot(service, _config())
        assert capsys.readouterr().err == ""

    async def test_mesh_not_started_warns_on_stderr(self, capsys):
        service = _service(status=PluginStartStatus.SKIPPED)
        assert await _boot(service, _config()) == ""
        assert "A2A mesh is not connected" in capsys.readouterr().err

    async def test_unreachable_broker_names_the_broker(self, capsys):
        service = _service(status=PluginStartStatus.SKIPPED)
        a2a = SimpleNamespace(enabled=False, broker_host="localhost", broker_port=1883)
        await _boot(service, _config(a2a=a2a))
        assert "localhost:1883" in capsys.readouterr().err

    async def test_no_a2a_section_says_so(self, capsys):
        service = _service(status=PluginStartStatus.SKIPPED)
        await _boot(service, _config(a2a=None))
        assert "no `a2a:` section" in capsys.readouterr().err


class TestRunHeadless:
    def test_keyboard_interrupt_is_a_normal_exit(self):
        """Ctrl+C is the lifecycle — it must not read as a startup failure."""
        from slife.headless import run_headless

        with patch("slife.headless.AgentService"), \
             patch("slife.headless._boot", AsyncMock(side_effect=KeyboardInterrupt)), \
             patch("slife.headless.shutdown_session", AsyncMock()):
            _svc, fatal = run_headless(_config())
        assert fatal == ""

    def test_teardown_runs_on_the_way_out(self):
        """A Ctrl+C that skipped the teardown would leave plugin children and
        a live MQTT session behind."""
        from slife.headless import run_headless

        service = _service()
        shutdown = AsyncMock()

        with patch("slife.headless.AgentService", return_value=service), \
             patch("slife.headless._boot", AsyncMock(side_effect=KeyboardInterrupt)), \
             patch("slife.headless.shutdown_session", shutdown):
            svc, fatal = run_headless(_config())

        assert fatal == ""
        assert svc is service
        service.inbox.cancel.assert_called_once()
        shutdown.assert_awaited_once_with(service)

    def test_marks_the_process_headless_before_the_service_exists(self):
        """The prompt is rendered when the service is built, so the flag has
        to be in the environment before that — and the process is headless
        even when it was launched from a terminal.

        The autouse fixture guarantees the marker starts unset, so what this
        reads is what ``run_headless`` put there.
        """
        import os

        from slife.headless import run_headless

        seen = {}

        def _fake_service(config, role):
            seen["headless"] = os.environ.get("SLIFE_HEADLESS")
            raise KeyboardInterrupt

        with patch("slife.headless.AgentService", side_effect=_fake_service), \
             patch("slife.headless.shutdown_session", AsyncMock()):
            _svc, fatal = run_headless(_config())
        assert fatal == ""
        assert seen["headless"] == "1"

    def test_a_broken_memory_stop_exits_with_the_reason(self):
        """The inbox is already frozen when that callback fires: a headless
        process must stop and say why, not sit there looking healthy."""
        from slife.headless import run_headless

        service = _service()
        service.on_memory_broken = lambda cb: cb("disk on fire")

        with patch("slife.headless.AgentService", return_value=service), \
             patch("slife.headless._boot", AsyncMock(return_value="")), \
             patch("slife.headless.shutdown_session", AsyncMock()):
            _svc, fatal = run_headless(_config())

        assert "disk on fire" in fatal
        service.inbox.cancel.assert_called_once()
