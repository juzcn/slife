"""Tunnel providers — "expose this local port as a public HTTPS URL".

The sharefile plugin owns exactly ONE provider instance, chosen by
``sharefile.json5``'s ``active_provider`` (see
:mod:`slife.plugins.sharefile.config`).  Providers are the only thing that
differs between tunnels: ``server.py`` talks to the :class:`TunnelProvider`
surface and never to a concrete implementation, so switching tunnels is a
config change rather than a code change.

Every provider shares one lifecycle (:class:`_TunnelProviderBase`) and supplies
only the three things that actually differ — how to establish the tunnel once,
how to tear it down, and how to tell whether it is still up:

* :class:`NgrokTunnel` drives the ngrok Python SDK, which embeds the agent as a
  native extension — no external binary.
* :class:`LocalhostRunTunnel` and :class:`CloudflareQuickTunnel` spawn a CLI
  (``ssh`` / ``cloudflared``) and read the public URL off its stdout, so they
  share :class:`_CliTunnelProvider` on top of the base.

Contract the base guarantees, and which ``server.py`` and the harness rely on:

* ``start(port)`` is **synchronous and blocking**.  It is always reached
  through ``slife.threads.run_daemon`` (a plain daemon thread), never on the
  event loop, so it must be callable from an arbitrary thread with no running
  loop — hence ``subprocess`` and not ``asyncio.create_subprocess_exec``.
* ``start()`` is single-flight and idempotent, and ``stop()`` never raises —
  it is called at shutdown while a start may still be in flight.
* ``status()`` is pure, synchronous, non-blocking and exception-free, and
  always carries a ``state`` key: ``active`` (a URL is live) / ``starting``
  (an attempt is in flight — the harness waits) / ``failed`` (terminal —
  reported only once an attempt has concluded, because the harness turns it
  into a one-time "tunnel down" warning) / ``idle`` (no attempt made).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: The provider used when the config is absent, unreadable, or names a
#: provider this build does not have.  ngrok is the historical default and
#: needs no config file at all.
DEFAULT_PROVIDER = "ngrok"

#: Providers this build can actually construct (see :func:`create_provider`).
KNOWN_PROVIDERS = frozenset({"ngrok", "localhost.run", "cloudflare"})

#: Environment variable the active tunnel publishes its URL through.  Set on
#: success, cleared on loss/stop — it is how a subagent that does NOT own the
#: tunnel still resolves the parent's URL.
_TUNNEL_URL_ENV = "SLIFE_SHAREFILE_URL"

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds
_HEALTH_INTERVAL = 30.0  # seconds between liveness probes

#: A start attempt stuck longer than this is considered dead (its daemon thread
#: is hung in credstore/forward) — a fresh attempt may supersede it.  The stale
#: thread is harmless: daemon threads die with the process.
_TUNNEL_START_TIMEOUT = 45.0

#: How long to wait for a CLI child to print its public URL.  cloudflared in
#: particular can take a while to negotiate and print its banner.
_START_TIMEOUT = 30.0

#: Output lines kept for the failure message when a CLI child dies early.
_TAIL_LINES = 20


# ═══════════════════════════════════════════════════════════════════════
# Binary / SDK discovery
# ═══════════════════════════════════════════════════════════════════════


def _which(binary: str, extra: tuple[Path, ...] = ()) -> str | None:
    """Resolve a CLI binary — PATH first (via slife's Windows-aware resolver),
    then any *extra* well-known absolute locations, in order.
    """
    from slife.platform import resolve_command

    found = shutil.which(resolve_command(binary))
    if found:
        return found
    for candidate in extra:
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_ssh_fallbacks() -> tuple[Path, ...]:
    """OpenSSH is an *optional* Windows capability — it ships with the OS but
    is not on PATH in every shell, so fall back to its install location.
    """
    if sys.platform != "win32":
        return ()
    return (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe",
    )


def _cloudflared_fallbacks() -> tuple[Path, ...]:
    """cloudflared is NOT bundled with slife and no installer installs it —
    these are the locations its own Windows installers use.
    """
    if sys.platform != "win32":
        return ()
    roots = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    return tuple(
        Path(root) / "cloudflared" / "cloudflared.exe" for root in roots if root
    )


def _import_ngrok() -> Any:
    """Import the official ngrok SDK (embeds the agent — no external binary)."""
    try:
        import ngrok
        return ngrok
    except ImportError:
        raise RuntimeError(
            "The ngrok Python SDK is not installed. Run: uv pip install ngrok"
        )


def _read_auth_token() -> str | None:
    """Read the ngrok auth token from the OS credential store or env var."""
    try:
        from credstore import get_credential  # pyright: ignore[reportMissingImports]
        token = get_credential("NGROK_AUTHTOKEN")
        if token:
            return token
    except (ImportError, OSError, ValueError):
        logger.warning("credstore_read_failed")
    return os.environ.get("NGROK_AUTHTOKEN")


def _ngrok_tunnel_alive(public_url: str) -> bool:
    """True if ngrok still lists *public_url* as a live tunnel.

    The embedded SDK does NOT report a server-side teardown (free-tier sessions
    get recycled by ngrok), so the monitor probes the API for our public URL.
    Any probe error returns True — don't flap on transient API trouble.
    """
    try:
        ngrok = _import_ngrok()
        if ngrok is None:
            return True
        get_tunnels = getattr(ngrok, "get_tunnels", None)
        if get_tunnels is None:
            return True
        return any(t.public_url == public_url for t in get_tunnels())
    except Exception:
        return True


@runtime_checkable
class TunnelProvider(Protocol):
    """What ``server.py`` needs from a tunnel.  See the module docstring."""

    def start(self, port: int) -> str: ...

    def stop(self) -> None: ...

    @property
    def is_active(self) -> bool: ...

    def status(self) -> dict[str, str]: ...

    def share_url_for(self, file_id: str) -> str | None: ...

    def start_monitor(self, port: int, on_tunnel_up=None) -> None: ...

    def stop_monitor(self) -> None: ...


# ═══════════════════════════════════════════════════════════════════════
# Shared lifecycle
# ═══════════════════════════════════════════════════════════════════════


class _TunnelProviderBase:
    """Everything a tunnel provider does apart from the transport itself.

    Subclasses implement exactly three hooks: :meth:`_do_start` (establish the
    tunnel once, return its public URL), :meth:`_teardown` (release the
    transport; must never raise) and :meth:`is_alive` (the liveness probe the
    monitor polls).
    """

    #: Name used in log lines and error messages.
    label: str = "tunnel"

    def __init__(self) -> None:
        self._public_url: str | None = None
        self._monitor_task: "asyncio.Task[None] | None" = None
        self._monitor_retries: int = 0
        self._starting: bool = False  # guard against concurrent starts
        self._starting_at: float | None = None  # monotonic time of the in-flight start
        self._start_gen: int = 0  # bumped per accepted start attempt (stale ownership)
        self._start_lock = threading.Lock()  # serializes guard mutation only
        # Set once a start attempt has concluded unsuccessfully.  Cleared on a
        # successful start or an explicit stop — lets the harness report
        # "tunnel down" as a terminal state instead of racing a live attempt.
        self._failed: bool = False
        # Factual message of the last terminal failure (empty when not failed).
        self._failure_reason: str = ""

    # ── Subclass hooks ─────────────────────────────────────────────

    def _do_start(self, port: int) -> str:
        """Establish the tunnel once and return its public URL.

        Raises on any failure; retrying is the base's job (see
        :meth:`_run_attempts`).  Called with the single-flight guard held.
        """
        raise NotImplementedError

    def _teardown(self) -> None:
        """Release the transport.  Must never raise."""

    def is_alive(self) -> bool:
        """Whether the tunnel is still up.  Synchronous — the monitor hands
        this to ``run_daemon``.
        """
        raise NotImplementedError

    # ── Properties ─────────────────────────────────────────────────

    @property
    def public_url(self) -> str | None:
        """The current tunnel's public URL, or a peer's from the env."""
        if self._public_url is not None:
            return self._public_url
        return os.environ.get(_TUNNEL_URL_ENV)

    @property
    def is_active(self) -> bool:
        """True when the tunnel is running."""
        return self.public_url is not None

    def share_url_for(self, file_id: str) -> str | None:
        """Build a public share URL for *file_id*."""
        url = self.public_url
        if url is None:
            return None
        return f"{url}/share/{file_id}"

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict[str, str]:
        """Report the tunnel's current state.  See the module docstring."""
        if self._public_url is not None:
            return {"state": "active", "url": self._public_url}
        if self._starting:
            return {"state": "starting", "url": ""}
        if self._failed:
            return {"state": "failed", "url": "", "reason": self._failure_reason}
        return {"state": "idle", "url": ""}

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self, port: int) -> str:
        """Start the tunnel to *port*.  Returns the public URL.

        Blocking and single-flight — see the module docstring.
        """
        if self._public_url is not None:
            logger.warning("tunnel_already_running url=%s", self._public_url)
            return self._public_url

        with self._start_lock:
            if self._starting:
                elapsed = time.monotonic() - (self._starting_at or time.monotonic())
                if elapsed < _TUNNEL_START_TIMEOUT:
                    logger.debug("tunnel_start_already_in_progress")
                    raise RuntimeError("Tunnel start already in progress")
                logger.warning(
                    "tunnel_start_stale_superseded elapsed=%.0fs timeout=%.0fs",
                    elapsed, _TUNNEL_START_TIMEOUT,
                )
            self._start_gen += 1
            gen = self._start_gen
            self._starting = True
            self._starting_at = time.monotonic()
        try:
            # Pessimistic: any exit below marks the attempt terminal.  Cleared
            # only on success, so ``status()`` reports "failed" once the
            # attempt has concluded.
            self._failed = True
            url = self._do_start(port)
            self._public_url = url
            os.environ[_TUNNEL_URL_ENV] = url
            self._failed = False
            self._failure_reason = ""
            logger.info("tunnel_started provider=%s port=%s url=%s", self.label, port, url)
            return url
        except Exception as e:
            self._failure_reason = str(e)  # factual last-failure message
            raise
        finally:
            # Only the current owner clears the guard — a superseded thread
            # finishing later must not clobber the newer attempt's state.
            with self._start_lock:
                if self._start_gen == gen:
                    self._starting = False
                    self._starting_at = None

    def _run_attempts(self, attempt: Callable[[], str]) -> str:
        """Retry *attempt* with linear backoff; return its public URL.

        Providers call this from :meth:`_do_start` once everything that must
        NOT be retried (a missing token, a missing binary) has been checked.
        """
        last_error: Exception | None = None
        for n in range(1, _MAX_RETRIES + 1):
            try:
                return attempt()
            except Exception as e:
                last_error = e
                self._after_failed_attempt()
                if n < _MAX_RETRIES:
                    delay = _RETRY_DELAY * n
                    logger.warning(
                        "tunnel_retry provider=%s attempt=%d/%d delay=%.1fs err=%s",
                        self.label, n, _MAX_RETRIES, delay, e,
                    )
                    time.sleep(delay)

        raise RuntimeError(
            f"Failed to start the {self.label} tunnel after {_MAX_RETRIES} attempts: "
            f"{last_error}"
        )

    def _after_failed_attempt(self) -> None:
        """Clean up between retries (a CLI provider kills its dead child)."""

    def stop(self) -> None:
        """Disconnect the tunnel.  Never raises."""
        # A stopped tunnel is never "failed" — clear the flag even when there
        # is nothing to disconnect.
        self._failed = False
        self._failure_reason = ""
        self._teardown()
        if self._public_url is None:
            return
        logger.info("tunnel_disconnected provider=%s url=%s", self.label, self._public_url)
        self._drop_url()

    def _drop_url(self) -> None:
        """Forget the published URL and un-export it."""
        self._public_url = None
        os.environ.pop(_TUNNEL_URL_ENV, None)

    # ── Health monitor ─────────────────────────────────────────────

    def start_monitor(self, port: int, on_tunnel_up=None) -> None:
        """Spawn a background monitor that restarts the tunnel on failure.

        When *on_tunnel_up* is provided, it is called (no arguments) if the
        monitor successfully starts the tunnel after a retry.
        """
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(
            self._run_monitor(port, on_tunnel_up)
        )
        logger.debug("tunnel_monitor_started provider=%s port=%s", self.label, port)

    def stop_monitor(self) -> None:
        """Cancel the health monitor."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _run_monitor(self, port: int, on_tunnel_up=None) -> None:
        """Background health loop: keep the tunnel up, restarting if it drops.

        A tunnel failure is temporary — free tiers recycle sessions and the
        transport can die at any time — so this loop never terminates; it runs
        until the monitor is cancelled (``stop_monitor`` / shutdown).
        """
        from slife.threads import run_daemon

        await asyncio.sleep(_RETRY_DELAY)  # let the daemon-thread handshake finish

        while True:
            if self._public_url is not None:
                alive = await run_daemon(self.is_alive, name=f"{self.label}-health")
                if alive:
                    await asyncio.sleep(_HEALTH_INTERVAL)
                    continue
                logger.warning(
                    "tunnel_lost provider=%s url=%s — restarting",
                    self.label, self._public_url,
                )
                self._teardown()
                self._drop_url()

            # No tunnel — (re)start.
            try:
                await run_daemon(self.start, port, name=f"{self.label}-tunnel-health")
            except Exception as e:
                self._monitor_retries += 1
                logger.warning(
                    "tunnel_restart_failed provider=%s port=%s err=%s retries=%d",
                    self.label, port, e, self._monitor_retries,
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue
            if self._public_url is not None:
                self._monitor_retries = 0
                if on_tunnel_up is not None:
                    on_tunnel_up()
            await asyncio.sleep(_HEALTH_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════
# ngrok — Python SDK, embedded agent (no external binary)
# ═══════════════════════════════════════════════════════════════════════


class NgrokTunnel(_TunnelProviderBase):
    """An ngrok HTTP tunnel driven by the official Python SDK.

    Requires an ``NGROK_AUTHTOKEN`` credential (credstore, or the env var).
    Endpoint pooling lets several slife instances share one dev domain.

    Note for the free tier: ngrok's edge answers any request whose User-Agent
    contains "Mozilla" with an interstitial splash page (``ERR_NGROK_6024``),
    so a link opened in a *browser* shows ngrok's warning rather than the file.
    Programmatic fetches — the multimodal-LLM path this tool exists for — pass
    through untouched.  ngrok forbids injecting the bypass header server-side,
    so this cannot be worked around here; pick another provider if browser
    access matters.
    """

    label = "ngrok"

    def __init__(self) -> None:
        super().__init__()
        self._listener: Any = None
        self._ngrok: Any = None

    def _do_start(self, port: int) -> str:
        # A missing token is terminal — never worth retrying.
        token = _read_auth_token()
        if not token:
            raise RuntimeError(
                "ngrok auth token not found. Register at https://ngrok.com/signup, "
                "then store the token via: credential_check NGROK_AUTHTOKEN"
            )

        self._ngrok = _import_ngrok()

        def _attempt() -> str:
            # Pooled so multiple slife instances (WSL + Windows, sub-agents on
            # different machines) can share the same ngrok dev domain — ngrok
            # load-balances across all online agents.
            self._listener = self._ngrok.forward(
                f"localhost:{port}", authtoken=token, pooling_enabled=True,
            )
            return str(self._listener.url()).rstrip("/")

        return self._run_attempts(_attempt)

    def _teardown(self) -> None:
        if self._public_url is None or self._ngrok is None:
            return
        try:
            self._ngrok.disconnect(self._public_url)
            logger.info("tunnel_disconnected provider=ngrok url=%s", self._public_url)
        except Exception as e:
            logger.warning("tunnel_disconnect_error err=%s", e)
        self._listener = None

    def is_alive(self) -> bool:
        if self._public_url is None:
            return False
        return _ngrok_tunnel_alive(self._public_url)


# ═══════════════════════════════════════════════════════════════════════
# Shared machinery for CLI-spawned tunnels
# ═══════════════════════════════════════════════════════════════════════

#: localhost.run prints, once the forward is up::
#:
#:     <id>.lhr.life tunneled with tls termination, https://<id>.lhr.life
#:
#: (an "authenticated as anonymous user" banner may precede it).  Match the
#: URL that follows the marker rather than the banner's bare hostname.
_LHR_MARKER = "tunneled with tls termination,"
_LHR_URL_RE = re.compile(r"https://[0-9A-Za-z.\-]+")


class _CliTunnelProvider(_TunnelProviderBase):
    """A tunnel built by spawning a CLI and reading its public URL off stdout.

    Subclasses supply the binary, the argument vector, a URL matcher, and the
    message to show when the binary is missing.  The spawn, the reader thread
    (which also republishes a rotated hostname), the failure tail, and the
    escalating kill are shared.
    """

    #: Binary name to look up when the config names none.
    default_binary: str = ""

    def __init__(self, options: dict | None = None) -> None:
        super().__init__()
        opts = options or {}
        self._binary: str = str(opts.get("binary") or self.default_binary)

        self._proc: subprocess.Popen | None = None
        self._pending_url: str | None = None  # set by the reader for a pending start
        self._reader: threading.Thread | None = None
        self._recent: collections.deque = collections.deque(maxlen=_TAIL_LINES)
        self._url_event = threading.Event()
        self._eof_event = threading.Event()

    # ── Subclass hooks ─────────────────────────────────────────────

    def _find_binary(self) -> str | None:
        raise NotImplementedError

    def _argv(self, binary: str, port: int) -> list[str]:
        raise NotImplementedError

    def _parse_url(self, line: str) -> str | None:
        raise NotImplementedError

    def _missing_binary_error(self) -> str:
        raise NotImplementedError

    # ── Lifecycle ──────────────────────────────────────────────────

    def _do_start(self, port: int) -> str:
        # A missing binary is terminal — never worth retrying.
        binary = self._find_binary()
        if binary is None:
            raise RuntimeError(self._missing_binary_error())
        return self._run_attempts(lambda: self._spawn(binary, port))

    def _teardown(self) -> None:
        self._kill_proc()

    def _after_failed_attempt(self) -> None:
        self._kill_proc()

    def is_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:  # noqa: BLE001 — a broken probe must not flap the tunnel
            return True

    def _spawn(self, binary: str, port: int) -> str:
        """Spawn the CLI and block until it prints the public URL (or fails)."""
        argv = self._argv(binary, port)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Own process group, so a stuck child can be killed as a group.
            kwargs["start_new_session"] = True

        self._recent.clear()
        self._url_event.clear()
        self._eof_event.clear()
        self._pending_url = None

        logger.info("tunnel_spawn provider=%s port=%s", self.label, port)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # the CLI's diagnostics explain an early exit
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        self._proc = proc

        self._reader = threading.Thread(
            target=self._read_output, args=(proc,),
            name=f"{self.label}-reader", daemon=True,
        )
        self._reader.start()

        deadline = time.monotonic() + _START_TIMEOUT
        while not self._url_event.wait(0.25):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{self.label} exited before the tunnel was up "
                    f"(code {proc.returncode}):{self._tail()}"
                )
            if self._eof_event.is_set():
                raise RuntimeError(
                    f"{self.label} closed its output without a tunnel URL:{self._tail()}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"timed out after {_START_TIMEOUT:.0f}s waiting for the "
                    f"{self.label} tunnel URL:{self._tail()}"
                )

        url = self._pending_url
        self._pending_url = None
        if not url:
            raise RuntimeError(f"{self.label} reported no tunnel URL:{self._tail()}")
        return url

    def _read_output(self, proc: subprocess.Popen) -> None:
        """Consume the child's output for the lifetime of the tunnel.

        The first URL satisfies the pending start; a later one means the
        service rotated the published hostname (localhost.run's anonymous tier
        does, every few hours), so the URL is swapped in place and re-exported
        rather than dropped.
        """
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._recent.append(line.rstrip())
                url = self._parse_url(line)
                if url is None:
                    continue
                if self._public_url is None:
                    self._pending_url = url
                    self._url_event.set()
                elif url != self._public_url:
                    logger.info(
                        "tunnel_url_rotated provider=%s old=%s new=%s",
                        self.label, self._public_url, url,
                    )
                    self._public_url = url
                    os.environ[_TUNNEL_URL_ENV] = url
        except Exception as e:  # noqa: BLE001 — never kill the reader silently
            logger.debug("tunnel_reader_error provider=%s err=%s", self.label, e)
        finally:
            self._eof_event.set()

    def _kill_proc(self) -> None:
        """Terminate the child (escalating terminate → kill) and drop the handle."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("tunnel_kill_escalated provider=%s", self.label)
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception as e:  # noqa: BLE001 — shutdown must never raise
            logger.debug("tunnel_kill_error provider=%s err=%s", self.label, e)

    def _tail(self) -> str:
        """The last output lines, for a failure message (never empty)."""
        return "\n  " + "\n  ".join(self._recent) if self._recent else " (no output)"


