"""Platform detection and platform-aware utilities."""

import asyncio
import ctypes
import logging
import os
import shutil
import signal
import subprocess as _subprocess
import sys
import time
import platform as _platform

IS_WINDOWS = sys.platform == "win32"

from slife.threads import run_daemon
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)


def _taskkill_tree_sync(pid: int, label: str = "") -> None:
    """Kill *pid* and its whole process tree (Windows).  Never raises.

    Best-effort and bounded: a wedged ``taskkill`` must not block the caller
    forever, and a missing ``taskkill`` or an already-dead pid is not a
    cleanup failure.  The one sync primitive every Windows tree kill goes
    through — :func:`kill_process_tree` (async callers), and the two
    terminate ladders below (which otherwise kill the direct child alone).
    """
    try:
        _subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            timeout=_timeouts.timeouts.grace.force,
        )
    except Exception as e:  # noqa: BLE001 — a cleanup path must never raise
        logger.debug("taskkill_failed pid=%s label=%s err=%s", pid, label, e)


def _descendant_pids(pid: int) -> list[int]:
    """Every live descendant of *pid* (POSIX), parents before children.

    Read from ``ps`` rather than reached with a process-group kill: a
    plugin's own child — the sharefile tunnel's cloudflared — is spawned in
    its OWN session on purpose, so a stuck tunnel can be killed as a group,
    which puts it outside the plugin's group where ``killpg`` cannot find
    it.  The walk must happen while the tree is INTACT: once the parent dies
    its children are reparented to init and the link is gone, so the caller
    reads the tree before signalling anything.

    Never raises — a missing ``ps`` costs the sweep, not the stop.
    """
    if IS_WINDOWS:
        return []
    try:
        out = _subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True, text=True,
            timeout=_timeouts.timeouts.grace.force,
        ).stdout
    except Exception as e:  # noqa: BLE001 — best-effort cleanup
        logger.debug("ps_tree_failed pid=%s err=%s", pid, e)
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = list(children.get(pid, ()))
    while queue:
        current = queue.pop(0)
        found.append(current)
        queue.extend(children.get(current, ()))
    return found


