"""Tests for the a2a plugin server — mesh channel as a replaceable plugin.

Mocks the A2AClient (no broker needed) and exercises the MCP tool
functions directly: sending, listing, broadcasting, and the harness
drain/dispatch tools.
"""

import json
import pytest; pytestmark = pytest.mark.unit


from unittest.mock import AsyncMock, MagicMock, patch

import slife.plugins.a2a.server as plugin


@pytest.fixture(autouse=True)
def _fresh_task_store():
    """The a2a task store is a module-level singleton shared across test
    files — isolate it per test so this module's peer-attribution assertions
    never read a record leaked from an earlier module (e.g. test_a2a_client)."""
    from slife.a2a.task_store import clear_store
    clear_store()
    yield
    clear_store()


def _fake_client():
    """A mocked A2AClient returning canned values."""
    client = MagicMock()
    client.is_connected = True
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_task = AsyncMock(return_value="result-text")
    client.send_task_async = AsyncMock(return_value="corr-1")
    client.list_agents = AsyncMock(return_value=[
        MagicMock(agent_name="peer-1", status="idle"),
    ])
    client.own_card = MagicMock(return_value=MagicMock(
        agent_name="self-1", status="idle",
    ))
    client.get_task_result = MagicMock(return_value="done")
    client.cancel_task = AsyncMock(return_value="cancelled")
    client.broadcast = AsyncMock(return_value=["peer-1:corr-1"])
    client.get_agent_card = MagicMock(return_value=MagicMock(
        agent_name="peer-1", status="idle",
    ))
    client.list_tasks = MagicMock(return_value=[])
    client.publish_message = AsyncMock()
    return client