# ═══════════════════════════════════════════════════════════════════════
# localhost.run — SSH reverse tunnel
# ═══════════════════════════════════════════════════════════════════════


class LocalhostRunTunnel(_CliTunnelProvider):
    """An SSH reverse tunnel to localhost.run.

    Free and account-less (``nokey@localhost.run``), and — unlike the ngrok
    free tier — it shows no interstitial to any client, because TLS is
    terminated at localhost.run's edge.  The published hostname is a random
    ``*.lhr.life`` name that rotates after a few hours; the reader thread
    swaps the new one in place.  File bytes are relayed by a third party.
    """

    label = "localhost.run"
    default_binary = "ssh"

    def __init__(self, options: dict | None = None) -> None:
        opts = options or {}
        # ``ssh`` is accepted as a synonym for ``binary`` — it reads better in
        # the config for an SSH-based provider.
        merged = dict(opts)
        if "binary" not in merged and merged.get("ssh"):
            merged["binary"] = merged["ssh"]
        super().__init__(merged)

        self._host: str = str(opts.get("host") or "localhost.run")
        self._user: str = str(opts.get("user") or "nokey")
        try:
            self._remote_port: int = int(opts.get("remote_port") or 80)
        except (TypeError, ValueError):
            logger.warning(
                "localhost_run_bad_remote_port value=%r fallback=80",
                opts.get("remote_port"),
            )
            self._remote_port = 80

    def _find_binary(self) -> str | None:
        return _which(self._binary, _windows_ssh_fallbacks())

    def _argv(self, binary: str, port: int) -> list[str]:
        return [
            binary,
            "-T",  # no pseudo-tty — keep stdout line-oriented and parseable
            # Never prompt: batch mode + accept-new let an unattended daemon
            # connect on a first run without a TTY to answer a host-key prompt.
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            # If the forward cannot be set up (e.g. the remote port is taken),
            # exit instead of holding a session that tunnels nothing.
            "-o", "ExitOnForwardFailure=yes",
            "-R", f"{self._remote_port}:localhost:{port}",
            f"{self._user}@{self._host}",
        ]

    def _parse_url(self, line: str) -> str | None:
        idx = line.find(_LHR_MARKER)
        if idx == -1:
            return None
        match = _LHR_URL_RE.search(line[idx + len(_LHR_MARKER):])
        return match.group(0).rstrip("/") if match else None

    def _missing_binary_error(self) -> str:
        return (
            f"ssh client not found ({self._binary!r}). localhost.run needs an "
            "SSH client — Windows 10+ ships OpenSSH (optional feature "
            "'OpenSSH Client'); on Linux/macOS install openssh-client."
        )


