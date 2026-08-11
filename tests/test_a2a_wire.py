"""Tests for Slife.a2a.wire — the official-shape wire contract."""

import pytest; pytestmark = pytest.mark.unit

import json

from slife.a2a.card import AgentCard
from slife.a2a.identity import AgentId
from slife.a2a import wire


class TestSendMessageEnvelope:
    def test_shape(self):
        env = wire.send_message_envelope(
            corr_id="abc", source="jack", task="do X", reply_to="Slife/jack/tasks/result",
        )
        assert env["jsonrpc"] == "2.0"
        assert env["method"] == "SendMessage"
        assert env["id"] == "abc"
        msg = env["params"]["message"]
        assert msg["role"] == "user"
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "do X"
        assert env["_slife"]["source"] == "jack"
        assert env["_slife"]["reply_to"] == "Slife/jack/tasks/result"

    def test_json_serializable(self):
        env = wire.send_message_envelope("id", "src", "task", "reply")
        json.dumps(env)  # must not raise


class TestTaskResultEnvelope:
    def test_shape(self):
        task = wire.Task.completed("abc", "the result")
        env = wire.task_result_envelope("abc", task)
        assert env["id"] == "abc"
        assert env["result"]["task"]["id"] == "abc"
        assert env["result"]["task"]["status"]["state"] == "completed"
        assert env["_slife"]["correlation_id"] == "abc"

    def test_task_result_text_extraction(self):
        task = wire.Task.completed("abc", "the result")
        d = task.to_dict()
        assert wire.task_result_text(d) == "the result"


class TestTask:
    def test_completed_roundtrip(self):
        task = wire.Task.completed("t1", "hello")
        d = task.to_dict()
        restored = wire.Task.from_dict(d)
        assert restored is not None
        assert restored.id == "t1"
        assert restored.status.state == "completed"

    def test_from_dict_none(self):
        assert wire.Task.from_dict(None) is None

    def test_cancelled_shape(self):
        """REVIEW C5 — a cancelled task carries CANCELLED state + text."""
        task = wire.Task.cancelled("cid-1", "stopped")
        d = task.to_dict()
        assert d["id"] == "cid-1"
        assert d["status"]["state"] == "cancelled"
        assert d["artifacts"][0]["parts"][0]["text"] == "stopped"

    def test_cancelled_empty_text(self):
        task = wire.Task.cancelled("cid-1")
        d = task.to_dict()
        assert d["status"]["state"] == "cancelled"
        assert d["artifacts"] == []


class TestMessage:
    def test_text_message(self):
        m = wire.Message.text_message("hi", role="user")
        assert m.role == "user"
        assert m.content[0].text == "hi"
        assert m.message_id  # fresh id

    def test_roundtrip(self):
        m = wire.Message.text_message("hi")
        d = m.to_dict()
        assert wire.Message.from_dict(d).content[0].text == "hi"


class TestTaskState:
    def test_values(self):
        assert wire.TaskState.COMPLETED.value == "completed"
        assert wire.TaskState.INPUT_REQUIRED.value == "input-required"


class TestAgentCardWire:
    def test_to_dict_keeps_slife_extensions(self):
        card = AgentCard.create(agent_id=AgentId("jack"), display_name="Jack", status="busy")
        d = card.to_dict()
        assert d["agent_id"] == "jack"
        assert d["display_name"] == "Jack"
        assert d["status"] == "busy"
        # Official fields present.
        assert d["protocolVersion"] == "0.3.0"
        assert d["name"] == "Jack"
        assert "capabilities" in d
        assert "skills" in d

    def test_from_dict_roundtrip(self):
        card = AgentCard(
            agent_id=AgentId("jack"), display_name="Jack", status="idle",
            name="Jack", description="d", url="http://x", version="1",
        )
        restored = AgentCard.from_dict(card.to_dict())
        assert restored.agent_id == "jack"
        assert restored.status == "idle"
        assert restored.name == "Jack"
        assert restored.url == "http://x"

    def test_from_dict_minimal_presence(self):
        """Legacy/minimal presence payloads (no official fields) still parse."""
        card = AgentCard.from_dict({"agent_id": "jack", "status": "online"})
        assert card.agent_id == "jack"
        assert card.status == "online"
