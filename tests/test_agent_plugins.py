"""Tests for slife.agent.plugins — PluginLifecycle container."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from slife.agent.plugins import PluginLifecycle, plugin_port_env


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_service():
    """A mock AgentService with tool_registry."""
    service = MagicMock()
    service.tool_registry = MagicMock()
    service.config = MagicMock()
    service.config.tool_timeout = 60
    return service


@pytest.fixture
def lifecycle(mock_service):
    """A fresh PluginLifecycle instance."""
    return PluginLifecycle("test_plugin", mock_service)


# ── Initialisation ────────────────────────────────────────────────────────


class TestPluginLifecycleInit:
    """Tests for PluginLifecycle.__init__."""

    def test_default_values(self, lifecycle, mock_service):
        """New lifecycle has all defaults."""
        assert lifecycle.name == "test_plugin"
        assert lifecycle._service is mock_service
        assert lifecycle.client is None
        assert lifecycle.process is None
        assert lifecycle.port == 0
        assert lifecycle.poll_task is None

    def test_different_names(self, mock_service):
        """Each plugin gets its own name."""
        mcp = PluginLifecycle("mcp", mock_service)
        memdb = PluginLifecycle("memdb", mock_service)
        assert mcp.name == "mcp"
        assert memdb.name == "memdb"
        assert mcp is not memdb


# ── plugin_port_env ───────────────────────────────────────────────────────


class TestPluginPortEnv:
    """The canonical SLIFE_{NAME}_PORT env key — dashes normalised to
    underscores so dash-named plugins (local-embed) match how subagents
    read plugin ports."""

    def test_underscore_name_unchanged(self):
        assert plugin_port_env("memfiles") == "SLIFE_MEMFILES_PORT"
        assert plugin_port_env("memdb") == "SLIFE_MEMDB_PORT"

    def test_dash_name_normalised(self):
        assert plugin_port_env("local-embed") == "SLIFE_LOCAL_EMBED_PORT"

    def test_case_normalised(self):
        assert plugin_port_env("Local-Embed") == "SLIFE_LOCAL_EMBED_PORT"


# ── spawn ─────────────────────────────────────────────────────────────────


class TestPluginLifecycleSpawn:
    """Tests for PluginLifecycle.spawn()."""

    @pytest.mark.asyncio
    async def test_spawn_sets_port_env_var(self, lifecycle):
        """spawn() sets SLIFE_{NAME}_PORT env var and marks the plugin ready."""
        mock_process = MagicMock()
        mock_process.port = 9999
        mock_client = MagicMock()
        mock_client.list_tools = AsyncMock(return_value=[
            {"name": "my_tool", "description": "A tool."},
        ])

        with patch("mcp_plugin.process.MCPWrapperProcess") as MockProc:
            MockProc.return_value = mock_process
            mock_process.start = AsyncMock()
            mock_process.create_client = AsyncMock(return_value=mock_client)

            with patch("slife.mcp.tool_adapter.create_proxy_tools") as mock_create:
                mock_tool = MagicMock()
                mock_create.return_value = [mock_tool]

                await lifecycle.spawn(
                    module="mcp_plugin.server",
                )

        import os
        assert os.environ.get("SLIFE_TEST_PLUGIN_PORT") == "9999"
        # Clean up
        os.environ.pop("SLIFE_TEST_PLUGIN_PORT", None)

        # Readiness: create_client() completed the MCP initialize handshake,
        # which IS the ready declaration (no __ready probe).
        assert lifecycle.ready is True
        assert lifecycle.ready_state == "ready"

    @pytest.mark.asyncio
    async def test_spawn_dash_name_writes_normalised_env_key(self, mock_service):
        """A dash-named plugin (local-embed) publishes its port under the
        canonical underscore key — no ``SLIFE_LOCAL-EMBED_PORT``."""
        lc = PluginLifecycle("local-embed", mock_service)
        mock_process = MagicMock()
        mock_process.port = 8000
        mock_client = MagicMock()
        mock_client.list_tools = AsyncMock(return_value=[
            {"name": "embed_status", "description": "Status."},
        ])

        with patch("mcp_plugin.process.MCPWrapperProcess") as MockProc:
            MockProc.return_value = mock_process
            mock_process.start = AsyncMock()
            mock_process.create_client = AsyncMock(return_value=mock_client)

            with patch("slife.mcp.tool_adapter.create_proxy_tools") as mock_create:
                mock_tool = MagicMock()
                mock_create.return_value = [mock_tool]

                await lc.spawn(module="local_embed.server")

        import os
        assert os.environ.get("SLIFE_LOCAL_EMBED_PORT") == "8000"
        assert os.environ.get("SLIFE_LOCAL-EMBED_PORT") is None
        # Clean up
        os.environ.pop("SLIFE_LOCAL_EMBED_PORT", None)

    @pytest.mark.asyncio
    async def test_spawn_registers_tools(self, lifecycle, mock_service):
        """spawn() registers LLM-visible tools in the service registry."""
        mock_process = MagicMock()
        mock_process.port = 8888
        mock_client = MagicMock()
        mock_client.list_tools = AsyncMock(return_value=[
            {"name": "visible_tool", "description": "For LLM."},
            {"name": "__internal_tool", "description": "Internal (__ prefix)."},
        ])

        with patch("mcp_plugin.process.MCPWrapperProcess") as MockProc:
            MockProc.return_value = mock_process
            mock_process.start = AsyncMock()
            mock_process.create_client = AsyncMock(return_value=mock_client)

            with patch("slife.mcp.tool_adapter.create_proxy_tools") as mock_create:
                mock_tool = MagicMock()
                mock_create.return_value = [mock_tool]

                await lifecycle.spawn(
                    module="mcp_plugin.server",
                )

        assert mock_service.tool_registry.register.called
        mock_service.tool_registry.register.assert_called_with(mock_tool)
        assert lifecycle.client is mock_client
        assert lifecycle.process is mock_process
        assert lifecycle.port == 8888

        import os
        os.environ.pop("SLIFE_TEST_PLUGIN_PORT", None)

    @pytest.mark.asyncio
    async def test_spawn_failure_resets_process_and_stops_child(self, lifecycle):
        """REVIEW M2 — a failed spawn must not leave the lifecycle pointing at
        a live-but-unconnected child (the watchdog would block on its wait()
        forever); it resets and stops the process before re-raising."""
        import os

        mock_process = MagicMock()
        mock_process.port = 7777
        mock_process.start = AsyncMock()
        mock_process.create_client = AsyncMock(
            side_effect=ConnectionError("refused"),
        )
        mock_process.stop = AsyncMock()

        with patch("mcp_plugin.process.MCPWrapperProcess") as MockProc:
            MockProc.return_value = mock_process
            with pytest.raises(ConnectionError):
                await lifecycle.spawn(module="mcp_plugin.server")

        assert lifecycle.process is None
        assert lifecycle.client is None
        assert lifecycle.port == 0
        mock_process.stop.assert_awaited_once()
        os.environ.pop("SLIFE_TEST_PLUGIN_PORT", None)


# ── mark_initialized (readiness = MCP initialize handshake) ──────────────


class TestPluginLifecycleMarkInitialized:
    """Readiness (MCP plugin contract): mark_initialized() records that the
    initialize handshake completed — there is no __ready probe anymore."""

    def test_marks_ready(self, lifecycle):
        lifecycle.client = MagicMock()  # connect()/initialize() done by spawn
        lifecycle.mark_initialized()
        assert lifecycle.ready is True
        assert lifecycle.ready_state == "ready"
        assert "initialized" in lifecycle.ready_detail

    def test_idempotent(self, lifecycle):
        lifecycle.mark_initialized()
        lifecycle.mark_initialized()
        assert lifecycle.ready is True
        assert lifecycle.ready_state == "ready"


# ── connect_http ──────────────────────────────────────────────────────────


class TestPluginLifecycleConnectHttp:
    """Tests for PluginLifecycle.connect_http()."""

    @pytest.mark.asyncio
    async def test_connect_http(self, lifecycle):
        """connect_http creates client and sets port."""
        with patch("slife.agent.plugins.MCPClient") as MockClient:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock()
            MockClient.return_value = mock_client

            await lifecycle.connect_http(port=7777)

            assert lifecycle.port == 7777
            assert lifecycle.client is mock_client
            mock_client.connect.assert_called_once_with("http://127.0.0.1:7777/mcp")


# ── stop ──────────────────────────────────────────────────────────────────


class TestPluginLifecycleStop:
    """Tests for PluginLifecycle.stop()."""

    @pytest.mark.asyncio
    async def test_stop_idempotent_when_nothing(self, lifecycle):
        """stop() on a fresh lifecycle is a no-op (no error)."""
        await lifecycle.stop()
        # Should not raise

    @pytest.mark.asyncio
    async def test_stop_disconnects_client(self, lifecycle):
        """stop() disconnects connected client."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        lifecycle.client = mock_client

        await lifecycle.stop()
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_skips_disconnected_client(self, lifecycle):
        """stop() skips client that is not connected."""
        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.disconnect = AsyncMock()
        lifecycle.client = mock_client

        await lifecycle.stop()
        mock_client.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_stops_process(self, lifecycle):
        """stop() stops the child process."""
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        lifecycle.process = mock_process

        await lifecycle.stop()
        mock_process.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_disconnect_error_does_not_crash(self, lifecycle):
        """stop() swallows disconnect errors."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
        lifecycle.client = mock_client

        # Should not raise
        await lifecycle.stop()

    @pytest.mark.asyncio
    async def test_stop_process_error_does_not_crash(self, lifecycle):
        """stop() swallows process stop errors."""
        mock_process = MagicMock()
        mock_process.stop = AsyncMock(side_effect=RuntimeError("boom"))
        lifecycle.process = mock_process

        # Should not raise
        await lifecycle.stop()

    @pytest.mark.asyncio
    async def test_stop_with_poll_task(self, lifecycle):
        """stop() with has_poll_task=True cancels the poll task."""
        async def poll_loop():
            while True:
                await asyncio.sleep(0.1)

        lifecycle.poll_task = asyncio.create_task(poll_loop())

        await lifecycle.stop(has_poll_task=True)
        assert lifecycle.poll_task is None

    @pytest.mark.asyncio
    async def test_stop_clears_client_and_process(self, lifecycle):
        """After stop(), client and process are None."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        lifecycle.client = mock_client
        lifecycle.process = mock_process

        await lifecycle.stop()
        assert lifecycle.client is None
        assert lifecycle.process is None


