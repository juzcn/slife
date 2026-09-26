"""Tests for slife.a2a.broker — probe_broker function."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from slife.a2a.broker import probe_broker


class TestProbeBroker:
    """Tests for probe_broker."""

    @pytest.mark.asyncio
    async def test_probe_success(self):
        """Returns True when a TCP listener is present."""
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch("asyncio.open_connection", AsyncMock(
            return_value=(MagicMock(), mock_writer),
        )):
            result = await probe_broker("localhost", 1883)
            assert result is True
            mock_writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_probe_connection_refused(self):
        """Returns False when connection fails."""
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            result = await probe_broker("localhost", 19999)
            assert result is False

    @pytest.mark.asyncio
    async def test_probe_timeout(self):
        """Returns False on timeout."""
        with patch("asyncio.wait_for", side_effect=TimeoutError):
            result = await probe_broker("localhost", 1883)
            assert result is False

    @pytest.mark.asyncio
    async def test_a_dead_first_address_does_not_hide_a_live_one(self):
        """Every resolved address gets tried: a name resolving to IPv6 first
        must not report a broker that listens on IPv4 as missing.

        On Windows a dead ``::1`` refused only after ~2 s, which spent the
        whole 1 s budget on the FIRST address — the probe answered False (and
        A2A stayed disabled) while the broker was up and reachable two lines
        further down the list.
        """
        import socket

        infos = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 1883, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1883)),
        ]
        mock_writer = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        calls: list[tuple] = []

        async def _open(host, port, **kwargs):
            calls.append((host, port, kwargs.get("family")))
            if host == "::1":
                raise ConnectionRefusedError
            return MagicMock(), mock_writer

        # The resolution is stubbed where ``probe_broker`` actually takes it:
        # it hands ``socket.getaddrinfo`` to a daemon thread through
        # ``run_daemon`` rather than calling ``loop.getaddrinfo`` — the loop's
        # helper runs on the default executor, whose non-daemon workers a hung
        # resolver would hold open at exit (slife/threads.py).  Patching
        # ``asyncio.get_running_loop`` here instead only breaks ``run_daemon``:
        # it calls that to get the loop it delivers the result on.
        with patch("socket.getaddrinfo", return_value=infos), \
             patch("asyncio.open_connection", _open):
            assert await probe_broker("localhost", 1883) is True

        # The dead family was tried first, the live one second — and it was
        # pinned to its own family rather than re-resolved by name.
        assert calls == [("::1", 1883, socket.AF_INET6),
                         ("127.0.0.1", 1883, socket.AF_INET)]