class TestPluginTools:
    @pytest.mark.asyncio
    async def test_a2a_send_task(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_task")("peer-1", "hello")
        assert result == "result-text"
        client.send_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_a2a_send_task_timeout_forwarded(self):
        """A positive per-call ``timeout`` is honored (native-timeout
        contract: the plugin enforces the deadline instead of the sender's
        harness killing the MCP call)."""
        from slife.a2a.identity import AgentName

        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_task")(
                "peer-1", "hello", timeout=30,
            )
        assert result == "result-text"
        client.send_task.assert_called_once_with(
            AgentName("peer-1"), "hello", timeout=30,
        )

    @pytest.mark.asyncio
    async def test_a2a_send_task_timeout_default_on_nonpositive(self):
        """timeout ≤ 0 selects the plugin default — no timeout kwarg is sent
        (the client then applies its ``task_timeout``), matching the loop's
        ``_timeout: 0`` convention."""
        from unittest.mock import call

        from slife.a2a.identity import AgentName

        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            await getattr(plugin, "a2a_send_task")("peer-1", "hello", timeout=0)
        client.send_task.assert_called_once_with(AgentName("peer-1"), "hello")

    @pytest.mark.asyncio
    async def test_a2a_send_task_async(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_task_async")("peer-1", "hello")
        assert result == "corr-1"

    @pytest.mark.asyncio
    async def test_a2a_send_task_async_poll_mode(self):
        """mode='poll' marks the task so its completion is not auto-pushed."""
        client = _fake_client()
        plugin._poll_tasks.clear()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_task_async")(
                "peer-1", "hello", mode="poll",
            )
        assert result.startswith("corr-1")
        assert "a2a_get_task_result" in result
        assert "corr-1" in plugin._poll_tasks

    @pytest.mark.asyncio
    async def test_a2a_send_task_async_rejects_invalid_mode(self):
        plugin._poll_tasks.clear()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=_fake_client())):
            result = await getattr(plugin, "a2a_send_task_async")(
                "peer-1", "hello", mode="nope",
            )
        assert result.startswith("Error")
        assert plugin._poll_tasks == set()

    @pytest.mark.asyncio
    async def test_a2a_list_agents_serializes_cards(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_list_agents")()
        data = json.loads(result)
        # Own card is first — the agent must see itself to tell it apart
        # from a same-named peer (e.g. a second "slife" process).
        assert data[0]["agent_name"] == "self-1"
        assert data[0]["status"] == "idle"
        assert data[1]["agent_name"] == "peer-1"
        assert data[1]["status"] == "idle"

    @pytest.mark.asyncio
    async def test_a2a_broadcast(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_broadcast")("task")
        assert "peer-1:corr-1" in result

    @pytest.mark.asyncio
    async def test_a2a_get_task_result_pending(self):
        client = _fake_client()
        client.get_task_result = MagicMock(return_value=None)
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            assert await getattr(plugin, "a2a_get_task_result")("peer-1", "x") == "pending"

    @pytest.mark.asyncio
    async def test_a2a_cancel_task(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            assert await getattr(plugin, "a2a_cancel_task")("peer-1", "corr-1") == "cancelled"


class TestA2aMessageTools:
    """Stateless message tools — mirror the task tools but never write the
    task store (a2a_list_tasks / a2a_cancel_task don't see messages)."""

    @pytest.mark.asyncio
    async def test_send_message_sync_stateless(self):
        """a2a_send_message sends with record=False and returns the reply."""
        from slife.a2a.identity import AgentName

        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_message")("peer-1", "hello")
        assert result == "result-text"
        client.send_task.assert_called_once()
        assert client.send_task.call_args.args == (AgentName("peer-1"), "hello")
        kwargs = client.send_task.call_args.kwargs
        assert kwargs["record"] is False
        assert callable(kwargs["on_abandoned"])

    @pytest.mark.asyncio
    async def test_send_message_timeout_forwarded(self):
        """a2a_send_message honors a per-call timeout, preserving the
        stateless record=False shape."""
        from slife.a2a.identity import AgentName

        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_message")(
                "peer-1", "hello", timeout=30,
            )
        assert result == "result-text"
        client.send_task.assert_called_once()
        assert client.send_task.call_args.args == (AgentName("peer-1"), "hello")
        kwargs = client.send_task.call_args.kwargs
        assert kwargs["record"] is False
        assert kwargs["timeout"] == 30
        assert callable(kwargs["on_abandoned"])

    @pytest.mark.asyncio
    async def test_send_message_sync_degrade_tracks_late_reply_as_message(self):
        """B1 regression — a sync stateless send whose wait degrades (B1) is
        registered in ``_message_sends``, so its late reply auto-pushes as a
        ``kind="message"`` reply with the peer preserved — never a task
        completion whose "peer" is the corr_id."""
        from slife.a2a.task_store import clear_store

        plugin._message_sends.clear()
        plugin._task_completions.clear()

        async def _degrading_send(target, task, **kwargs):
            # The real client's auto-degrade: drop the waiter, then fire the
            # abandonment hook so the plugin keeps tracking the late reply.
            cb = kwargs.get("on_abandoned")
            if cb is not None:
                await cb("corr-9")
            return (
                f"Task to '{target}' timed out after 0.05s — auto-degraded "
                f"to async … task_id: corr-9"
            )

        client = _fake_client()
        client.send_task = AsyncMock(side_effect=_degrading_send)
        try:
            with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
                result = await getattr(plugin, "a2a_send_message")(
                    "peer-1", "hello", timeout=0.05,
                )
            assert "auto-degraded" in result

            # The degraded wait registered the peer, mirroring the async path.
            assert plugin._message_sends.get("corr-9") == "peer-1"

            await plugin._on_task_result("corr-9", "the answer", False)
            out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
            assert out["task_completions"] == [{
                "corr_id": "corr-9", "result": "the answer",
                "cancelled": False, "peer": "peer-1", "kind": "message",
            }]
            assert plugin._message_sends == {}
        finally:
            plugin._message_sends.clear()
            plugin._task_completions.clear()
            clear_store()

    @pytest.mark.asyncio
    async def test_send_message_async_auto_completion_kind_message(self):
        """auto-mode message — the reply completes as kind='message' with the
        peer preserved (there is no task-store record to look it up)."""
        from slife.a2a.identity import AgentName
        from slife.a2a.task_store import get_store, clear_store

        clear_store()
        plugin._message_sends.clear()
        plugin._poll_tasks.clear()
        plugin._task_completions.clear()
        client = _fake_client()
        try:
            with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
                result = await getattr(plugin, "a2a_send_message_async")(
                    "peer-1", "hello",
                )
            assert result == "corr-1"
            client.send_task_async.assert_called_once_with(
                AgentName("peer-1"), "hello", record=False,
            )
            assert plugin._message_sends.get("corr-1") == "peer-1"
            assert "corr-1" not in plugin._poll_tasks
            assert get_store().list_tasks() == []

            await plugin._on_task_result("corr-1", "the answer", False)
            out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
            assert out["task_completions"] == [{
                "corr_id": "corr-1", "result": "the answer",
                "cancelled": False, "peer": "peer-1", "kind": "message",
            }]
            assert plugin._message_sends == {}
        finally:
            clear_store()

    @pytest.mark.asyncio
    async def test_send_message_async_poll_suppresses_push(self):
        """poll-mode message — no auto-push; the reply is left for
        a2a_get_task_result (client-side), and the peer map is consumed."""
        plugin._message_sends.clear()
        plugin._poll_tasks.clear()
        plugin._task_completions.clear()
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_send_message_async")(
                "peer-1", "hello", mode="poll",
            )
        assert result.startswith("corr-1")
        assert "a2a_get_task_result" in result
        assert "corr-1" in plugin._poll_tasks
        assert plugin._message_sends.get("corr-1") == "peer-1"

        await plugin._on_task_result("corr-1", "the answer", False)
        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert out["task_completions"] == []
        assert "corr-1" not in plugin._poll_tasks
        assert plugin._message_sends == {}


class TestIncomingCancel:
    """REVIEW C5 — inbound CancelTask drops the queued task (replying a
    CANCELLED result) and queues a cancel for the harness."""

    @pytest.mark.asyncio
    async def test_drops_queued_task_and_replies_cancelled(self):
        plugin._inbound_tasks.clear()
        plugin._cancellations.clear()
        plugin._inbound_tasks.append({
            "type": "task", "source": "peer-1", "content": "do X",
            "reply_to": "Slife/peer-1/tasks/result", "correlation_id": "cid-1",
        })
        client = _fake_client()
        plugin._client = client
        try:
            await plugin._on_incoming_cancel("cid-1")
        finally:
            plugin._client = None

        assert plugin._inbound_tasks == []  # dropped, never reaches the loop
        assert plugin._cancellations == [{"type": "cancel", "corr_id": "cid-1"}]
        # A CANCELLED result was published back to the waiting sender.
        args = client.publish_message.call_args
        assert args.args[0] == "Slife/peer-1/tasks/result"
        env = json.loads(args.args[1])
        assert env["result"]["task"]["status"]["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_queues_cancel_when_task_already_drained(self):
        plugin._inbound_tasks.clear()
        plugin._cancellations.clear()
        plugin._client = None

        await plugin._on_incoming_cancel("cid-already-running")

        assert plugin._cancellations == [
            {"type": "cancel", "corr_id": "cid-already-running"},
        ]

    @pytest.mark.asyncio
    async def test_drain_includes_cancellations(self):
        plugin._inbound_tasks.clear()
        plugin._presence_events.clear()
        plugin._cancellations.clear()
        plugin._cancellations.append({"type": "cancel", "corr_id": "cid-1"})

        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())

        assert out["cancellations"] == [{"type": "cancel", "corr_id": "cid-1"}]
        assert plugin._cancellations == []  # drained

    @pytest.mark.asyncio
    async def test_dispatch_result_cancelled(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            await getattr(plugin, "__a2a_dispatch_result")(
                "topic", "cid-1", "partial", cancelled=True,
            )
        env = json.loads(client.publish_message.call_args.args[1])
        assert env["result"]["task"]["status"]["state"] == "cancelled"


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
            MagicMock(agent_name="peer-1", status="idle"),
            "online",
        )

        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert len(out["tasks"]) == 1
        assert out["tasks"][0]["content"] == "do this"
        assert out["tasks"][0]["correlation_id"] == "cid-1"
        assert out["tasks"][0]["kind"] == "task"  # default for unstamped peers
        assert len(out["presence"]) == 1
        assert out["presence"][0]["event"] == "online"

    @pytest.mark.asyncio
    async def test_drain_preserves_inbound_kind(self):
        """The wire kind (message vs task) rides the drained task entry."""
        from slife.a2a.identity import AgentMessage

        plugin._inbound_tasks.clear()
        await plugin._on_incoming_task(AgentMessage(
            source="peer-1", content="hi",
            reply_to="Slife/slife/tasks/result", correlation_id="cid-9",
            metadata={"a2a_kind": "message"},
        ))
        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert out["tasks"][0]["kind"] == "message"

    @pytest.mark.asyncio
    async def test_task_completion_queued_and_drained(self):
        """An outbound async result is queued by _on_task_result and drained
        for auto-push to the harness."""
        plugin._task_completions.clear()
        await plugin._on_task_result("cid-1", "the answer", False)
        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert out["task_completions"] == [
            {"corr_id": "cid-1", "result": "the answer",
             "cancelled": False, "peer": "", "kind": "task"},
        ]
        # Queue is cleared after drain
        assert plugin._inbound_tasks == []
        assert plugin._presence_events == []

    @pytest.mark.asyncio
    async def test_task_completion_skipped_for_poll_mode(self):
        """Poll-mode tasks don't auto-push — the marker is consumed and the
        completion never reaches the harness queue."""
        plugin._task_completions.clear()
        plugin._poll_tasks.add("cid-poll")
        await plugin._on_task_result("cid-poll", "the answer", False)
        out = json.loads(await getattr(plugin, "__a2a_drain_incoming")())
        assert out["task_completions"] == []
        assert "cid-poll" not in plugin._poll_tasks

    @pytest.mark.asyncio
    async def test_dispatch_result_publishes(self):
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            await getattr(plugin, "__a2a_dispatch_result")("Slife/x/tasks/result", "cid-1", "reply")
        client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_result_publishes_official_envelope(self):
        """The dispatched result is the official JSON-RPC Task envelope."""
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            await getattr(plugin, "__a2a_dispatch_result")(
                "Slife/x/tasks/result", "cid-1", "the result",
            )
        topic, payload = client.publish_message.call_args.args
        assert topic == "Slife/x/tasks/result"
        env = json.loads(payload)
        assert env["id"] == "cid-1"
        assert env["_slife"]["correlation_id"] == "cid-1"
        task = env["result"]["task"]
        assert task["id"] == "cid-1"
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the result"

    @pytest.mark.asyncio
    async def test_a2a_list_tasks_returns_official_task_shape(self):
        """a2a_list_tasks serializes TaskRecords as official Task dicts."""
        from slife.a2a.task_store import TaskRecord, get_store, clear_store
        clear_store()
        get_store()._records["t1"] = TaskRecord(
            task_id="t1", agent_name="peer-1", task_preview="do x",
            status="completed", transport="mqtt", result="done",
        )
        client = _fake_client()
        client.list_tasks = MagicMock(
            return_value=[
                TaskRecord(
                    task_id="t1", agent_name="peer-1", task_preview="do x",
                    status="completed", transport="mqtt", result="done",
                ).to_task(),
            ],
        )
        try:
            with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
                result = await getattr(plugin, "a2a_list_tasks")()
            data = json.loads(result)
            assert data[0]["id"] == "t1"
            assert data[0]["status"]["state"] == "completed"
            assert data[0]["artifacts"][0]["parts"][0]["text"] == "done"
        finally:
            clear_store()

    @pytest.mark.asyncio
    async def test_internal_tools_prefixed_double_underscore(self):
        """Internal (LLM-invisible) tools carry the ``__`` prefix convention."""
        tools = await plugin.mcp._list_tools()
        names = {t.name for t in tools}
        for name in ("__a2a_drain_incoming", "__a2a_dispatch_result", "__check"):
            assert name in names, f"{name} missing from plugin tools"

    @pytest.mark.asyncio
    async def test_a2a_tools_visible_no_internal_prefix(self):
        """The a2a_* mesh tools are LLM-visible: no '__' prefix."""
        tools = await plugin.mcp._list_tools()
        by_name = {t.name: t.description for t in tools}
        for name in (
            "a2a_send_task", "a2a_send_task_async", "a2a_send_message",
            "a2a_send_message_async", "a2a_list_agents",
            "a2a_get_task_result", "a2a_cancel_task", "a2a_list_tasks",
            "a2a_agent_card", "a2a_broadcast", "a2a_set_task_done",
        ):
            assert name in by_name, f"{name} missing from plugin tools"
            assert not name.startswith("__"), name


class TestSetTaskDone:
    """a2a_set_task_done — complete a received task with its result."""

    def setup_method(self):
        plugin._reply_tos.clear()

    @pytest.mark.asyncio
    async def test_publishes_completed_to_snapshot_reply_to(self):
        """Completes the ONE inbound task named by task_id, to its requester's
        reply topic, as the official completed envelope — no task record."""
        from slife.a2a.task_store import get_store

        plugin._reply_tos["cid-1"] = "Slife/jack/tasks/result"
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_set_task_done")("cid-1", "the answer")

        assert result == "ok"
        topic, payload = client.publish_message.call_args.args
        assert topic == "Slife/jack/tasks/result"
        env = json.loads(payload)
        assert env["id"] == "cid-1"
        task = env["result"]["task"]
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer"
        # One-shot and never a task-store record.
        assert "cid-1" not in plugin._reply_tos
        assert get_store().list_tasks() == []

    @pytest.mark.asyncio
    async def test_unknown_task_id_errors(self):
        """A task id this agent never received is refused — no publish."""
        plugin._reply_tos.clear()
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            result = await getattr(plugin, "a2a_set_task_done")("cid-nope", "x")

        assert result.startswith("Error")
        assert "unknown task_id" in result
        client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancelled_publishes_cancelled(self):
        plugin._reply_tos["cid-2"] = "Slife/x/tasks/result"
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            await getattr(plugin, "a2a_set_task_done")(
                "cid-2", "", cancelled=True,
            )
        env = json.loads(client.publish_message.call_args.args[1])
        assert env["result"]["task"]["status"]["state"] == "cancelled"
        assert "cid-2" not in plugin._reply_tos

    @pytest.mark.asyncio
    async def test_duplicate_reply_becomes_unknown(self):
        """A second completion of the same task is refused, not duplicated."""
        from slife.a2a.identity import AgentMessage

        plugin._reply_tos["cid-3"] = "Slife/x/tasks/result"
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)):
            first = await getattr(plugin, "a2a_set_task_done")("cid-3", "once")
            second = await getattr(plugin, "a2a_set_task_done")("cid-3", "twice")

        assert first == "ok"
        assert second.startswith("Error")
        assert client.publish_message.call_count == 1

    @pytest.mark.asyncio
    async def test_snapshot_recorded_on_incoming_task(self):
        """Receiving a task snapshots its reply topic for later completion."""
        from slife.a2a.identity import AgentMessage

        plugin._reply_tos.clear()
        plugin._inbound_tasks.clear()
        await plugin._on_incoming_task(AgentMessage(
            source="jack", content="do it",
            reply_to="Slife/jack/tasks/result", correlation_id="cid-snap",
        ))
        assert plugin._reply_tos.get("cid-snap") == "Slife/jack/tasks/result"

    @pytest.mark.asyncio
    async def test_snapshot_skipped_without_reply_to_or_corr(self):
        from slife.a2a.identity import AgentMessage

        plugin._reply_tos.clear()
        await plugin._on_incoming_task(AgentMessage(
            source="jack", content="no id",
        ))
        await plugin._on_incoming_task(AgentMessage(
            source="jack", content="no reply_to", correlation_id="cid-x",
        ))
        assert plugin._reply_tos == {}

    @pytest.mark.asyncio
    async def test_snapshot_evicts_oldest(self):
        """The snapshot map stays bounded — the oldest entry is evicted when a
        flood of inbound tasks arrives (insert path evicts, like _message_sends)."""
        from slife.a2a.identity import AgentMessage

        plugin._reply_tos.clear()
        plugin._inbound_tasks.clear()
        for i in range(plugin._MAX_QUEUED + 1):
            await plugin._on_incoming_task(AgentMessage(
                source="jack", content="t",
                reply_to=f"t{i}", correlation_id=f"c-{i:04d}",
            ))
        assert "c-0000" not in plugin._reply_tos
        assert f"c-{plugin._MAX_QUEUED:04d}" in plugin._reply_tos

    @pytest.mark.asyncio
    async def test_incoming_cancel_pops_snapshot(self):
        plugin._reply_tos["cid-c"] = "Slife/x/tasks/result"
        await plugin._on_incoming_cancel("cid-c")
        assert "cid-c" not in plugin._reply_tos


