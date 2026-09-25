"""Tests for the a2a plugin server — standard A2A mesh as a replaceable plugin.

Mocks the A2AMesh (no broker needed) and exercises the MCP tool
functions directly: the standard operations (SendMessage, CancelTask,
discovery, responder completion, broadcast), the harness drain tools, and
the __check health probe.
"""

import json
import pytest; pytestmark = pytest.mark.unit


from unittest.mock import AsyncMock, MagicMock, patch

import slife.plugins.a2a.server as plugin
from slife.a2a.config import A2AConfig


@pytest.fixture(autouse=True)
def _fresh_plugin_state():
    """The mesh client is a module-level singleton shared across test files —
    isolate it per test."""
    plugin._client = None
    yield
    plugin._client = None


def _config(enabled: bool = True) -> A2AConfig:
    return A2AConfig(
        enabled=enabled, agent_name="self-1",
        broker_host="localhost", broker_port=1883,
    )


def _fake_client():
    """A mocked A2AMesh returning canned values."""
    client = MagicMock()
    client.agent_name = "self-1"
    client.status = "online"
    client.is_connected = True
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_message = AsyncMock(return_value="corr-1")
    client.cancel_task = AsyncMock(return_value="cancelled")
    client.broadcast = AsyncMock()
    client.complete_task = MagicMock(return_value="ok")
    client.list_agents = MagicMock(return_value=[
        MagicMock(agent_name="self-1", status="online"),
        MagicMock(agent_name="peer-1", status="online"),
    ])
    return client


