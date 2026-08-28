"""Thread helpers — the single rule encoded here mirrors slife's convention.

Long-running or blocking sync calls (llama-cpp ``create_embedding``, a
``SentenceTransformer.encode``, a first-time model download) must never run
on the event loop, and must never run on a non-daemon ``ThreadPoolExecutor``
thread.  At shutdown, both ``loop.shutdown_default_executor()`` and the
interpreter's ``concurrent.futures.thread._python_exit()`` join *every*
executor worker thread with ``wait=True`` — a worker blocked forever (a hung
model load) hangs the whole interpreter.  A plain **daemon** thread avoids
this entirely: daemon threads are not joined by either path and are killed
when the interpreter exits.

This is a standalone copy of the slife convention — local-embed must not
import slife.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")

__all__ = ["run_daemon"]


def run_daemon(
    fn: Callable[..., T],
    *args: Any,
    name: str = "local-embed-daemon",
) -> "asyncio.Future[T]":
    """Run ``fn(*args)`` on a daemon thread; return an awaitable Future.

    The result (or raised exception) is delivered back on the calling event
    loop via ``call_soon_threadsafe``, so awaiting the returned Future
    behaves exactly like ``loop.run_in_executor`` — except a hung callable
    can never block shutdown.

    If the caller drops the reference without awaiting (fire-and-forget),
    the Future is garbage-collected once the loop loses it, exactly as with
    ``run_in_executor``.
    """
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future[T]" = loop.create_future()

    def _run() -> None:
        try:
            result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - deliver to the awaiting task
            if not fut.done():
                _post(loop, fut.set_exception, exc)
        else:
            if not fut.done():
                _post(loop, fut.set_result, result)

    threading.Thread(target=_run, daemon=True, name=name).start()
    return fut


def _post(loop: asyncio.AbstractEventLoop, callback, *args: Any) -> None:
    """Deliver ``callback(*args)`` on *loop*, ignoring a closed loop.

    A daemon thread may finish after the loop has shut down (e.g. the app
    quit while a slow call was still running).  Posting then raises
    ``RuntimeError``; nobody is awaiting the result, so it is dropped.
    """
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:
        pass
