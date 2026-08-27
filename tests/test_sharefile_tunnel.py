"""Tests for slife.plugins.sharefile.tunnel — ngrok tunnel lifecycle management."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from slife.plugins.sharefile import tunnel as tmod
from slife.plugins.sharefile.tunnel import NgrokTunnel, _read_auth_token


# ── NgrokTunnel ───────────────────────────────────────────────────────────────


class TestNgrokTunnelInit:
    """Tests for NgrokTunnel initial state."""

    def test_initial_state(self, monkeypatch):
        """A fresh tunnel is not active and has no public URL."""
        monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
        tunnel = NgrokTunnel()
        assert tunnel.is_active is False
        assert tunnel.public_url is None


class TestNgrokTunnelStatus:
    """Tests for NgrokTunnel.status() — terminal state for the harness."""

    def test_idle_never_started(self, monkeypatch):
        """A fresh tunnel (no attempt) reports 'idle', not 'failed'."""
        monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
        tunnel = NgrokTunnel()
        assert tunnel.status() == {"state": "idle", "url": ""}

    def test_active_when_public_url_set(self):
        """A live tunnel reports 'active' with its public URL."""
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://test.ngrok.io"
        assert tunnel.status() == {"state": "active", "url": "https://test.ngrok.io"}

    def test_starting_while_attempt_in_flight(self):
        """A start attempt in flight reports 'starting', not 'failed'."""
        tunnel = NgrokTunnel()
        tunnel._starting = True
        assert tunnel.status()["state"] == "starting"

    def test_failed_after_all_retries(self):
        """start() exhausting its retries leaves a terminal 'failed' state."""
        mock_ngrok = MagicMock()
        mock_ngrok.forward.side_effect = ConnectionError("persistent error")

        with patch.object(tmod, "_read_auth_token", return_value="token"), \
             patch.object(tmod, "_import_ngrok", return_value=mock_ngrok), \
             patch("time.sleep"):
            tunnel = NgrokTunnel()
            with pytest.raises(RuntimeError, match="3 attempts"):
                tunnel.start(9090)

        assert tunnel.status() == {"state": "failed", "url": ""}

    def test_failed_when_token_missing(self):
        """A missing auth token is a terminal failure, not a transient one."""
        with patch.object(tmod, "_read_auth_token", return_value=None):
            tunnel = NgrokTunnel()
            with pytest.raises(RuntimeError, match="auth token not found"):
                tunnel.start(8080)

        assert tunnel.status()["state"] == "failed"

    def test_success_clears_failed(self):
        """A successful start clears the failed flag (active state)."""
        mock_ngrok = MagicMock()
        mock_listener = MagicMock()
        mock_listener.url.return_value = "https://test.ngrok.io/"
        mock_ngrok.forward.return_value = mock_listener

        with patch.object(tmod, "_read_auth_token", return_value="token"), \
             patch.object(tmod, "_import_ngrok", return_value=mock_ngrok):
            tunnel = NgrokTunnel()
            tunnel.start(8080)

        assert tunnel._failed is False
        assert tunnel.status()["state"] == "active"

    def test_stop_clears_failed(self):
        """An explicit stop returns the tunnel to a clean (idle) state."""
        tunnel = NgrokTunnel()
        tunnel._failed = True
        tunnel.stop()
        assert tunnel._failed is False
        assert tunnel.status()["state"] == "idle"


class TestNgrokTunnelIsActive:
    """Tests for NgrokTunnel.is_active."""

    def test_false_initially(self, monkeypatch):
        """is_active is False on a fresh instance."""
        monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
        tunnel = NgrokTunnel()
        assert tunnel.is_active is False

    def test_true_when_public_url_set(self):
        """is_active becomes True once _public_url is populated."""
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://test.ngrok.io"
        assert tunnel.is_active is True
        assert tunnel.public_url == "https://test.ngrok.io"

    def test_true_when_env_var_set(self, monkeypatch):
        """is_active is True (and public_url resolved) when SLIFE_SHAREFILE_URL is set."""
        monkeypatch.setenv("SLIFE_SHAREFILE_URL", "https://env.ngrok.io")
        tunnel = NgrokTunnel()
        assert tunnel.is_active is True
        assert tunnel.public_url == "https://env.ngrok.io"


class TestNgrokTunnelShareUrlFor:
    """Tests for NgrokTunnel.share_url_for()."""

    def test_builds_correct_url(self):
        """share_url_for builds a /share/<file_id> URL from the public URL."""
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://test.ngrok.io"
        assert tunnel.share_url_for("abc123") == "https://test.ngrok.io/share/abc123"

    def test_returns_none_when_offline(self, monkeypatch):
        """share_url_for returns None when no public URL is available."""
        monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
        tunnel = NgrokTunnel()
        assert tunnel.share_url_for("test123") is None


class TestNgrokTunnelStart:
    """Tests for NgrokTunnel.start()."""

    def test_start_success(self):
        """start() calls ngrok.forward, sets _public_url, and exports the env var."""
        mock_ngrok = MagicMock()
        mock_listener = MagicMock()
        mock_listener.url.return_value = "https://test.ngrok.io/"
        mock_ngrok.forward.return_value = mock_listener

        with patch.object(tmod, "_read_auth_token", return_value="test-token"), \
             patch.object(tmod, "_import_ngrok", return_value=mock_ngrok):
            tunnel = NgrokTunnel()
            url = tunnel.start(8080)

        assert url == "https://test.ngrok.io"
        assert tunnel._public_url == "https://test.ngrok.io"
        assert os.environ.get("SLIFE_SHAREFILE_URL") == "https://test.ngrok.io"
        mock_ngrok.forward.assert_called_once_with(
            "localhost:8080", authtoken="test-token", pooling_enabled=True,
        )

    def test_missing_token_raises(self):
        """start() raises RuntimeError when the auth token is not found."""
        with patch.object(tmod, "_read_auth_token", return_value=None):
            tunnel = NgrokTunnel()
            with pytest.raises(RuntimeError, match="auth token not found"):
                tunnel.start(8080)

    def test_already_running_returns_existing_url(self):
        """start() returns the existing URL without calling ngrok when already up."""
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://existing.ngrok.io"
        with patch.object(tmod, "_read_auth_token") as mock_token, \
             patch.object(tmod, "_import_ngrok") as mock_import:
            url = tunnel.start(8080)
        assert url == "https://existing.ngrok.io"
        mock_token.assert_not_called()
        mock_import.assert_not_called()

    def test_retry_success_on_second_attempt(self):
        """start() retries and succeeds when the first attempt fails."""
        mock_ngrok = MagicMock()
        mock_listener = MagicMock()
        mock_listener.url.return_value = "https://retry.ngrok.io/"
        mock_ngrok.forward.side_effect = [
            ConnectionError("network hiccup"),
            mock_listener,
        ]

        with patch.object(tmod, "_read_auth_token", return_value="token"), \
             patch.object(tmod, "_import_ngrok", return_value=mock_ngrok), \
             patch("time.sleep"):
            tunnel = NgrokTunnel()
            url = tunnel.start(8080)

        assert url == "https://retry.ngrok.io"
        assert mock_ngrok.forward.call_count == 2

    def test_all_retries_fail(self):
        """start() raises RuntimeError with attempt count when every retry fails."""
        mock_ngrok = MagicMock()
        mock_ngrok.forward.side_effect = ConnectionError("persistent error")

        with patch.object(tmod, "_read_auth_token", return_value="token"), \
             patch.object(tmod, "_import_ngrok", return_value=mock_ngrok), \
             patch("time.sleep"):
            tunnel = NgrokTunnel()
            with pytest.raises(RuntimeError, match="3 attempts"):
                tunnel.start(9090)

        assert mock_ngrok.forward.call_count == 3

    def test_concurrent_start_guard(self):
        """start() raises RuntimeError when a concurrent start is already in progress."""
        tunnel = NgrokTunnel()
        tunnel._starting = True
        with pytest.raises(RuntimeError, match="already in progress"):
            tunnel.start(8080)

    def test_stale_start_is_superseded(self):
        """A start stuck past _TUNNEL_START_TIMEOUT is superseded, not rejected.

        Guards against the permanent wedge: a hung daemon thread leaves
        _starting=True forever, so every later start would raise "already
        in progress".  After the timeout a fresh attempt must proceed.
        """
        tunnel = NgrokTunnel()
        tunnel._starting = True
        tunnel._starting_at = time.monotonic() - tmod._TUNNEL_START_TIMEOUT - 10
        with patch.object(tunnel, "_do_start", return_value="https://fresh.ngrok.io") as mock_do:
            url = tunnel.start(8080)
        assert url == "https://fresh.ngrok.io"
        mock_do.assert_called_once_with(8080)
        # Guard cleared after the fresh attempt completes.
        assert tunnel._starting is False
        assert tunnel._starting_at is None

    def test_stale_start_resets_guard(self):
        """After a superseded start, a fresh concurrent start is guarded again."""
        tunnel = NgrokTunnel()
        tunnel._starting = True
        tunnel._starting_at = time.monotonic() - tmod._TUNNEL_START_TIMEOUT - 10
        with patch.object(tunnel, "_do_start", return_value="https://fresh.ngrok.io"):
            tunnel.start(8080)
        # A second start while a *new* attempt is in flight must be rejected.
        tunnel._starting = True
        tunnel._starting_at = time.monotonic()
        with pytest.raises(RuntimeError, match="already in progress"):
            tunnel.start(8080)


class TestNgrokTunnelStop:
    """Tests for NgrokTunnel.stop()."""

    def test_disconnects_and_clears(self, monkeypatch):
        """stop() disconnects ngrok and clears _public_url + the env var."""
        mock_ngrok = MagicMock()
        tunnel = NgrokTunnel()
        tunnel._ngrok = mock_ngrok
        tunnel._public_url = "https://stop-me.ngrok.io"
        monkeypatch.setenv("SLIFE_SHAREFILE_URL", "https://stop-me.ngrok.io")

        tunnel.stop()

        mock_ngrok.disconnect.assert_called_once_with("https://stop-me.ngrok.io")
        assert tunnel._public_url is None
        assert "SLIFE_SHAREFILE_URL" not in os.environ

    def test_not_running_noop(self):
        """stop() does nothing when the tunnel is not running."""
        tunnel = NgrokTunnel()
        # Both _public_url and _ngrok are None — should not raise
        tunnel.stop()

        # _public_url set but _ngrok is None — should not raise
        tunnel._public_url = "https://orphan.ngrok.io"
        tunnel.stop()

        # _ngrok set but _public_url is None — should not raise
        tunnel._public_url = None
        mock_ngrok = MagicMock()
        tunnel._ngrok = mock_ngrok
        tunnel.stop()
        mock_ngrok.disconnect.assert_not_called()


class TestNgrokTunnelStartMonitor:
    """Tests for NgrokTunnel.start_monitor() + _run_monitor()."""

    @pytest.mark.asyncio
    async def test_creates_async_task(self):
        """start_monitor() sets _monitor_task to an asyncio Task."""
        tunnel = NgrokTunnel()
        mock_coro = MagicMock()
        with patch.object(tunnel, "_run_monitor", return_value=mock_coro):
            tunnel.start_monitor(8080)
        assert tunnel._monitor_task is not None
        # The task should be an asyncio Task wrapping our mock coro
        mock_coro.assert_not_called()  # not called yet — it's scheduled

    @pytest.mark.asyncio
    async def test_callback_called_on_retry_success(self):
        """_run_monitor restarts a missing tunnel and calls on_tunnel_up."""
        tunnel = NgrokTunnel()
        callback = MagicMock()

        real_sleep = asyncio.sleep

        async def fast_sleep(_d):
            await real_sleep(0.01)

        def fake_start(port):
            tunnel._public_url = "https://monitor.ngrok.io"
            return tunnel._public_url

        with patch("asyncio.sleep", side_effect=fast_sleep), \
             patch("slife.plugins.sharefile.tunnel._tunnel_alive", return_value=True), \
             patch.object(tunnel, "start", side_effect=fake_start):
            task = asyncio.create_task(tunnel._run_monitor(8080, on_tunnel_up=callback))
            try:
                for _ in range(200):
                    await real_sleep(0.02)
                    if callback.called:
                        break
                callback.assert_called_once()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_skips_when_already_connected(self):
        """_run_monitor does NOT restart a live tunnel (callback not fired)."""
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://already-up.ngrok.io"
        callback = MagicMock()

        real_sleep = asyncio.sleep

        async def fast_sleep(_d):
            await real_sleep(0.01)

        with patch("asyncio.sleep", side_effect=fast_sleep), \
             patch("slife.plugins.sharefile.tunnel._tunnel_alive", return_value=True), \
             patch.object(tunnel, "start", return_value="https://already-up.ngrok.io"):
            task = asyncio.create_task(tunnel._run_monitor(8080, on_tunnel_up=callback))
            try:
                for _ in range(20):
                    await real_sleep(0.02)
                callback.assert_not_called()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_restarts_when_tunnel_dropped(self):
        """Regression: a tunnel dropped server-side (ngrok recycles free-tier
        sessions) must be detected by the health probe and restarted — not
        reported 'active' forever."""
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://dropped.ngrok.io"
        alive_results = iter([False, True])  # dead on first probe, alive after restart

        real_sleep = asyncio.sleep

        async def fast_sleep(_d):
            await real_sleep(0.01)

        def fake_start(port):
            tunnel._public_url = "https://restarted.ngrok.io"
            return tunnel._public_url

        with patch("asyncio.sleep", side_effect=fast_sleep), \
             patch("slife.plugins.sharefile.tunnel._tunnel_alive",
                   side_effect=lambda _u: next(alive_results)), \
             patch.object(tunnel, "start", side_effect=fake_start) as mock_start:
            task = asyncio.create_task(tunnel._run_monitor(8080))
            try:
                for _ in range(200):
                    await real_sleep(0.02)
                    if tunnel._public_url == "https://restarted.ngrok.io":
                        break
                assert tunnel._public_url == "https://restarted.ngrok.io"
                mock_start.assert_called_once()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ── NgrokTunnel state (server-owned single instance) ─────────────────────────


class TestServerOwnedTunnel:
    """The sharefile plugin server owns its own NgrokTunnel instance."""

    def test_server_holds_tunnel_instance(self):
        """server.py owns an NgrokTunnel — the tunnel API is instance-based."""
        from slife.plugins.sharefile import server
        from slife.plugins.sharefile.tunnel import NgrokTunnel
        assert isinstance(server._tunnel, NgrokTunnel)

    def test_is_active_and_public_url(self, monkeypatch):
        monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
        tunnel = NgrokTunnel()
        assert tunnel.is_active is False
        assert tunnel.public_url is None

        tunnel._public_url = "https://module.ngrok.io"
        assert tunnel.is_active is True
        assert tunnel.public_url == "https://module.ngrok.io"

    def test_share_url_for(self, monkeypatch):
        monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
        tunnel = NgrokTunnel()
        tunnel._public_url = "https://share.ngrok.io"
        assert tunnel.share_url_for("test-id") == "https://share.ngrok.io/share/test-id"

        tunnel._public_url = None
        assert tunnel.share_url_for("test-id") is None


# ── _read_auth_token ─────────────────────────────────────────────────────────


class TestReadAuthToken:
    """Tests for _read_auth_token()."""

    def test_from_env_var_when_credstore_fails(self, monkeypatch):
        """Falls back to NGROK_AUTHTOKEN env var when credstore is unavailable."""
        monkeypatch.setenv("NGROK_AUTHTOKEN", "env-token-123")
        # Make get_credential raise so _read_auth_token falls back to the env var.
        with patch(
            "credstore.get_credential", side_effect=OSError("mock")
        ) as mock_get, patch(
            "slife.plugins.sharefile.tunnel.logger"
        ) as mock_logger:
            result = _read_auth_token()
        assert result == "env-token-123"
        mock_get.assert_called_once_with("NGROK_AUTHTOKEN")
        mock_logger.warning.assert_called_once_with("credstore_read_failed")