class TestConfig:
    def test_load_config_from_env(self, monkeypatch):
        import json as _json
        from slife.a2a.config import A2AConfig
        cfg = A2AConfig(enabled=True, agent_name="slife", broker_host="localhost", broker_port=1883)
        monkeypatch.setenv("SLIFE_A2A_CONFIG", _json.dumps({
            "enabled": True, "agent_name": "slife",
            "transport": "mqtt", "broker_host": "localhost", "broker_port": 1883,
            "http_host": "127.0.0.1", "http_port": 0,
            "heartbeat_interval": 15, "heartbeat_timeout": 45, "task_timeout": 120,
        }))
        loaded = plugin._load_config()
        assert loaded.agent_name == "slife"
        assert loaded.broker_port == 1883


class TestEagerConnect:
    """The mesh connects at plugin startup (lifespan), not lazily on tool call."""

    @pytest.fixture(autouse=True)
    def _reset_state(self):
        plugin._client = None
        plugin._inbound_tasks.clear()
        plugin._presence_events.clear()
        yield
        plugin._client = None
        plugin._inbound_tasks.clear()
        plugin._presence_events.clear()

    @pytest.mark.asyncio
    async def test_lifespan_connects_on_startup(self):
        """Entering the lifespan eagerly calls _ensure_connected."""
        client = _fake_client()
        with patch.object(plugin, "_ensure_connected", AsyncMock(return_value=client)) as mock_conn:
            async with plugin._a2a_lifespan(None):
                mock_conn.assert_awaited_once()
        # The mock didn't set plugin._client — nothing to disconnect on exit.

    @pytest.mark.asyncio
    async def test_lifespan_disconnects_on_shutdown(self):
        """Exiting the lifespan disconnects a live client (announces offline)."""
        client = _fake_client()
        plugin._client = client
        with patch.object(plugin, "_ensure_connected", AsyncMock()):
            async with plugin._a2a_lifespan(None):
                pass
        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_tolerates_connect_failure(self):
        """A failed eager connect must not crash plugin startup."""
        with patch.object(
            plugin, "_ensure_connected",
            AsyncMock(side_effect=RuntimeError("broker down")),
        ):
            async with plugin._a2a_lifespan(None):
                pass  # no raise

    @pytest.mark.asyncio
    async def test_ensure_connected_creates_and_connects_client(self, monkeypatch):
        """_ensure_connected builds an A2AClient, connects it, and caches it."""
        import json as _json
        monkeypatch.setenv("SLIFE_A2A_CONFIG", _json.dumps({
            "enabled": True, "agent_name": "slife",
            "transport": "mqtt", "broker_host": "localhost", "broker_port": 1883,
            "http_host": "127.0.0.1", "http_port": 0,
            "heartbeat_interval": 15, "heartbeat_timeout": 45, "task_timeout": 120,
        }))
        fake_client = _fake_client()
        with patch.object(plugin, "A2AClient", return_value=fake_client) as mock_cls:
            result = await plugin._ensure_connected()
        mock_cls.assert_called_once()
        fake_client.connect.assert_awaited_once()
        assert result is fake_client
        assert plugin._client is fake_client