# ── kill ──────────────────────────────────────────────────────────────────


class TestPluginLifecycleKill:
    """Tests for PluginLifecycle.kill()."""

    def test_kill_no_process_no_error(self, lifecycle):
        """kill() with no process is a no-op."""
        lifecycle.kill()  # Should not raise

    def test_kill_terminates_process(self, lifecycle):
        """kill() terminates a running process."""
        mock_subprocess = MagicMock()
        mock_subprocess.terminate = MagicMock()
        mock_subprocess.returncode = None

        mock_process = MagicMock()
        mock_process._process = mock_subprocess
        lifecycle.process = mock_process

        lifecycle.kill()
        mock_subprocess.terminate.assert_called_once()

    def test_kill_terminate_error_does_not_crash(self, lifecycle):
        """kill() swallows terminate errors."""
        mock_subprocess = MagicMock()
        mock_subprocess.returncode = None
        mock_subprocess.terminate = MagicMock(side_effect=RuntimeError("boom"))

        mock_process = MagicMock()
        mock_process._process = mock_subprocess
        lifecycle.process = mock_process

        lifecycle.kill()  # Should not raise

    def test_kill_already_exited_is_noop(self, lifecycle):
        """kill() skips a process that already exited (returncode set)."""
        mock_subprocess = MagicMock()
        mock_subprocess.terminate = MagicMock()
        mock_subprocess.returncode = 0

        mock_process = MagicMock()
        mock_process._process = mock_subprocess
        lifecycle.process = mock_process

        lifecycle.kill()
        mock_subprocess.terminate.assert_not_called()

    def test_kill_no_underlying_process(self, lifecycle):
        """kill() handles process wrapper with no _process attribute."""
        mock_process = MagicMock(spec=[])  # no _process
        lifecycle.process = mock_process

        lifecycle.kill()  # Should not raise — getattr returns None


