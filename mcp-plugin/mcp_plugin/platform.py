"""Platform-aware subprocess helpers for mcp_plugin (slife-free).

A trimmed subset of ``slife.platform`` plus ``kill_process_tree`` (moved
verbatim from ``slife.tools.exec``) — everything the MCP connection layer
needs to spawn, terminate and notify without importing slife.
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess as _subprocess
import sys
import platform as _platform

IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger(__name__)


def resolve_command(command: str) -> str:
    """Resolve a command name to its full path on Windows.

    On Windows, appends .cmd/.exe extensions if needed and resolves
    via shutil.which(). On other platforms, returns the command as-is.
    """
    if IS_WINDOWS and not command.lower().endswith((".exe", ".cmd", ".bat")):
        resolved = shutil.which(command) or shutil.which(command + ".cmd") or shutil.which(command + ".exe")
        if resolved:
            return resolved
    return command


def _close_pipe_transports(process: asyncio.subprocess.Process) -> None:
    """Close stdin/stdout/stderr pipe transports on *process*.

    On Windows ProactorEventLoop, subprocess pipes are wrapped in
    ``_ProactorBasePipeTransport``.  If these aren't explicitly closed
    before the process handle becomes invalid, ``__del__`` tries to
    access ``self._sock.fileno()`` on a closed pipe and raises
    ``ValueError: I/O operation on closed pipe`` during GC.

    Call this after the subprocess has exited to silence the warning.
    """
    # stdin is a StreamWriter — close() is public.
    if process.stdin:
        try:
            process.stdin.close()
        except Exception:
            pass
    # stdout / stderr are StreamReader — transport is at ._transport.
    for attr in ("stdout", "stderr"):
        pipe = getattr(process, attr, None)
        if pipe is None:
            continue
        try:
            pipe._transport.close()  # type: ignore[attr-defined]
        except Exception:
            pass


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    graceful_timeout: float = 3.0,
    force_timeout: float = 5.0,
    label: str = "",
) -> None:
    """Gracefully terminate an asyncio subprocess with escalating force.

    1. Close stdin to signal EOF.
    2. Send SIGTERM / ``terminate()``.
    3. Wait *graceful_timeout* seconds for graceful exit.
    4. Force-kill if still running.
    5. Wait *force_timeout* seconds for kill to take effect.
    6. Close remaining pipe transports (prevents ``ResourceWarning``
       on Windows ProactorEventLoop where the pipe handle is already
       invalid by the time ``__del__`` runs).

    Swallows ``ProcessLookupError`` (already exited) and logs otherwise.
    """
    if process is None:
        return
    try:
        if process.returncode is None:
            # Close stdin first to signal the process
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass

            # Graceful termination
            if IS_WINDOWS:
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)

            # Wait for graceful exit
            try:
                await asyncio.wait_for(process.wait(), timeout=graceful_timeout)
                logger.debug("process_exited pid=%s label=%s", process.pid, label)
            except asyncio.TimeoutError:
                logger.warning("process_force_kill pid=%s label=%s", process.pid, label)
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=force_timeout)
                except asyncio.TimeoutError:
                    pass  # Best effort
    except ProcessLookupError:
        pass  # Already exited
    except Exception as e:
        logger.debug("process_terminate_error label=%s err=%s", label, e)
    finally:
        # Close stdout/stderr transports to prevent "unclosed transport"
        # ResourceWarning on Windows.  On ProactorEventLoop, if the pipe
        # handle is already invalid when __del__ runs, accessing
        # self._sock.fileno() raises "I/O operation on closed pipe".
        # Closing transports here marks them closed so __del__ is a no-op.
        _close_pipe_transports(process)


async def kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its whole process tree.

    ``process.kill()`` only kills the direct child (``cmd.exe`` / ``sh``).
    Any grandchildren it spawned — e.g. yt-dlp started by a shell, or
    ffmpeg spawned by yt-dlp — survive as orphans, keep writing to the
    console and garble the UI, and hold their pipes open forever.  This
    kills the tree: ``taskkill /T`` on Windows, the process group on POSIX
    (children are spawned with ``start_new_session=True``).

    Runs even when the direct child already exited — its grandchildren may
    still be alive as orphans. ``taskkill``/``killpg`` on a dead
    pid is harmless (errors are swallowed).
    """
    if process is None:
        return
    if os.name == "nt":
        await asyncio.to_thread(
            _subprocess.run,
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )
    else:
        try:
            pgid = os.getpgid(process.pid)
            if pgid == process.pid:
                # Child leads its own process group (start_new_session=True)
                # — kill the whole group safely.
                os.killpg(pgid, signal.SIGKILL)
            else:
                # Child shares our process group — killing the group would
                # SIGKILL us too. Kill only the direct child.
                os.kill(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await process.wait()
    except (ProcessLookupError, OSError):
        pass


def desktop_notify(title: str, message: str) -> None:
    """Fire a best-effort desktop notification (cross-platform).

    Uses native platform facilities — never raises, never blocks the
    caller on failure.
    """
    system = _platform.system()
    try:
        if system == "Windows":
            _subprocess.run(
                ["powershell", "-Command",
                 f"Add-Type -AssemblyName System.Windows.Forms; "
                 f"$n = New-Object System.Windows.Forms.NotifyIcon; "
                 f"$n.Icon = [System.Drawing.SystemIcons]::Information; "
                 f"$n.BalloonTipTitle = '{title}'; "
                 f"$n.BalloonTipText = '{message}'; "
                 f"$n.Visible = $true; "
                 f"$n.ShowBalloonTip(5000);"],
                capture_output=True, timeout=10,
            )
        elif system == "Darwin":
            _subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                capture_output=True, timeout=5,
            )
        else:
            _subprocess.run(
                ["notify-send", title, message],
                capture_output=True, timeout=5,
            )
    except Exception:
        # Desktop notification is best-effort — never let it fail the caller
        pass