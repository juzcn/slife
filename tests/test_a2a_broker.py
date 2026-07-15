"""Tests for Slife.a2a.broker — BrokerManager construction and lifecycle."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.a2a.broker import BrokerManager


class TestBrokerManagerConstruction:
    """Tests for BrokerManager.__init__."""

    def test_default_construction(self):
        mgr = BrokerManager()
        assert mgr._command == "mosquitto"
        assert mgr._args == []
        assert mgr._host == "localhost"
        assert mgr._port == 1883
        assert mgr._process is None
        assert mgr._running is False

    def test_custom_construction(self):
        mgr = BrokerManager(
            command="/usr/sbin/mosquitto",
            args=["-c", "custom.conf"],
            host="mqtt.local",
            port=8883,
        )
        assert mgr._command == "/usr/sbin/mosquitto"
        assert mgr._args == ["-c", "custom.conf"]
        assert mgr._host == "mqtt.local"
        assert mgr._port == 8883


class TestBrokerManagerStop:
    """Tests for BrokerManager.stop."""

    @pytest.mark.asyncio
    async def test_stop_not_running_noop(self):
        """Stop does nothing when no process is running."""
        mgr = BrokerManager()
        await mgr.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_running_process(self):
        """Stop terminates a running process."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait = AsyncMock()
        mock_proc.terminate = MagicMock()
        mock_proc.stderr = None

        mgr = BrokerManager()
        mgr._process = mock_proc
        mgr._running = True

        await mgr.stop()

        mock_proc.terminate.assert_called_once()
        assert mgr._running is False
        assert mgr._process is None

    @pytest.mark.asyncio
    async def test_stop_timeout_triggers_kill(self):
        """When terminate times out, the process is killed."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        # First wait() raises TimeoutError → triggers kill path
        # Second wait() succeeds (after kill)
        mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, None])
        mock_proc.kill = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.stderr = None

        mgr = BrokerManager()
        mgr._process = mock_proc
        mgr._running = True

        await mgr.stop()

        mock_proc.kill.assert_called_once()
        assert mgr._running is False

    @pytest.mark.asyncio
    async def test_stop_process_lookup_error_swallowed(self):
        """ProcessLookupError during stop is silently handled."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.terminate = MagicMock(side_effect=ProcessLookupError)
        mock_proc.stderr = None

        mgr = BrokerManager()
        mgr._process = mock_proc
        mgr._running = True

        await mgr.stop()  # Should not raise
        assert mgr._running is False


class TestBrokerManagerProbe:
    """Tests for BrokerManager._probe."""

    @pytest.mark.asyncio
    async def test_probe_connection_refused(self):
        """Probe returns False when connection fails."""
        mgr = BrokerManager(port=19999)  # Unlikely to have a broker
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            result = await mgr._probe()
            assert result is False

    @pytest.mark.asyncio
    async def test_probe_timeout(self):
        """Probe returns False on timeout."""
        mgr = BrokerManager()
        with patch("asyncio.wait_for", side_effect=TimeoutError):
            result = await mgr._probe()
            assert result is False
