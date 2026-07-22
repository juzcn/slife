"""Tests for Slife.mcp.client — MCPClient (SSE transport)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.mcp.client import MCPClient


# ── MCPClient ───────────────────────────────────────────────────────────────


class TestMCPClientProperties:
    """Tests for MCPClient properties and initial state."""

    def test_initial_not_connected(self):
        client = MCPClient()
        assert client.is_connected is False

    def test_initial_state(self):
        client = MCPClient()
        assert client._session is None
        assert client._exit_stack is None


class TestMCPClientDisconnect:
    """Tests for disconnect."""

    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self):
        client = MCPClient()
        client._connected = True

        await client.disconnect()

        assert not client.is_connected
        assert client._session is None
        assert client._exit_stack is None

    @pytest.mark.asyncio
    async def test_disconnect_handles_clean_shutdown(self):
        client = MCPClient()
        client._connected = True

        # Should not raise
        await client.disconnect()
        assert not client.is_connected


class TestMCPClientEnsureConnected:
    """Tests for _ensure_connected."""

    def test_raises_when_not_connected(self):
        client = MCPClient()
        with pytest.raises(RuntimeError, match="not connected"):
            client._ensure_connected()

    def test_ok_when_connected(self):
        client = MCPClient()
        client._connected = True
        client._session = MagicMock()
        # Should not raise
        client._ensure_connected()


class TestMCPClientListTools:
    """Tests for list_tools."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_dicts(self):
        client = MCPClient()
        client._connected = True

        mock_tool1 = MagicMock()
        mock_tool1.name = "tool1"
        mock_tool1.description = "Tool 1"
        mock_tool1.inputSchema = {"type": "object"}

        mock_tool2 = MagicMock()
        mock_tool2.name = "tool2"
        mock_tool2.description = None
        mock_tool2.inputSchema = {}

        mock_result = MagicMock()
        mock_result.tools = [mock_tool1, mock_tool2]
        client._session = MagicMock()
        client._session.list_tools = AsyncMock(return_value=mock_result)

        tools = await client.list_tools()

        assert len(tools) == 2
        assert tools[0]["name"] == "tool1"
        assert tools[0]["description"] == "Tool 1"
        assert tools[1]["name"] == "tool2"
        assert tools[1]["description"] == ""


class TestMCPClientCallTool:
    """Tests for call_tool."""

    @pytest.mark.asyncio
    async def test_call_tool_returns_text(self):
        client = MCPClient()
        client._connected = True

        mock_text_block = MagicMock()
        mock_text_block.text = "Hello, World!"

        mock_result = MagicMock()
        mock_result.content = [mock_text_block]
        client._session = MagicMock()
        client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await client.call_tool("echo", {"message": "Hello"})
        assert result == "Hello, World!"
        client._session.call_tool.assert_called_once_with("echo", {"message": "Hello"})

    @pytest.mark.asyncio
    async def test_call_tool_binary_data(self):
        client = MCPClient()
        client._connected = True

        mock_bin_block = MagicMock()
        del mock_bin_block.text  # has no text
        mock_bin_block.data = b"binary stuff"

        mock_result = MagicMock()
        mock_result.content = [mock_bin_block]
        client._session = MagicMock()
        client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await client.call_tool("read", {})
        assert "[binary data: 12 bytes]" in result

    @pytest.mark.asyncio
    async def test_call_tool_no_arguments(self):
        client = MCPClient()
        client._connected = True

        mock_text_block = MagicMock()
        mock_text_block.text = "OK"
        mock_result = MagicMock()
        mock_result.content = [mock_text_block]
        client._session = MagicMock()
        client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await client.call_tool("noop")
        client._session.call_tool.assert_called_once_with("noop", {})


class TestMCPClientPing:
    """Tests for ping."""

    @pytest.mark.asyncio
    async def test_ping_success(self):
        client = MCPClient()
        client._connected = True
        client._session = MagicMock()
        client._session.send_ping = AsyncMock()

        result = await client.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_failure(self):
        client = MCPClient()
        client._connected = True
        client._session = MagicMock()
        client._session.send_ping = AsyncMock(side_effect=Exception("timeout"))

        result = await client.ping()
        assert result is False


class TestMCPClientConnectSSE:
    """Tests for connect_sse (SSE transport with retry)."""

    @pytest.mark.asyncio
    async def test_connect_sse_sets_state(self):
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.mcp.client.sse_client") as mock_sse:
            mock_read = MagicMock()
            mock_write = MagicMock()
            mock_sse_ctx = MagicMock()
            mock_sse_ctx.__aenter__ = AsyncMock(
                return_value=(mock_read, mock_write),
            )
            mock_sse_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_sse.return_value = mock_sse_ctx

            with patch("slife.mcp.client.ClientSession") as mock_session_cls:
                mock_session_ctx = MagicMock()
                mock_session_ctx.__aenter__ = AsyncMock(
                    return_value=mock_session,
                )
                mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_session_cls.return_value = mock_session_ctx

                await client.connect_sse("http://127.0.0.1:1234/sse")

                assert client.is_connected is True
                assert client._session is mock_session
                assert client._exit_stack is not None
                mock_session.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_sse_already_connected(self):
        client = MCPClient()
        client._connected = True

        with patch("slife.mcp.client.logger") as mock_logger:
            await client.connect_sse("http://127.0.0.1:1234/sse")
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_sse_retries_on_failure(self):
        """ConnectionError triggers retry, eventually succeeds."""
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.mcp.client.sse_client") as mock_sse:
            call_count = [0]

            def _make_ctx():
                mock_read = MagicMock()
                mock_write = MagicMock()
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(
                    return_value=(mock_read, mock_write),
                )
                mock_ctx.__aexit__ = AsyncMock(return_value=None)
                return mock_ctx

            fail_ctx = MagicMock()
            fail_ctx.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
            fail_ctx.__aexit__ = AsyncMock(return_value=None)

            mock_sse.side_effect = [
                fail_ctx,
                fail_ctx,
                _make_ctx(),
            ]

            with patch("slife.mcp.client.ClientSession") as mock_sc:
                mock_sc_ctx = MagicMock()
                mock_sc_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                mock_sc_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_sc.return_value = mock_sc_ctx

                with patch("slife.mcp.client.asyncio.sleep", AsyncMock()):
                    await client.connect_sse("http://127.0.0.1:1234/sse")

                assert client.is_connected is True