class TestPluginTools:
    @pytest.mark.asyncio
    async def test_a2a_send_message_returns_task_id_immediately(self):
        """SendMessage is async-native: returns the task_id, never waits."""
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_send_message("peer-1", "hello")
        assert result.startswith("corr-1")
        assert "The result will arrive in your inbox" in result
        client.send_message.assert_called_once_with(
            "peer-1", "hello", message_type="task_request",
        )
        # No timeout parameter — the tool never blocks.
        import inspect
        assert "timeout" not in inspect.signature(plugin.a2a_send_message).parameters

    @pytest.mark.asyncio
    async def test_a2a_cancel_task_forwards(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_cancel_task("peer-1", "corr-1")
        assert result == "cancelled"
        client.cancel_task.assert_called_once_with("peer-1", "corr-1")

    @pytest.mark.asyncio
    async def test_a2a_list_agents_own_card_first(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = json.loads(await plugin.a2a_list_agents())
        assert result[0]["agent_name"] == "self-1"
        assert result[1]["agent_name"] == "peer-1"
        assert {c["status"] for c in result} == {"online"}

    @pytest.mark.asyncio
    async def test_a2a_broadcast_fire_and_forget(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_broadcast("all hands on deck")
        assert "Broadcast sent" in result
        client.broadcast.assert_awaited_once_with("all hands on deck")

    @pytest.mark.asyncio
    async def test_a2a_send_message_task_response_completes_bridge(self):
        """message_type='task_response' completes the inbound task named by
        task_id — it does NOT send a new task."""
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_send_message(
                "peer-1", "the answer", message_type="task_response", task_id="t-1",
            )
        assert result == "ok"
        client.complete_task.assert_called_once_with("t-1", "the answer", False)
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a2a_send_message_task_response_requires_id(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_send_message(
                "peer-1", "x", message_type="task_response",
            )
        assert result.startswith("Error")
        assert "task_id required" in result
        client.complete_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_a2a_send_message_message_type_no_task(self):
        """message_type='message' sends a bare conversation — no task."""
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_send_message(
                "peer-1", "hi there", message_type="message",
            )
        assert result.startswith("corr-1")
        assert "No task created" in result
        client.send_message.assert_called_once_with(
            "peer-1", "hi there", message_type="message",
        )

    @pytest.mark.asyncio
    async def test_a2a_send_message_invalid_type(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await plugin.a2a_send_message(
                "peer-1", "x", message_type="nope",
            )
        assert result.startswith("Error")
        client.send_message.assert_not_called()


class TestDrain:
    """Inbound queues → __a2a_drain_incoming, FIFO + cleared."""

    @pytest.mark.asyncio
    async def test_drain_returns_queued_entries_and_clears(self):
        plugin._on_inbound_task("jack", "do X", "t-1")
        plugin._on_inbound_task("jill", "do Y", "t-2")
        plugin._on_broadcast_event("bill", "hi all")
        plugin._on_agent_change(
            MagicMock(agent_name="peer-1", status="online"), "online",
        )
        plugin._on_peer_cancel("t-1", "jack")
        plugin._on_task_completion("c-1", "result", False, "peer-1")
        plugin._on_task_completion("c-2", "", True, "peer-2")

        data = json.loads(await getattr(plugin, "__a2a_drain_incoming")())

        assert data["tasks"] == [
            {"type": "task", "source": "jack", "content": "do X", "task_id": "t-1", "kind": "task"},
            {"type": "task", "source": "jill", "content": "do Y", "task_id": "t-2", "kind": "task"},
        ]
        assert data["events"] == [
            {"type": "event", "source": "bill", "content": "hi all"},
        ]
        assert data["presence"] == [{
            "type": "presence", "event": "online",
            "card": {"agent_name": "peer-1", "status": "online"},
        }]
        assert data["cancellations"] == [
            {"type": "cancel", "corr_id": "t-1", "peer": "jack"},
        ]
        assert data["task_completions"] == [
            {"corr_id": "c-1", "result": "result", "cancelled": False, "peer": "peer-1", "kind": "task"},
            {"corr_id": "c-2", "result": "", "cancelled": True, "peer": "peer-2", "kind": "task"},
        ]

        # Second drain is empty — the queues were cleared.
        again = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert all(again[k] == [] for k in (
            "tasks", "events", "presence", "cancellations", "task_completions",
        ))

    @pytest.mark.asyncio
    async def test_drain_reports_orphaned_tasks(self, monkeypatch, tmp_path):
        """``stale_tasks`` rides the drain — read off the live mesh when there
        is one, and off disk otherwise, so a restart still reports its orphans
        when the broker never came back up."""
        monkeypatch.setenv("A2A_INBOUND_FILE", str(tmp_path / "inbound.json"))
        from slife.a2a.inbound_store import InboundStore
        InboundStore().add("ec604319", "jack")  # left in flight, then "restart"

        data = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert [t["task_id"] for t in data["stale_tasks"]] == ["ec604319"]
        assert data["stale_tasks"][0]["peer"] == "jack"

        # It is state, not a queue: a second drain still reports it.
        again = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert [t["task_id"] for t in again["stale_tasks"]] == ["ec604319"]

    @pytest.mark.asyncio
    async def test_drain_orphans_come_from_the_live_mesh(self, monkeypatch):
        """With a mesh connected, the drain reads ITS store — the plugin and
        the mesh must never disagree about what is still answerable."""
        mesh = MagicMock()
        mesh.stale_inbound.return_value = [
            {"task_id": "t-1", "peer": "jack", "since": "2026-01-01T00:00:00Z"},
        ]
        monkeypatch.setattr(plugin, "_client", mesh)

        data = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert data["stale_tasks"] == mesh.stale_inbound.return_value

    @pytest.mark.asyncio
    async def test_drain_task_always_carries_task_id(self):
        """Every inbound A2A exchange is a task with a task_id (no message
        kind, no reply_to stamping)."""
        plugin._on_inbound_task("jack", "do X", "t-9")
        data = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        task = data["tasks"][0]
        assert task["task_id"] == "t-9"
        assert task["kind"] == "task"
        assert "reply_to" not in task


class TestA2aStatusTool:
    """__check — the system_health probe, read-only, no connect side effect."""

    @pytest.mark.asyncio
    async def test_disconnected_when_no_client(self):
        with patch.object(plugin, "_load_config", return_value=_config()):
            data = json.loads(await getattr(plugin, "__check")())
        assert data["connected"] is False
        assert data["agent_name"] == ""
        assert data["broker"] == "localhost:1883"

    @pytest.mark.asyncio
    async def test_connected_with_peers(self):
        plugin._client = _fake_client()
        with patch.object(plugin, "_load_config", return_value=_config()):
            data = json.loads(await getattr(plugin, "__check")())
        assert data["connected"] is True
        assert data["agent_name"] == "self-1"
        assert data["status"] == "online"
        assert data["peers"] == [
            {"agent_name": "peer-1", "status": "online"},
        ]

    @pytest.mark.asyncio
    async def test_status_does_not_trigger_connect(self):
        """__check with no client must never attempt a connect."""
        with patch.object(plugin, "_ensure_connected", AsyncMock()) as ensure:
            with patch.object(plugin, "_load_config", return_value=_config()):
                await getattr(plugin, "__check")()
            ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_reports_queued_counts(self):
        plugin._on_inbound_task("jack", "x", "t-1")
        plugin._on_broadcast_event("bill", "e")
        plugin._client = _fake_client()
        with patch.object(plugin, "_load_config", return_value=_config()):
            data = json.loads(await getattr(plugin, "__check")())
        assert data["queued"]["tasks"] == 1
        assert data["queued"]["events"] == 1


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_connected_creates_and_connections_mesh(self):
        client = _fake_client()
        with patch.object(plugin, "A2AMesh", return_value=client) as mock_cls:
            with patch.object(plugin, "_load_config", return_value=_config()):
                result = await plugin._ensure_connected()
        assert result is client
        mock_cls.assert_called_once()
        client.connect.assert_awaited_once()
        assert plugin._client is client
        # The plugin's inbound queues are wired as mesh callbacks.
        assert client.on_inbound_task is plugin._on_inbound_task
        assert client.on_task_completion is plugin._on_task_completion
        assert client.on_agent_change is plugin._on_agent_change
        assert client.on_peer_cancel is plugin._on_peer_cancel
        assert client.on_broadcast_event is plugin._on_broadcast_event

    @pytest.mark.asyncio
    async def test_ensure_connected_failure_cleans_up(self):
        """A failed connect must not leak a half-started mesh (retry-safe)."""
        client = _fake_client()
        client.connect = AsyncMock(side_effect=RuntimeError("refused"))
        with patch.object(plugin, "A2AMesh", return_value=client):
            with patch.object(plugin, "_load_config", return_value=_config()):
                with pytest.raises(RuntimeError, match="refused"):
                    await plugin._ensure_connected()
        client.disconnect.assert_awaited()
        assert plugin._client is None

    @pytest.mark.asyncio
    async def test_ensure_connected_when_not_enabled(self):
        with patch.object(plugin, "_load_config", return_value=_config(enabled=False)):
            with pytest.raises(RuntimeError, match="not enabled"):
                await plugin._ensure_connected()

    @pytest.mark.asyncio
    async def test_lifespan_eager_connects_and_disconnects(self):
        client = _fake_client()
        plugin._client = client
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            async with plugin._a2a_lifespan(None):
                assert client.connect is not None
        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ensure_connected_requires_lock_cached(self):
        """A connected client is returned without re-connecting."""
        client = _fake_client()
        plugin._client = client
        result = await plugin._ensure_connected()
        assert result is client
        client.connect.assert_not_awaited()


class TestToolVisibility:
    @pytest.mark.asyncio
    async def test_a2a_tools_visible_no_internal_prefix(self):
        tools = await plugin.mcp._list_tools()
        by_name = {t.name: t.description for t in tools}
        for name in (
            "a2a_send_message", "a2a_cancel_task", "a2a_list_agents",
            "a2a_broadcast",
        ):
            assert name in by_name, f"{name} missing from plugin tools"
            assert not name.startswith("__"), name
        # The non-standard surface is gone.
        assert "a2a_send_task" not in by_name
        assert "a2a_send_message_async" not in by_name
        assert "a2a_get_task_result" not in by_name

    @pytest.mark.asyncio
    async def test_internal_tools_prefixed_double_underscore(self):
        tools = await plugin.mcp._list_tools()
        names = {t.name for t in tools}
        for name in ("__a2a_drain_incoming", "__check"):
            assert name in names, f"{name} missing from plugin tools"
        # __a2a_dispatch_result (dead wire publish) is gone.
        assert "__a2a_dispatch_result" not in names