# ═══════════════════════════════════════════════════════════════════════
# Cloudflare Quick Tunnel
# ═══════════════════════════════════════════════════════════════════════

#: cloudflared logs a banner box containing the URL on its own line, e.g.::
#:
#:     INF |  https://random-words-here.trycloudflare.com  |
#:
#: so the URL is matched directly rather than after a marker.
_CF_URL_RE = re.compile(r"https://[0-9A-Za-z\-]+\.trycloudflare\.com")


class CloudflareQuickTunnel(_CliTunnelProvider):
    """A Cloudflare Quick Tunnel (``cloudflared tunnel --url``).

    No account, no domain: Cloudflare hands out a random
    ``*.trycloudflare.com`` hostname, and — like localhost.run — it shows no
    interstitial, so browsers and API fetchers both get the file.  The URL is
    stable for as long as the process lives (no rotation).

    Two caveats worth knowing before selecting it:

    * ``cloudflared`` is **not** bundled with slife and nothing installs it.
      A missing binary is a terminal ``failed`` state with an actionable
      reason rather than a startup failure — but it *is* the common first-run
      state.
    * Cloudflare positions Quick Tunnels for testing/development, and its
      free-tier terms restrict proxying media through the CDN.  A tunnel that
      serves images sits in that grey area.
    """

    label = "cloudflare"
    default_binary = "cloudflared"

    def _find_binary(self) -> str | None:
        return _which(self._binary, _cloudflared_fallbacks())

    def _argv(self, binary: str, port: int) -> list[str]:
        return [
            binary,
            "tunnel",
            # Don't let a background self-update swap the binary under us.
            "--no-autoupdate",
            "--url", f"http://localhost:{port}",
        ]

    def _parse_url(self, line: str) -> str | None:
        match = _CF_URL_RE.search(line)
        return match.group(0).rstrip("/") if match else None

    def _missing_binary_error(self) -> str:
        return (
            f"cloudflared not found ({self._binary!r}). A Cloudflare Quick "
            "Tunnel needs the cloudflared binary — install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/"
            "connect-networks/downloads/ (or set 'binary' in sharefile.json5)."
        )


# ═══════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════


def create_provider(name: str, options: dict | None = None) -> TunnelProvider:
    """Build the tunnel provider named by ``sharefile.json5``.

    An unknown name falls back to ngrok with a warning — a bad config value
    must never keep the plugin from loading.
    """
    if name == "localhost.run":
        return LocalhostRunTunnel(options)
    if name == "cloudflare":
        return CloudflareQuickTunnel(options)
    if name != DEFAULT_PROVIDER:
        logger.warning(
            "sharefile_provider_unknown provider=%s fallback=%s",
            name, DEFAULT_PROVIDER,
        )
    return NgrokTunnel()
