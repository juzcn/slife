"""Tests for slife.threads — daemon-thread helper run_daemon."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import threading

from slife.threads import run_daemon


class TestRunDaemon:
    """Tests for run_daemon(fn, *args, name) -> asyncio.Future."""

    @pytest.mark.asyncio
    async def test_returns_result(self):
        """Awaiting run_daemon yields the callable's return value."""
        assert await run_daemon(lambda: 42, name="t") == 42

    @pytest.mark.asyncio
    async def test_forwards_exception(self):
        """Exceptions raised on the daemon thread propagate to the awaiter."""
        def boom() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            await run_daemon(boom, name="t")

    @pytest.mark.asyncio
    async def test_worker_is_daemon(self):
        """The worker thread is a daemon — it must never block interpreter exit."""
        fut = run_daemon(lambda: threading.current_thread().daemon, name="t")
        assert await fut is True

    @pytest.mark.asyncio
    async def test_accepts_args(self):
        """Positional args are forwarded to the callable."""
        result = await run_daemon(lambda a, b: a + b, 2, 3, name="t")
        assert result == 5

    @pytest.mark.asyncio
    async def test_fire_and_forget_is_safe(self):
        """Dropping the Future without awaiting neither raises nor hangs."""
        run_daemon(lambda: None, name="t")  # noqa: B018 - deliberate drop
        await asyncio.sleep(0.05)  # let the thread finish and be GC'd
