"""Tests for Slife.a2a.config — A2AConfig and Slife.a2a.card — AgentCard."""

import pytest
from unittest.mock import patch

from slife.a2a.config import A2AConfig
from slife.a2a.card import AgentCard
from slife.a2a.identity import AgentId


# ── A2AConfig.from_dict ─────────────────────────────────────────────────


class TestA2AConfigFromDict:
    """Tests for A2AConfig.from_dict."""

    def test_none_data_no_name_returns_disabled(self):
        cfg = A2AConfig.from_dict(None)
        assert cfg.enabled is False

    def test_none_agent_name_returns_disabled(self):
        cfg = A2AConfig.from_dict({}, agent_name=None)
        assert cfg.enabled is False

    def test_empty_data_with_name_enables(self):
        cfg = A2AConfig.from_dict({}, agent_name="my-agent")
        assert cfg.enabled is True
        assert cfg.agent_id == "my-agent"
        assert cfg.agent_name == "my-agent"

    def test_broker_defaults_from_empty_dict(self):
        cfg = A2AConfig.from_dict({}, agent_name="agent-1")
        assert cfg.broker_host == "localhost"
        assert cfg.broker_port == 1883

    def test_broker_from_data(self):
        cfg = A2AConfig.from_dict(
            {"broker": {"host": "mqtt.example.com", "port": 8883}},
            agent_name="agent-1",
        )
        assert cfg.broker_host == "mqtt.example.com"
        assert cfg.broker_port == 8883

    def test_broker_command_from_data(self):
        cfg = A2AConfig.from_dict(
            {"broker": {"command": "/usr/sbin/mosquitto"}},
            agent_name="agent-1",
        )
        assert cfg.broker_command == "/usr/sbin/mosquitto"

    def test_heartbeat_defaults(self):
        cfg = A2AConfig.from_dict({}, agent_name="agent-1")
        assert cfg.heartbeat_interval == 15
        assert cfg.heartbeat_timeout == 45
        assert cfg.task_timeout == 120

    def test_custom_heartbeat_values(self):
        cfg = A2AConfig.from_dict(
            {"heartbeat_interval": 30, "heartbeat_timeout": 90, "task_timeout": 300},
            agent_name="agent-1",
        )
        assert cfg.heartbeat_interval == 30
        assert cfg.heartbeat_timeout == 90
        assert cfg.task_timeout == 300

    def test_broker_not_a_dict_falls_back(self):
        """When broker is not a dict, use defaults."""
        cfg = A2AConfig.from_dict(
            {"broker": "just a string"},
            agent_name="agent-1",
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
