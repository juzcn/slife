"""Tests for Slife.mcp.client — MCPClient (SSE transport)."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp import MCPError
from mcp.types import CallToolResult, TextContent
from mcp_types import REQUEST_TIMEOUT

from slife.plugins.mcp_gateway.client import MCPClient, _LINK_DOWN_CODE


# ── MCPClient ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def era_stub():
    """Stub the protocol-era glue (its own tests cover the real behaviour).

    ``connect()`` negotiates the peer's era instead of calling
    ``session.initialize()``, so the connect tests need that seam replaced —
    a mocked session cannot run the SDK's ``server/discover`` probe.  The
    stub reports a LEGACY peer: no listen supervisor is spawned (that path
    has its own test below).
    """
    negotiate = AsyncMock(return_value="2025-11-25")
    peer_era = MagicMock(return_value="legacy")
    with (
        patch("slife.plugins.mcp_gateway.client.negotiate_era", negotiate),
        patch("slife.plugins.mcp_gateway.client.peer_era", peer_era),
    ):
        yield negotiate


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

        with patch("slife.timeouts.timeouts.grace.cleanup", 0.2):
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
        mock_result.is_error = False
        del mock_result.isError  # a real CallToolResult has no isError attr
        mock_result.content = [mock_text_block]
        client._session = MagicMock()
        client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await client.call_tool("echo", {"message": "Hello"})
        assert result == "Hello, World!"
        client._session.call_tool.assert_called_once_with("echo", {"message": "Hello"})

    @pytest.mark.asyncio
    async def test_call_tool_error_result_gets_error_prefix(self):
        """A CallToolResult with is_error=True must surface as ``Error: …``
        (the loop derives is_error from the "Error" prefix)."""
        client = MCPClient()
        client._connected = True

        client._session = MagicMock()
        client._session.call_tool = AsyncMock(return_value=CallToolResult(
            content=[TextContent(text="boom")], is_error=True,
        ))

        result = await client.call_tool("echo", {"message": "Hello"})
        assert result.startswith("Error")
        assert "boom" in result

    @pytest.mark.asyncio
    async def test_call_tool_binary_data(self):
        client = MCPClient()
        client._connected = True

        mock_bin_block = MagicMock()
        del mock_bin_block.text  # has no text
        mock_bin_block.data = b"binary stuff"

        mock_result = MagicMock()
        mock_result.is_error = False
        del mock_result.isError  # a real CallToolResult has no isError attr
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

        await client.call_tool("noop")
        client._session.call_tool.assert_called_once_with("noop", {})


class TestMCPClientLinkRecovery:
    """A session that dies under a live-looking ``_connected`` is rebuilt once."""

    @staticmethod
    def _text_result(text: str) -> MagicMock:
        block = MagicMock()
        block.text = text
        result = MagicMock()
        result.is_error = False
        result.content = [block]
        return result

    @staticmethod
    def _client(session: Any, *, url: str = "http://127.0.0.1:1234/mcp") -> MCPClient:
        client = MCPClient()
        client._connected = True
        client._url = url
        client._session = session
        return client

    def test_link_down_code_matches_the_sdk(self):
        """``_LINK_DOWN_CODE`` is spelled locally (``mcp`` does not re-export
        it) — pin it to the SDK's own so a renumber fails here, not in prod."""
        from mcp_types import CONNECTION_CLOSED

        assert _LINK_DOWN_CODE == CONNECTION_CLOSED

    @pytest.mark.asyncio
    async def test_call_tool_rebuilds_a_dead_session_and_retries(self, monkeypatch):
        """The observed failure: the first call on a session is fine, the
        session then dies, and every later call failed forever because
        ``_connected`` stayed True.  One rebuild turns that into a retry."""
        dead = MagicMock()
        dead.call_tool = AsyncMock(
            side_effect=MCPError(code=_LINK_DOWN_CODE, message="Connection closed"),
        )
        client = self._client(dead)

        fresh = MagicMock()
        fresh.call_tool = AsyncMock(return_value=self._text_result("recovered"))
        reconnects: list[str] = []

        async def _connect(url: str) -> None:
            reconnects.append(url)
            client._session = fresh
            client._connected = True
            client._generation += 1

        async def _teardown() -> None:
            client._connected = False

        monkeypatch.setattr(client, "connect", _connect)
        monkeypatch.setattr(client, "_teardown_session", _teardown)

        result = await client.call_tool("__mcp_check", {"a": 1})

        assert result == "recovered"
        assert reconnects == ["http://127.0.0.1:1234/mcp"]
        dead.call_tool.assert_awaited_once()
        fresh.call_tool.assert_awaited_once_with("__mcp_check", {"a": 1})

    @pytest.mark.asyncio
    async def test_other_errors_are_not_retried(self, monkeypatch):
        """Only a link-down earns the retry: a call the peer may be executing
        (a timeout) or one it rejected must never be issued twice."""
        session = MagicMock()
        session.call_tool = AsyncMock(
            side_effect=MCPError(code=REQUEST_TIMEOUT, message="Request timed out"),
        )
        client = self._client(session)
        reconnects = AsyncMock()
        monkeypatch.setattr(client, "connect", reconnects)

        result = await client.call_tool("job-write", {})

        assert result.startswith("Error: Tool 'job-write' failed: MCPError")
        session.call_tool.assert_awaited_once()
        reconnects.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_rebuild_reports_the_original_error(self, monkeypatch):
        """A gateway that is gone for good stays an error string, not a raise
        and not an endless reconnect loop."""
        session = MagicMock()
        session.call_tool = AsyncMock(
            side_effect=MCPError(code=_LINK_DOWN_CODE, message="Connection closed"),
        )
        client = self._client(session)

        async def _teardown() -> None:
            client._connected = False

        async def _connect(url: str) -> None:
            raise ConnectionError("refused")

        monkeypatch.setattr(client, "connect", _connect)
        monkeypatch.setattr(client, "_teardown_session", _teardown)

        result = await client.call_tool("__mcp_check")

        assert result.startswith("Error: Tool '__mcp_check' failed:")
        assert "Connection closed" in result
        session.call_tool.assert_awaited_once()  # no second attempt

    @pytest.mark.asyncio
    async def test_call_after_a_failed_rebuild_still_returns_a_string(self):
        """A client left disconnected (rebuild failed, or a plain shutdown)
        reports through the result string — ``call_tool`` never raises."""
        client = MCPClient()
        client._connected = False

        result = await client.call_tool("__mcp_check")

        assert result.startswith("Error: Tool '__mcp_check' failed: RuntimeError")

    @pytest.mark.asyncio
    async def test_concurrent_callers_rebuild_the_session_once(self, monkeypatch):
        """Two callers that failed on the SAME dead session share one rebuild:
        the generation guard makes the loser retry instead of reconnecting."""
        dead = MagicMock()
        dead.call_tool = AsyncMock(
            side_effect=MCPError(code=_LINK_DOWN_CODE, message="Connection closed"),
        )
        client = self._client(dead)
        fresh = MagicMock()
        fresh.call_tool = AsyncMock(return_value=self._text_result("ok"))
        rebuilds = 0

        async def _connect(url: str) -> None:
            nonlocal rebuilds
            rebuilds += 1
            await asyncio.sleep(0)  # hold the lock across both callers
            client._session = fresh
            client._connected = True
            client._generation += 1

        async def _teardown() -> None:
            # Deliberately leaves ``_connected`` alone: the gather's
            # interleaving must not decide which caller gets past
            # ``_ensure_connected`` — the generation guard is what is under
            # test here.
            pass

        monkeypatch.setattr(client, "connect", _connect)
        monkeypatch.setattr(client, "_teardown_session", _teardown)

        results = await asyncio.gather(
            client.call_tool("a"), client.call_tool("b"),
        )

        assert results == ["ok", "ok"]
        assert rebuilds == 1

    @pytest.mark.asyncio
    async def test_list_tools_recovers_the_same_way(self, monkeypatch):
        dead = MagicMock()
        dead.list_tools = AsyncMock(
            side_effect=MCPError(code=_LINK_DOWN_CODE, message="Connection closed"),
        )
        client = self._client(dead)
        tool = MagicMock()
        tool.name = "t"
        tool.description = "d"
        tool.input_schema = {}
        fresh = MagicMock()
        fresh.list_tools = AsyncMock(return_value=MagicMock(tools=[tool]))

        async def _connect(url: str) -> None:
            client._session = fresh
            client._connected = True
            client._generation += 1

        async def _teardown() -> None:
            client._connected = False

        monkeypatch.setattr(client, "connect", _connect)
        monkeypatch.setattr(client, "_teardown_session", _teardown)

        tools = await client.list_tools()

        assert [t["name"] for t in tools] == ["t"]