# ── Watchdog restart (REVIEW H5) ─────────────────────────────────────────


class TestWatchdogRestart:
    """Watchdog auto-restart behavior — memdb fallback + retry-on-failure."""

    @staticmethod
    def _dead_process(returncode=1):
        """A process wrapper whose child exits with *returncode*."""
        proc = MagicMock()
        proc._process.wait = AsyncMock(return_value=returncode)
        return proc

    @staticmethod
    def _living_process():
        """A process wrapper whose child stays alive (blocks until cancelled)."""
        proc = MagicMock()
        proc._process.wait = lambda: asyncio.Future()  # never resolves on its own
        return proc

    @pytest.mark.asyncio
    async def test_restart_via_fallback_spawn_without_restart_cb(self, lifecycle):
        """A plugin spawned without restart_cb (like memdb) is restarted via spawn."""
        new_proc = self._living_process()
        lifecycle.process = self._dead_process()
        lifecycle._module = "slife.plugins.memdb.server"

        spawned = []
        async def fake_spawn(module):
            spawned.append(module)
            lifecycle.process = new_proc

        with patch.object(lifecycle, "spawn", new=fake_spawn):
            task = asyncio.create_task(lifecycle._watchdog_loop())
            try:
                for _ in range(100):
                    if lifecycle.process is new_proc:
                        break
                    await asyncio.sleep(0.01)
                # The watchdog respawned via the fallback path (no restart_cb).
                assert lifecycle.process is new_proc
                assert spawned == ["slife.plugins.memdb.server"]
                # A successful restart resets the counters.
                assert lifecycle._restart_count == 0
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    @pytest.mark.asyncio
    async def test_failed_restart_retries_with_backoff(self, lifecycle, monkeypatch):
        """A failed restart backs off and retries instead of killing the watchdog."""
        import slife.agent.plugins as plugin_mod

        monkeypatch.setattr(plugin_mod, "_WATCHDOG_BACKOFF_INITIAL", 0.01)
        monkeypatch.setattr(plugin_mod, "_WATCHDOG_BACKOFF_MULTIPLIER", 2.0)

        new_proc = self._living_process()
        lifecycle.process = self._dead_process()
        lifecycle._module = "slife.plugins.memdb.server"

        attempts = 0
        async def flaky_spawn(module):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("spawn boom")
            lifecycle.process = new_proc

        with patch.object(lifecycle, "spawn", new=flaky_spawn):
            task = asyncio.create_task(lifecycle._watchdog_loop())
            try:
                for _ in range(200):
                    if lifecycle.process is new_proc:
                        break
                    await asyncio.sleep(0.01)
                # First attempt failed, watchdog retried and succeeded.
                assert lifecycle.process is new_proc
                assert attempts == 2
                assert lifecycle._restart_count == 0
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
