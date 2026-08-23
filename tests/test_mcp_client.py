"""Tests for Slife.mcp.client — MCPClient (SSE transport)."""

import pytest; pytestmark = pytest.mark.unit


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

    @pytest.mark.asyncio
    async def test_cleanup_bounded_when_aclose_hangs(self):
        """_cleanup must return promptly even if the SDK transport's aclose
        never resolves — the connect retry loop depends on it (a request hung
        against a not-yet-ready server can keep teardown from completing)."""
        client = MCPClient()
        hang = asyncio.Event()

        async def _hang_close():
            await hang.wait()

        stack = MagicMock()
        stack.aclose = _hang_close
        client._exit_stack = stack
        client._session = object()

        with patch("slife.mcp.client._CLEANUP_TIMEOUT", 0.2):
            await client._cleanup()

        # The hung stack was abandoned; state cleared for the next attempt.
        assert client._exit_stack is None
        assert client._session is None


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

    @pytest.mark.asyncio
    async def test_list_tools_timeout_raises(self):
        """A hung session.list_tools surfaces as TimeoutError, not a hang.

        ``asyncio.timeout`` (not ``wait_for``) breaks the stuck SSE session
        at the deadline even when the inner task won't finish cancelling —
        this is what makes the plugin-load race detectable.
        """
        client = MCPClient()
        client._connected = True
        client._tool_timeout = 0.05  # force the timeout quickly
        client._session = MagicMock()

        async def _hang() -> None:
            await asyncio.sleep(3600)  # never responds

        client._session.list_tools = _hang

        with pytest.raises(TimeoutError, match="list_tools timed out"):
            await client.list_tools()


class TestMCPClientCallTool:
    """Tests for call_tool."""

    @pytest.mark.asyncio
    async def test_call_tool_returns_text(self):
        client = MCPClient()
        client._connected = True

        mock_text_block = MagicMock()
        mock_text_block.text = "Hello, World!"

        mock_result = MagicMock()
        mock_result.isError = False
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
        mock_result.isError = False
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


