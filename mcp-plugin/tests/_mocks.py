"""Shared mock doubles for mcp_plugin tests (excluded from pytest collection
by the leading underscore convention, mirroring credstore/tests/_mocks.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def make_mock_mcp_client(**overrides) -> MagicMock:
    """A MagicMock MCPClient with the async surface pre-stubbed."""
    client = MagicMock()
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value="{}")
    client.is_connected = False
    client.ping = AsyncMock()
    for k, v in overrides.items():
        setattr(client, k, v)
    return client