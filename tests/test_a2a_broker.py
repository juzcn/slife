"""Tests for slife.a2a.broker — probe_broker function."""

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