class TestMCPClientConnect:
    """Tests for connect() (Streamable HTTP transport with retry)."""

    @pytest.mark.asyncio
    async def test_connect_sets_state(self, era_stub):
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.plugins.mcp_gateway.client.streamable_http_client") as mock_transport:
            mock_read = MagicMock()
            mock_write = MagicMock()
            mock_transport_ctx = MagicMock()
            mock_transport_ctx.__aenter__ = AsyncMock(
                return_value=(mock_read, mock_write),
            )
            mock_transport_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_transport.return_value = mock_transport_ctx

            with patch("slife.plugins.mcp_gateway.client.ClientSession") as mock_session_cls:
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
                # The era is negotiated (probe → adopt modern, or the legacy
                # handshake) — never a bare initialize() call.
                era_stub.assert_awaited_once_with(mock_session)
                assert client._era == "2025-11-25"

    @pytest.mark.asyncio
    async def test_connect_passes_proxy_free_http_client(self):
        """Local plugin traffic must not route through the OS proxy.

        Regression: a Windows system proxy (e.g. 127.0.0.1:7890) was being
        picked up via the MCP SDK's default httpx2 client (trust_env=True),
        502ing every localhost plugin session — so plugin startup hung and
        the "插件已加载" messages never rendered.  connect() now supplies its
        own httpx2.AsyncClient(trust_env=False).
        """
        client = MCPClient()
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.plugins.mcp_gateway.client.streamable_http_client") as mock_transport:
            mock_read, mock_write, _ = MagicMock(), MagicMock(), MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=(mock_read, mock_write))
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_transport.return_value = mock_ctx

            with patch("slife.plugins.mcp_gateway.client.ClientSession") as mock_session_cls:
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

        with patch("slife.plugins.mcp_gateway.client.logger") as mock_logger:
            await client.connect("http://127.0.0.1:1234/mcp")
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_retries_on_failure(self):
        """ConnectionError triggers retry, eventually succeeds."""
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        with patch("slife.plugins.mcp_gateway.client.streamable_http_client") as mock_transport:
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

            mock_transport.side_effect = [
                fail_ctx,
                fail_ctx,
                _make_ctx(),
            ]

            with patch("slife.plugins.mcp_gateway.client.ClientSession") as mock_sc:
                mock_sc_ctx = MagicMock()
                mock_sc_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                mock_sc_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_sc.return_value = mock_sc_ctx

                with patch("slife.plugins.mcp_gateway.client.asyncio.sleep", AsyncMock()):
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
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(
                return_value=(mock_read, mock_write),
            )
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            return mock_ctx

        with patch("slife.plugins.mcp_gateway.client.streamable_http_client") as mock_transport:
            mock_transport.side_effect = [hang_ctx, _make_ctx()]

            with patch("slife.plugins.mcp_gateway.client.ClientSession") as mock_sc:
                mock_sc_ctx = MagicMock()
                mock_sc_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                mock_sc_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_sc.return_value = mock_sc_ctx

                with (
                    patch("slife.timeouts.timeouts.ready.connect_attempt", 0.2),
                    patch("slife.timeouts.timeouts.ready.connect_retry_delay", 0.01),
                    patch("slife.plugins.mcp_gateway.client.asyncio.sleep", AsyncMock()),
                ):
                    await client.connect("http://127.0.0.1:1234/mcp")

                # The hung attempt timed out, then the next attempt succeeded.
                assert client.is_connected is True
                assert mock_transport.call_count == 2

    @pytest.mark.asyncio
    async def test_connect_retries_sdk_internal_scope_cancel(self):
        """SDK-internal ``CancelledError("Cancelled via cancel scope ...")``
        must go through the retry loop, not abort the connect.

        Regression (WSL only): the MCP SDK's ``streamable_http_client`` tears
        down its own task group — raising ``CancelledError`` at the await
        point — when a sibling stream (GET/SSE negotiation) dies against a
        plugin whose uvicorn accept loop has not started yet (the port signal
        fires inside the plugin's lifespan).  The task's own cancellation
        counter stays ``0``, so it is not a real external cancel; previously
        the unconditional ``raise`` skipped the retry loop entirely and the
        plugin spawn failed in milliseconds.
        """
        client = MCPClient()

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        def _make_ctx():
            mock_read = MagicMock()
            mock_write = MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(
                return_value=(mock_read, mock_write),
            )
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            return mock_ctx

        def _make_cancel_ctx():
            mock_ctx = MagicMock()
            mock_ctx.__aenter__ = AsyncMock(
                side_effect=asyncio.CancelledError(
                    "Cancelled via cancel scope deadbeef",  # noqa: S106
                ),
            )
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            return mock_ctx

        with patch("slife.plugins.mcp_gateway.client.streamable_http_client") as mock_transport:
            mock_transport.side_effect = [
                _make_cancel_ctx(),
                _make_cancel_ctx(),
                _make_ctx(),
            ]

            with patch("slife.plugins.mcp_gateway.client.ClientSession") as mock_sc:
                mock_sc_ctx = MagicMock()
                mock_sc_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                mock_sc_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_sc.return_value = mock_sc_ctx

                with patch("slife.plugins.mcp_gateway.client.asyncio.sleep", AsyncMock()):
                    await client.connect("http://127.0.0.1:1234/mcp")

                # The scoped-cancel attempts were retried, not fatal.
                assert client.is_connected is True
                assert mock_transport.call_count == 3

    @pytest.mark.asyncio
    async def test_connect_external_cancel_propagates(self):
        """A genuine external cancellation (``task.cancel()``) still
        propagates out of ``connect()`` and is never swallowed into a retry.

        The internal-scope retry (`cancelling() == 0`) must not catch a real
        controller cancellation — the task's counter is ``>= 1``, so the
        original ``raise`` path is taken instead.
        """
        client = MCPClient()
        gate = asyncio.Event()

        async def _hang_enter(*_a):
            await gate.wait()

        hang_ctx = MagicMock()
        hang_ctx.__aenter__ = _hang_enter
        hang_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("slife.plugins.mcp_gateway.client.streamable_http_client") as mock_transport:
            mock_transport.return_value = hang_ctx

            task = asyncio.ensure_future(
                client.connect("http://127.0.0.1:1234/mcp"),
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert client.is_connected is False
            # One attempt only — the cancel propagated instead of retrying.
            assert mock_transport.call_count == 1


class TestMCPClientListenStream:
    """A modern peer's listen stream exists only where a handler does.

    The stream is the client's one piece of connection state (the protocol is
    stateless: no session id, no standalone channel), and a client with no
    handler could not consume an event from it.
    """

    @staticmethod
    def _live_modern(monkeypatch) -> MCPClient:
        """A connected client whose peer negotiated the modern era."""
        monkeypatch.setattr(
            "slife.plugins.mcp_gateway.client.peer_era",
            MagicMock(return_value="modern"),
        )
        monkeypatch.setattr(
            "slife.plugins.mcp_gateway.client.watch_tools_changed",
            MagicMock(return_value=asyncio.sleep(3600)),
        )
        client = MCPClient()
        client._connected = True
        client._session = MagicMock()
        client._url = "http://127.0.0.1:1234/mcp"
        return client

    @pytest.mark.asyncio
    async def test_no_handler_opens_no_stream(self, monkeypatch):
        client = self._live_modern(monkeypatch)

        client._ensure_watch_task()

        assert client._watch_task is None

    @pytest.mark.asyncio
    async def test_handler_assigned_after_connect_opens_the_stream(self, monkeypatch):
        """The host wires its handler AFTER ``connect`` — the stream follows
        the handler, so it must still open then."""
        client = self._live_modern(monkeypatch)

        client.on_notification = AsyncMock()

        assert client._watch_task is not None
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_legacy_peer_gets_no_stream(self, monkeypatch):
        client = self._live_modern(monkeypatch)
        monkeypatch.setattr(
            "slife.plugins.mcp_gateway.client.peer_era",
            MagicMock(return_value="legacy"),
        )

        client.on_notification = AsyncMock()

        assert client._watch_task is None


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
