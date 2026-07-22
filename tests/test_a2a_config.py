"""Tests for Slife.a2a.config — A2AConfig and Slife.a2a.card — AgentCard."""

import platform

import pytest
from unittest.mock import patch

from slife.a2a.config import A2AConfig, _default_agent_id
from slife.a2a.card import AgentCard
from slife.a2a.identity import AgentId


# ── _default_agent_id ────────────────────────────────────────────────────


class TestDefaultAgentId:
    """Tests for the _default_agent_id helper."""

    def test_hostname_with_dots_uses_first_segment(self, monkeypatch):
        """When hostname is 'myhost.local', agent_id starts with 'myhost-'."""
        monkeypatch.setattr(platform, "node", lambda: "myhost.local")
        agent_id = _default_agent_id()
        assert agent_id.startswith("myhost-")

    def test_empty_hostname_falls_back_to_unknown(self, monkeypatch):
        """When hostname is empty, agent_id starts with 'unknown-'."""
        monkeypatch.setattr(platform, "node", lambda: "")
        agent_id = _default_agent_id()
        assert agent_id.startswith("unknown-")


# ── A2AConfig.from_dict ─────────────────────────────────────────────────


class TestA2AConfigFromDict:
    """Tests for A2AConfig.from_dict."""

    def test_none_data_returns_disabled(self):
        cfg = A2AConfig.from_dict(None)
        assert cfg.enabled is False

    def test_none_data_with_custom_agent_id(self):
        """None data still uses the caller-supplied agent_id."""
        cfg = A2AConfig.from_dict(None, agent_id="my-agent")
        assert cfg.agent_id == "my-agent"
        assert cfg.enabled is False
        assert cfg.agent_name == ""
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883

    def test_empty_data_returns_disabled_with_defaults(self):
        cfg = A2AConfig.from_dict({})
        assert cfg.enabled is False  # runtime probe sets this
        assert cfg.agent_id == "slife"  # default agent_id
        assert cfg.agent_name == ""

    def test_user_becomes_agent_id(self):
        cfg = A2AConfig.from_dict({}, agent_id="bob")
        assert cfg.agent_id == "bob"
        assert cfg.enabled is False  # runtime probe sets this

    def test_agent_name_from_data(self):
        cfg = A2AConfig.from_dict({"agent_name": "My Agent"}, agent_id="bob")
        assert cfg.agent_id == "bob"
        assert cfg.agent_name == "My Agent"

    def test_broker_defaults_from_empty_dict(self):
        cfg = A2AConfig.from_dict({}, agent_id="agent-1")
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883

    def test_broker_from_data(self):
        cfg = A2AConfig.from_dict(
            {"broker": {"host": "mqtt.example.com", "port": 8883}},
            agent_id="agent-1",
        )
        assert cfg.broker_host == "mqtt.example.com"
        assert cfg.broker_port == 8883

    def test_heartbeat_defaults(self):
        cfg = A2AConfig.from_dict({}, agent_id="agent-1")
        assert cfg.heartbeat_interval == 15
        assert cfg.heartbeat_timeout == 45
        assert cfg.task_timeout == 120

    def test_custom_heartbeat_values(self):
        cfg = A2AConfig.from_dict(
            {"heartbeat_interval": 30, "heartbeat_timeout": 90, "task_timeout": 300},
            agent_id="agent-1",
        )
        assert cfg.heartbeat_interval == 30
        assert cfg.heartbeat_timeout == 90
        assert cfg.task_timeout == 300

    def test_broker_not_a_dict_falls_back(self):
        """When broker is not a dict, use defaults."""
        cfg = A2AConfig.from_dict(
            {"broker": "just a string"},
            agent_id="agent-1",
        )
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883

    def test_broker_is_none_falls_back(self):
        """When broker is None, use defaults."""
        cfg = A2AConfig.from_dict(
            {"broker": None},
            agent_id="agent-1",
        )
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883

    def test_broker_is_list_falls_back(self):
        """When broker is a list (not a dict), use defaults."""
        cfg = A2AConfig.from_dict(
            {"broker": [1, 2, 3]},
            agent_id="agent-1",
        )
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883


# ── A2AConfig defaults ──────────────────────────────────────────────────


class TestA2AConfigDefaults:
    """Tests for A2AConfig default values."""

    def test_default_disabled(self):
        cfg = A2AConfig()
        assert cfg.enabled is False

    def test_default_agent_id_has_format(self):
        """Default agent_id is hostname-pid format."""
        cfg = A2AConfig()
        assert "-" in cfg.agent_id
        assert len(cfg.agent_id) > 0

    def test_default_heartbeat_values(self):
        cfg = A2AConfig()
        assert cfg.heartbeat_interval == 15
        assert cfg.heartbeat_timeout == 45
        assert cfg.task_timeout == 120


# ── AgentCard ───────────────────────────────────────────────────────────


class TestAgentCard:
    """Tests for AgentCard dataclass."""

    def test_default_values(self):
        card = AgentCard(agent_id=AgentId("agent-1"))
        assert card.agent_id == "agent-1"
        assert card.display_name == ""
        assert card.status == "idle"

    def test_full_values(self):
        card = AgentCard(
            agent_id=AgentId("agent-1"),
            display_name="My Agent",
            status="busy",
        )
        assert card.display_name == "My Agent"
        assert card.status == "busy"

    def test_create_factory(self):
        card = AgentCard.create(
            agent_id=AgentId("agent-x"),
            display_name="X Agent",
            status="idle",
        )
        assert card.agent_id == "agent-x"
        assert card.display_name == "X Agent"
        assert card.status == "idle"

    def test_create_defaults(self):
        card = AgentCard.create(agent_id=AgentId("agent-x"))
        assert card.display_name == ""
        assert card.status == "idle"

    def test_create_busy_status(self):
        card = AgentCard.create(
            agent_id=AgentId("agent-x"),
            display_name="X Agent",
            status="busy",
        )
        assert card.agent_id == "agent-x"
        assert card.display_name == "X Agent"
        assert card.status == "busy"
