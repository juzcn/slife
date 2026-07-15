"""Tests for Slife.mcp.client — MCPClient, adapters, is_wrapper_running."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.mcp.client import (
    MCPClient,
    _ReadAdapter,
    _WriteAdapter,
    DEFAULT_WRAPPER_URL,
)


# ── _ReadAdapter / _WriteAdapter ────────────────────────────────────────────


class TestReadAdapter:
    """Tests for _ReadAdapter."""

    @pytest.mark.asyncio
    async def test_receive_returns_from_queue(self):
        queue = asyncio.Queue()
        await queue.put("test_message")
        adapter = _ReadAdapter(queue)

        result = await adapter.receive()
        assert result == "test_message"

    @pytest.mark.asyncio
    async def test_aiter_yields_items(self):
        queue = asyncio.Queue()
        await queue.put("first")
        await queue.put("second")

        async def closer():
            await asyncio.sleep(0.01)
            await queue.put(None)  # sentinel

        adapter = _ReadAdapter(queue)
        items = []
        # We check __aiter__ returns self
        assert adapter.__aiter__() is adapter


class TestWriteAdapter:
    """Tests for _WriteAdapter."""

    @pytest.mark.asyncio
    async def test_send_puts_to_queue(self):
        queue = asyncio.Queue()
        adapter = _WriteAdapter(queue)

        await adapter.send("msg")

        result = await queue.get()
        assert result == "msg"


# ── MCPClient ───────────────────────────────────────────────────────────────


class TestMCPClientProperties:
    """Tests for MCPClient properties and initial state."""

    def test_initial_not_connected(self):
        client = MCPClient()
        assert client.is_connected is False

    def test_initial_state(self):
        client = MCPClient()
        assert client._session is None
        assert client._process is None
        assert client._transport is None


class TestMCPClientIsWrapperRunning:
    """Tests for is_wrapper_running static method."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_wrapper_running_status_200(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await MCPClient.is_wrapper_running()
        assert result is True

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_wrapper_running_404(self, mock_client_cls):
        """404 is < 500, so it counts as running."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await MCPClient.is_wrapper_running()
        assert result is True

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_wrapper_not_running(self, mock_client_cls):
        """Connection error means not running."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client_cls.return_value = mock_client

        result = await MCPClient.is_wrapper_running()
        assert result is False

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_wrapper_error_500(self, mock_client_cls):
        """500 is not < 500, should return False."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await MCPClient.is_wrapper_running()
        assert result is False

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_custom_url(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await MCPClient.is_wrapper_running("http://custom:9999/mcp")
        assert result is True
        mock_client.get.assert_called_once_with("http://custom:9999/mcp")


class TestMCPClientConnectHTTP:
    """Tests for connect_http."""

    @pytest.mark.asyncio
    @patch("slife.mcp.client.ClientSession")
    @patch("mcp.client.streamable_http.streamablehttp_client")
    async def test_connect_http_success(self, mock_streamable, mock_session_cls):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.initialize = AsyncMock()
        mock_session_cls.return_value = mock_session

        mock_transport = MagicMock()
        mock_transport.__aenter__ = AsyncMock(
            return_value=(AsyncMock(), AsyncMock(), None),
        )
        mock_transport.__aexit__ = AsyncMock(return_value=None)
        mock_streamable.return_value = mock_transport

        client = MCPClient()
        await client.connect_http()

        assert client.is_connected
        mock_streamable.assert_called_once_with(DEFAULT_WRAPPER_URL)

    @pytest.mark.asyncio
    async def test_connect_http_already_connected(self):
        client = MCPClient()
        client._connected = True

        # Should not raise and not try to connect
        with patch("mcp.client.streamable_http.streamablehttp_client") as mock_streamable:
            await client.connect_http()
            mock_streamable.assert_not_called()

    @pytest.mark.asyncio
    @patch("slife.mcp.client.ClientSession")
    @patch("mcp.client.streamable_http.streamablehttp_client")
    async def test_connect_http_custom_url(self, mock_streamable, mock_session_cls):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.initialize = AsyncMock()
        mock_session_cls.return_value = mock_session

        mock_transport = MagicMock()
        mock_transport.__aenter__ = AsyncMock(
            return_value=(AsyncMock(), AsyncMock(), None),
        )
        mock_transport.__aexit__ = AsyncMock(return_value=None)
        mock_streamable.return_value = mock_transport

        client = MCPClient()
        await client.connect_http("http://other:8888/mcp")

        mock_streamable.assert_called_once_with("http://other:8888/mcp")


class TestMCPClientDisconnect:
    """Tests for disconnect."""

    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self):
        client = MCPClient()
        client._connected = True
        client._transport = MagicMock()
        client._transport.__aexit__ = AsyncMock()

        await client.disconnect()

        assert not client.is_connected
        assert client._session is None
        assert client._transport is None

    @pytest.mark.asyncio
    async def test_disconnect_handles_transport_error(self):
        client = MCPClient()
        client._connected = True
        client._transport = MagicMock()
        client._transport.__aexit__ = AsyncMock(side_effect=Exception("boom"))

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