class TestA2aStatusTool:
    """Tests for the __check internal tool (health-check probe)."""

    _CONFIG = json.dumps({
        "enabled": True, "agent_name": "slife",
        "transport": "mqtt", "broker_host": "localhost", "broker_port": 1883,
        "http_host": "127.0.0.1", "http_port": 0,
        "heartbeat_interval": 15, "heartbeat_timeout": 45, "task_timeout": 120,
    })

    @pytest.fixture(autouse=True)
    def _reset_state(self, monkeypatch):
        plugin._client = None
        plugin._inbound_tasks.clear()
        plugin._presence_events.clear()
        plugin._cancellations.clear()
        monkeypatch.setenv("SLIFE_A2A_CONFIG", self._CONFIG)
        yield
        plugin._client = None
        plugin._inbound_tasks.clear()
        plugin._presence_events.clear()
        plugin._cancellations.clear()

    @pytest.mark.asyncio
    async def test_disconnected_when_client_none(self):
        """No live client → connected=False, empty peers (broker down at startup)."""
        out = json.loads(await getattr(plugin, "__check")())
        assert out["enabled"] is True
        assert out["connected"] is False
        assert out["agent_name"] == ""
        assert out["status"] == ""
        assert out["peers"] == []
        assert out["broker"] == "localhost:1883"

    @pytest.mark.asyncio
    async def test_connected_with_peers(self):
        client = _fake_client()
        client.agent_name = "slife"
        client.status = "idle"
        plugin._client = client
        out = json.loads(await getattr(plugin, "__check")())
        assert out["connected"] is True
        assert out["agent_name"] == "slife"
        assert out["status"] == "idle"
        assert out["broker"] == "localhost:1883"
        assert out["peers"] == [
            {"agent_name": "peer-1", "status": "idle"},
        ]

    @pytest.mark.asyncio
    async def test_reports_queued_counts(self):
        plugin._inbound_tasks.append({"type": "task", "source": "peer-1",
                                      "content": "do X", "reply_to": "t",
                                      "correlation_id": "c1"})
        plugin._cancellations.append({"type": "cancel", "corr_id": "c2"})
        plugin._task_completions.append({"corr_id": "c3", "result": "ok",
                                         "cancelled": False, "peer": ""})
        out = json.loads(await getattr(plugin, "__check")())
        assert out["queued"] == {
            "tasks": 1, "presence": 0,
            "cancellations": 1, "task_completions": 1,
        }

    @pytest.mark.asyncio
    async def test_status_does_not_trigger_connect(self):
        """A status probe must never side-effect a connection."""
        with patch.object(plugin, "_ensure_connected", AsyncMock()) as mock_conn:
            await getattr(plugin, "__check")()
        mock_conn.assert_not_awaited()