class TestMCPClientConnect:
    """Tests for connect() (Streamable HTTP transport with retry)."""

    @pytest.mark.asyncio
    async def test_connect_sets_state(self):
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.mcp.client.streamable_http_client") as mock_transport:
            mock_read = MagicMock()
            mock_write = MagicMock()
            mock_info = MagicMock()
            mock_transport_ctx = MagicMock()
            mock_transport_ctx.__aenter__ = AsyncMock(
                return_value=(mock_read, mock_write, mock_info),
            )
            mock_transport_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_transport.return_value = mock_transport_ctx

            with patch("slife.mcp.client.ClientSession") as mock_session_cls:
                mock_session_ctx = MagicMock()
                mock_session_ctx.__aenter__ = AsyncMock(
                    return_value=mock_session,
                )
                mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_session_cls.return_value = mock_session_ctx

                await client.connect("http://127.0.0.1:1234/mcp")

                assert client.is_connected is True
                assert client._session is mock_session
                assert client._exit_stack is not None
                mock_session.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_passes_proxy_free_http_client(self):
        """Local plugin traffic must not route through the OS proxy.

        Regression: a Windows system proxy (e.g. 127.0.0.1:7890) was being
        picked up via the MCP SDK's default httpx client (trust_env=True),
        502ing every localhost plugin session — so plugin startup hung and
        the "插件已加载" messages never rendered.  connect() now supplies its
        own httpx.AsyncClient(trust_env=False).
        """
        client = MCPClient()
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.mcp.client.streamable_http_client") as mock_transport:
            mock_read, mock_write, mock_info = MagicMock(), MagicMock(), MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=(mock_read, mock_write, mock_info))
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_transport.return_value = mock_ctx

            with patch("slife.mcp.client.ClientSession") as mock_session_cls:
                mock_sc = MagicMock()
                mock_sc.__aenter__ = AsyncMock(return_value=mock_session)
                mock_sc.__aexit__ = AsyncMock(return_value=None)
                mock_session_cls.return_value = mock_sc

                await client.connect("http://127.0.0.1:1234/mcp")

        assert client._http_client is not None
        assert client._http_client.trust_env is False
        # The provided client is handed to the transport, not the SDK default.
        _, kwargs = mock_transport.call_args
        assert kwargs.get("http_client") is client._http_client

    @pytest.mark.asyncio
    async def test_connect_already_connected(self):
        client = MCPClient()
        client._connected = True

        with patch("slife.mcp.client.logger") as mock_logger:
            await client.connect("http://127.0.0.1:1234/mcp")
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_retries_on_failure(self):
        """ConnectionError triggers retry, eventually succeeds."""
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.mcp.client.streamable_http_client") as mock_transport:
            def _make_ctx():
                mock_read = MagicMock()
                mock_write = MagicMock()
                mock_info = MagicMock()
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(
                    return_value=(mock_read, mock_write, mock_info),
                )
                mock_ctx.__aexit__ = AsyncMock(return_value=None)
                return mock_ctx

            fail_ctx = MagicMock()
            fail_ctx.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
            fail_ctx.__aexit__ = AsyncMock(return_value=None)

            mock_transport.side_effect = [
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
                    await client.connect("http://127.0.0.1:1234/mcp")

                assert client.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_hanging_transport_enter_is_bounded_and_retried(self):
        """A transport ``__aenter__`` that never resolves must be bounded by the
        attempt timeout and retried, not left pending forever.

        Regression: memfiles' eager ngrok tunnel delayed the app past the port
        signal; under load the ``streamable_http_client`` enter could hang, and
        only ``initialize()`` was wrapped in a timeout — so ``connect()`` (and
        the plugin spawn) hung indefinitely.
        """
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        hang_forever = asyncio.Event()

        async def _hang_enter(*_a):
            await hang_forever.wait()

        hang_ctx = MagicMock()
        hang_ctx.__aenter__ = _hang_enter
        hang_ctx.__aexit__ = AsyncMock(return_value=None)

        def _make_ctx():
            mock_read = MagicMock()
            mock_write = MagicMock()
            mock_info = MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(
                return_value=(mock_read, mock_write, mock_info),
            )
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            return mock_ctx

        with patch("slife.mcp.client.streamable_http_client") as mock_transport:
            mock_transport.side_effect = [hang_ctx, _make_ctx()]

            with patch("slife.mcp.client.ClientSession") as mock_sc:
                mock_sc_ctx = MagicMock()
                mock_sc_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                mock_sc_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_sc.return_value = mock_sc_ctx

                with (
                    patch("slife.mcp.client._CONNECT_ATTEMPT_TIMEOUT", 0.2),
                    patch("slife.mcp.client._CONNECT_RETRY_DELAY", 0.01),
                    patch("slife.mcp.client.asyncio.sleep", AsyncMock()),
                ):
                    await client.connect("http://127.0.0.1:1234/mcp")

                # The hung attempt timed out, then the next attempt succeeded.
                assert client.is_connected is True
                assert mock_transport.call_count == 2


class TestMCPClientNotificationHandler:
    """MCPClient._handle_server_message dispatches server notifications."""

    @pytest.mark.asyncio
    async def test_dispatches_tools_list_changed(self):
        from types import SimpleNamespace

        client = MCPClient()
        seen = {}

        async def handler(method, params):
            seen["method"] = method
            seen["params"] = params

        client.on_notification = handler
        msg = SimpleNamespace(
            method="notifications/tools/list_changed",
            params={"server": "foo"},
        )
        await client._handle_server_message(msg)

        assert seen["method"] == "notifications/tools/list_changed"
        assert seen["params"] == {"server": "foo"}

    @pytest.mark.asyncio
    async def test_ignores_non_notification_methods(self):
        from types import SimpleNamespace

        client = MCPClient()
        handler = AsyncMock()
        client.on_notification = handler
        # A server→client request or a response never reaches the handler.
        await client._handle_server_message(
            SimpleNamespace(method="ping", params={}),
        )
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_handler_is_noop(self):
        from types import SimpleNamespace

        client = MCPClient()
        await client._handle_server_message(
            SimpleNamespace(method="notifications/tools/list_changed", params={}),
        )  # must not raise

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate(self):
        from types import SimpleNamespace

        client = MCPClient()

        async def boom(method, params):
            raise RuntimeError("boom")

        client.on_notification = boom
        await client._handle_server_message(
            SimpleNamespace(method="notifications/tools/list_changed", params={}),
        )  # must not raise

    @pytest.mark.asyncio
    async def test_params_model_dump_is_extracted(self):
        from types import SimpleNamespace

        client = MCPClient()
        seen = {}

        async def handler(method, params):
            seen["params"] = params

        client.on_notification = handler
        msg = SimpleNamespace(
            method="notifications/tools/list_changed",
            params=SimpleNamespace(model_dump=lambda: {"a": 1}),
        )
        await client._handle_server_message(msg)
        assert seen["params"] == {"a": 1}
