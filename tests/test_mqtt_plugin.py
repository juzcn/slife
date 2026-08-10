"""Tests for the a2a plugin server — mesh channel as a replaceable plugin.

Mocks the A2AClient (no broker needed) and exercises the MCP tool
functions directly: sending, listing, broadcasting, and the harness
drain/dispatch tools.
"""

import json
import pytest; pytestmark = pytest.mark.unit


from unittest.mock import AsyncMock, MagicMock, patch

import slife.plugins.mqtt.server as plugin


def _fake_client():
    """A mocked A2AClient returning canned values."""
    client = MagicMock()
    client.is_connected = True
    client.send_task = AsyncMock(return_value="result-text")
    client.send_task_async = AsyncMock(return_value="corr-1")
    client.list_agents = AsyncMock(return_value=[
        MagicMock(agent_id="peer-1", display_name="Peer One", status="idle"),
    ])
    client.get_task_result = MagicMock(return_value="done")
    client.cancel_task = AsyncMock(return_value=True)
    client.broadcast = AsyncMock(return_value=["peer-1:corr-1"])
    client.subscribe_task = AsyncMock(return_value="sub-result")
    client.get_agent_card = MagicMock(return_value=MagicMock(
        agent_id="peer-1", display_name="Peer One", status="idle",
    ))
    client.list_tasks = MagicMock(return_value=[])
    client.publish_message = AsyncMock()
    return client


class TestPluginTools:
    @pytest.mark.asyncio
    async def test__send_task(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "__send_task")("peer-1", "hello")
        assert result == "result-text"
        client.send_task.assert_called_once()

    @pytest.mark.asyncio
    async def test__send_task_async(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "__send_task_async")("peer-1", "hello")
        assert result == "corr-1"

    @pytest.mark.asyncio
    async def test__list_agents_serializes_cards(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "__list_agents")()
        data = json.loads(result)
        assert data[0]["agent_id"] == "peer-1"
        assert data[0]["display_name"] == "Peer One"

    @pytest.mark.asyncio
    async def test__broadcast(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "__broadcast")("task")
        assert "peer-1:corr-1" in result

    @pytest.mark.asyncio
    async def test__get_task_result_pending(self):
        client = _fake_client()
        client.get_task_result = MagicMock(return_value=None)
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            assert await getattr(plugin, "__get_task_result")("x") == "pending"

    @pytest.mark.asyncio
    async def test__cancel_task(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            assert await getattr(plugin, "__cancel_task")("peer-1", "corr-1") == "cancelled"


class TestHarnessTools:
    @pytest.mark.asyncio
    async def test_drain_returns_queued_tasks_and_presence(self):
        plugin._inbound_tasks.clear()
        plugin._presence_events.clear()
        from slife.a2a.identity import AgentMessage

        await plugin._on_incoming_task(AgentMessage(
            source="peer-1", content="do this",
            reply_to="Slife/slife/tasks/result", correlation_id="cid-1",
        ))
        await plugin._on_agent_change(
            MagicMock(agent_id="peer-1", display_name="Peer One", status="idle"),
            "online",
        )

        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert len(out["tasks"]) == 1
        assert out["tasks"][0]["content"] == "do this"
        assert out["tasks"][0]["correlation_id"] == "cid-1"
        assert len(out["presence"]) == 1
        assert out["presence"][0]["event"] == "online"
        # Queue is cleared after drain
        assert plugin._inbound_tasks == []
        assert plugin._presence_events == []

    @pytest.mark.asyncio
    async def test_dispatch_result_publishes(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            await getattr(plugin, "__a2a_dispatch_result")("Slife/x/tasks/result", "cid-1", "reply")
        client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_harness_tools_marked_harness_only(self):
        """The filter keys off 'harness-only' in the registered description."""
        tools = await plugin.mcp._list_tools()
        by_name = {t.name: t.description for t in tools}
        assert "harness-only" in by_name["__a2a_drain_incoming"].lower()
        assert "harness-only" in by_name["__a2a_dispatch_result"].lower()


class TestConfig:
    def test_load_config_from_env(self, monkeypatch):
        import json as _json
        from slife.a2a.config import A2AConfig
        cfg = A2AConfig(enabled=True, agent_id="slife", broker_host="localhost", broker_port=1883)
        monkeypatch.setenv("SLIFE_MQTT_CONFIG", _json.dumps({
            "enabled": True, "agent_id": "slife", "agent_name": "",
            "transport": "mqtt", "broker_host": "localhost", "broker_port": 1883,
            "http_host": "127.0.0.1", "http_port": 0,
            "heartbeat_interval": 15, "heartbeat_timeout": 45, "task_timeout": 120,
        }))
        loaded = plugin._load_config()
        assert loaded.agent_id == "slife"
        assert loaded.broker_port == 1883