def _signal_pids_sync(
    pids: list[int], sig: int = signal.SIGTERM, label: str = "",
) -> None:
    """Signal each pid, ignoring the ones already gone (POSIX).  Never raises.

    SIGTERM by default — a leaked tunnel child is an ordinary process that
    should still be allowed to deregister itself, the same signal its own
    provider would have sent it.
    """
    for target in pids:
        try:
            os.kill(target, sig)
        except OSError:
            pass
    if pids:
        logger.debug("tree_signalled sig=%s pids=%s label=%s", sig, pids, label)


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

    The ``taskkill`` call runs on a daemon thread (:func:`slife.threads.run_daemon`)
    — never the default executor, whose non-daemon workers are joined at
    interpreter exit and would hang it on a wedged child (the ``threads.py``
    invariant).
    """
    if process is None:
        return
    if os.name == "nt":
        await run_daemon(_taskkill_tree_sync, process.pid, name="taskkill-tree")
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


# ── Windows: kill-on-close job object ───────────────────────────────
#
# The explicit kill paths above share one blind spot: they only run while
# slife is alive and unwinding.  A hard-killed parent runs no code at all —
# Windows ``terminate()`` is TerminateProcess, Task Manager's End Task is the
# same, and neither unwinds a Python ``finally``.  Everything a plugin had
# spawned underneath it (the sharefile tunnel's cloudflared, every external
# MCP server the gateway runs) is then orphaned: still connected, still
# holding resources, and — for the tunnel — still handing out a public URL
# whose origin port died with its owner.
#
# A job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE closes that hole at
# the kernel: the job owns the processes, and when the last handle to it
# closes — which happens when slife's process dies, for ANY reason — the
# kernel terminates whatever is still inside.  Children inherit the job, so
# grandchildren are covered without naming any of them.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

#: The process-wide job every spawned child is assigned to.  Created once and
#: deliberately never closed: the handle IS the job's lifetime, so holding it
#: for the whole run is what makes "slife dies → its tree dies" true.
_job_handle: int | None = None


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_job() -> int | None:
    """The process-wide kill-on-close job object, or ``None`` when unavailable.

    Never raises: a job that cannot be created (an ancient Windows, a
    restricted token) degrades to the explicit-kill behaviour, which is what
    every platform had before this existed.
    """
    global _job_handle
    if not IS_WINDOWS:
        return None
    if _job_handle is not None:
        return _job_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.debug("job_create_failed err=%s", ctypes.get_last_error())
            return None
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        if not kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            logger.debug("job_set_info_failed err=%s", ctypes.get_last_error())
            return None
        _job_handle = handle
        logger.debug("job_created kill_on_close=1")
        return handle
    except Exception as e:  # noqa: BLE001 — a missing guarantee is not a crash
        logger.debug("job_unavailable err=%s", e)
        return None


def assign_to_job_object(pid: int, label: str = "") -> bool:
    """Put *pid* (and everything it spawns) into the kill-on-close job.

    Returns whether the assignment took.  Never raises — a child that cannot
    be assigned keeps the older explicit-kill cleanup instead of failing its
    own startup.  Windows-only; a no-op returning False elsewhere.
    """
    if not IS_WINDOWS or pid <= 0:
        return False
    handle = _windows_job()
    if handle is None:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        process_handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
        )
        if not process_handle:
            logger.debug("job_open_process_failed pid=%s label=%s err=%s",
                         pid, label, ctypes.get_last_error())
            return False
        try:
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                logger.debug("job_assign_failed pid=%s label=%s err=%s",
                             pid, label, ctypes.get_last_error())
                return False
        finally:
            kernel32.CloseHandle(process_handle)
        logger.debug("job_assigned pid=%s label=%s", pid, label)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("job_assign_error pid=%s label=%s err=%s", pid, label, e)
        return False


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


def get_os_info() -> str:
    """Return a human-readable OS identifier.

    Returns one of: "Windows", "Linux", "macOS".
    """
    system = _platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    if system == "Linux":
        return "Linux"
    return system  # Fallback for other platforms (e.g. "FreeBSD")


def detect_current_shell() -> str:
    """Detect the shell that launched slife.

    Returns ``"powershell"``, ``"bash"``, ``"cmd"``, or ``"sh"``.
    """
    if os.name != "nt":
        return os.environ.get("SHELL", "sh")
    # On Windows, cmd.exe sets PROMPT in the environment (default "$P$G");
    # PowerShell does not.  A machine with PowerShell installed always has
    # PSModulePath, so relying on it alone would misclassify a cmd.exe-launched
    # session as PowerShell and route every shell command (cmd syntax) through
    # the wrong shell.
    if os.environ.get("PROMPT") is not None:
        return "cmd"
    if os.environ.get("PSModulePath"):
        return "powershell"
    return "cmd"


def _resolve_skill_script(script_path: str) -> str:
    """Resolve a ``skills/…`` path to the actual install location.

    Skills live in ``<data_dir>/skills/`` — the project root in dev
    mode, ``~/.slife/skills/`` in production.  Returns the absolute
    path if the file exists; otherwise returns the original path
    unchanged.
    """
    from slife.paths import get_skills_dir

    if script_path.startswith(("skills/", "skills\\")):
        skills_dir = get_skills_dir()
        rel = script_path[len("skills/"):].lstrip("/\\") if script_path.startswith("skills/") else script_path[len("skills\\"):].lstrip("/\\")
        resolved = skills_dir / rel
        if resolved.is_file():
            return str(resolved)
    return script_path


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
    graceful_timeout: float | None = None,
    force_timeout: float | None = None,
    label: str = "",
) -> None:
    """Gracefully terminate an asyncio subprocess with escalating force.

    ``graceful_timeout`` / ``force_timeout`` default to the registry's
    grace.gentle / grace.force (call-time lookup).

    1. Close stdin to signal EOF.
    2. Read the child's descendants (POSIX — done here, while the tree is
       still intact), then send SIGTERM / ``taskkill /T`` on Windows.
    3. Wait *graceful_timeout* seconds for graceful exit.
    4. Force-kill if still running.
    5. Wait *force_timeout* seconds for kill to take effect.
    6. Sweep any descendant the child left behind (POSIX).
    7. Close remaining pipe transports (prevents ``ResourceWarning``
       on Windows ProactorEventLoop where the pipe handle is already
       invalid by the time ``__del__`` runs).

    Swallows ``ProcessLookupError`` (already exited) and logs otherwise.
    """
    if graceful_timeout is None:
        graceful_timeout = _timeouts.timeouts.grace.gentle
    if force_timeout is None:
        force_timeout = _timeouts.timeouts.grace.force
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

            # Graceful termination.  Windows has no graceful signal to send —
            # terminate() IS TerminateProcess — and killing the direct child
            # alone leaves whatever it spawned running, so the whole tree goes
            # in one taskkill (the same fix the sync ladder below carries).
            # POSIX: the tree is read BEFORE anything is signalled — a dead
            # parent's children are reparented to init and unfindable.
            descendants = [] if IS_WINDOWS else await run_daemon(
                _descendant_pids, process.pid, name="ps-tree",
            )
            if IS_WINDOWS:
                await run_daemon(
                    _taskkill_tree_sync, process.pid, label, name="taskkill-tree",
                )
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
            if descendants:
                # Whatever the child left behind: a plugin killed before its
                # own teardown ran still owns a live cloudflared, which the
                # process-group kill cannot reach (it leads its own session).
                # It also holds the stdout/stderr pipe it inherited, and
                # asyncio does not finish wait() while a pipe is open — so
                # without this sweep the wait above runs out its timeout and
                # reports a force-kill for a child that died immediately.
                await run_daemon(_signal_pids_sync, descendants, name="tree-sweep")
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


def terminate_process_sync(
    process: asyncio.subprocess.Process,
    *,
    timeout: float | None = None,
    label: str = "",
) -> None:
    """Synchronous best-effort child process termination.

    ``timeout`` defaults to the registry's grace.gentle (call-time lookup).

    Crash-path version of :func:`terminate_process` for ``finally`` blocks
    where no event loop is running — ``await process.wait(...)`` and
    ``asyncio.wait_for`` are out, and ``Process.wait(timeout=…)`` doesn't
    exist (that signature belongs to ``subprocess.Popen.wait``).

    Windows: ``terminate()`` is ``TerminateProcess`` (immediate hard kill),
    so waiting is pointless and skipped entirely — one ``taskkill /T`` takes
    the tree instead.
    POSIX: read the tree first (before anything dies and its children are
    reparented away), send SIGTERM, poll ``os.waitpid(pid, os.WNOHANG)`` for
    up to *timeout* seconds, escalate to SIGKILL, then sweep whatever the
    child left behind.

    Best-effort: never raises; terminate/kill errors are logged at debug.
    """
    if timeout is None:
        timeout = _timeouts.timeouts.grace.gentle  # call-time lookup
    if process is None or process.returncode is not None:
        return
    tag = f"label={label} " if label else ""
    if IS_WINDOWS:
        # TerminateProcess kills ONE process, and this ladder runs from the
        # Ctrl+C `finally` — where the children a plugin spawned underneath
        # itself (the sharefile tunnel's cloudflared, the external MCP servers
        # the gateway runs) would be orphaned by a single-process kill.  One
        # taskkill /T takes the tree; nothing to wait for, TerminateProcess is
        # not refusable.
        try:
            _taskkill_tree_sync(process.pid, label)
        except Exception as e:  # noqa: BLE001 — this runs in a shutdown finally
            logger.debug("terminate_process_sync_kill_error %spid=%s err=%s",
                         tag, process.pid, e)
        return
    # POSIX: read the tree BEFORE the signal — once this child dies its own
    # children are reparented to init and can no longer be found.  The sweep
    # runs from a `finally` so the early exits below (already exited, reaped)
    # still take it: a plugin that died without cleaning up still has a live
    # cloudflared underneath it.
    descendants = _descendant_pids(process.pid)
    try:
        try:
            process.terminate()
        except Exception:
            logger.debug("terminate_process_sync_terminate_error %spid=%s", tag, process.pid, exc_info=True)
            return
        pid = process.pid
        deadline = time.monotonic() + timeout
        while True:
            try:
                _, status = os.waitpid(pid, os.WNOHANG)  # type: ignore[attr-defined]
            except OSError:
                return  # Already reaped / exited — nothing more to do.
            if status != 0:
                return  # Exited.
            if time.monotonic() >= deadline:
                break
            time.sleep(_timeouts.timeouts.pacing.reap_poll)
        logger.warning("terminate_process_sync_force_kill %spid=%s", tag, pid)
        try:
            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except OSError:
            pass
    finally:
        if descendants:
            _signal_pids_sync(descendants, label=label)


def _ps_quote(value: str) -> str:
    """Escape a string for a PowerShell single-quoted literal (``'`` → ``''``)."""
    return value.replace("'", "''")


def _applescript_quote(value: str) -> str:
    """Escape a string for an AppleScript double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def desktop_notify(title: str, message: str) -> None:
    """Fire a best-effort desktop notification (cross-platform).

    Uses native platform facilities — never raises, never blocks the
    caller on failure.  Title/message are quoted for the shell fragment
    they're interpolated into: PowerShell and AppleScript both break on
    an embedded apostrophe/quote, which would silently drop the
    notification.
    """
    system = _platform.system()
    try:
        if system == "Windows":
            _subprocess.run(
                ["powershell", "-Command",
                 f"Add-Type -AssemblyName System.Windows.Forms; "
                 f"$n = New-Object System.Windows.Forms.NotifyIcon; "
                 f"$n.Icon = [System.Drawing.SystemIcons]::Information; "
                 f"$n.BalloonTipTitle = '{_ps_quote(title)}'; "
                 f"$n.BalloonTipText = '{_ps_quote(message)}'; "
                 f"$n.Visible = $true; "
                 f"$n.ShowBalloonTip(5000);"],
                capture_output=True, timeout=10,  # noqa-timeout — desktop notify, sync + best-effort (never fails the caller)
            )
        elif system == "Darwin":
            _subprocess.run(
                ["osascript", "-e",
                 f'display notification "{_applescript_quote(message)}" with title "{_applescript_quote(title)}"'],
                capture_output=True, timeout=5,  # noqa-timeout — desktop notify, sync + best-effort
            )
        else:
            _subprocess.run(
                ["notify-send", title, message],
                capture_output=True, timeout=5,  # noqa-timeout — desktop notify, sync + best-effort
            )
    except Exception:
        # Desktop notification is best-effort — never let it fail the caller
        pass
