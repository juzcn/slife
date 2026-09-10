"""Tunnel providers — the factory, the shared lifecycle, and the CLI providers.

The CLI child is faked by swapping the ``subprocess`` *module object* inside
``slife.plugins.sharefile.providers`` (not the stdlib module) and the binary
lookup on the shared base class, so no test ever opens a real connection and no
other code in the process sees the fake.
"""

from __future__ import annotations

import os
import subprocess
import threading
import types

import pytest

from slife.plugins.sharefile import providers

pytestmark = pytest.mark.unit


# ── Fakes ─────────────────────────────────────────────────────────────


class _LinesStdout:
    """A child's stdout: the scripted lines, then EOF."""

    def __init__(self, lines):
        self._it = iter(lines)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed = True


class _SilentStdout:
    """A child that connected but never prints a URL — blocks the reader."""

    def __init__(self):
        self._release = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        self._release.wait()  # never set — the daemon reader dies with the test
        raise StopIteration

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, stdout, returncode=None):
        self.stdout = stdout
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _install_fake_popen(monkeypatch, stdout, returncode=None, captured=None):
    """Fake the CLI spawn and the binary lookup.

    *stdout* may be a factory — a retrying start spawns a *new* child each
    attempt, so a shared iterator would be exhausted after the first one.
    """
    captured = captured if captured is not None else []
    make_stdout = stdout if callable(stdout) else (lambda: stdout)

    def _popen(argv, **kwargs):
        captured.append((argv, kwargs))
        return _FakeProcess(make_stdout(), returncode)

    fake = types.SimpleNamespace(
        Popen=_popen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
        DEVNULL=subprocess.DEVNULL,
        CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(providers, "subprocess", fake)
    # Fake only the "is it installed?" lookup — every configured binary
    # resolves to itself, so the option plumbing stays under test (and no test
    # ever finds a REAL ssh on PATH and connects to a real service).
    monkeypatch.setattr(providers, "_which", lambda binary, extra=(): binary)
    return captured


def _install_missing_binary(monkeypatch):
    monkeypatch.setattr(providers, "_which", lambda binary, extra=(): None)


def _lhr_line(host: str) -> str:
    return f"{host} tunneled with tls termination, https://{host}\n"


#: cloudflared prints the URL inside a banner box, on its own line.
CF_BANNER = (
    "2026-09-10T20:00:00Z INF |  Your quick Tunnel has been created!  |\n"
    "2026-09-10T20:00:00Z INF |  https://random-words-here.trycloudflare.com  |\n"
)


@pytest.fixture(autouse=True)
def _clean_tunnel_env(monkeypatch):
    """The providers write os.environ directly — undo that per test."""
    monkeypatch.delenv("SLIFE_SHAREFILE_URL", raising=False)
    yield
    os.environ.pop("SLIFE_SHAREFILE_URL", None)


# ── factory + protocol ────────────────────────────────────────────────


class TestCreateProvider:
    def test_ngrok_is_the_default(self):
        assert isinstance(providers.create_provider("ngrok"), providers.NgrokTunnel)

    def test_builds_localhost_run(self):
        p = providers.create_provider("localhost.run", {"user": "nokey"})
        assert isinstance(p, providers.LocalhostRunTunnel)

    def test_builds_cloudflare(self):
        p = providers.create_provider("cloudflare", {})
        assert isinstance(p, providers.CloudflareQuickTunnel)

    def test_unknown_name_falls_back_to_ngrok(self):
        assert isinstance(providers.create_provider("made-up"), providers.NgrokTunnel)

    def test_every_known_provider_is_constructible(self):
        for name in providers.KNOWN_PROVIDERS:
            assert isinstance(
                providers.create_provider(name), providers.TunnelProvider
            ), name

    def test_every_provider_shares_the_lifecycle_base(self):
        """One lifecycle for all three — nothing re-implements status/start/stop."""
        for name in providers.KNOWN_PROVIDERS:
            assert isinstance(
                providers.create_provider(name), providers._TunnelProviderBase
            ), name


# ── URL parsing ───────────────────────────────────────────────────────


class TestLocalhostRunUrlParsing:
    def test_extracts_the_url_after_the_marker(self):
        line = "0b1c8d2351fa97.lhr.life tunneled with tls termination, https://0b1c8d2351fa97.lhr.life\n"
        assert providers.LocalhostRunTunnel()._parse_url(line) == "https://0b1c8d2351fa97.lhr.life"

    def test_ignores_the_anonymous_banner(self):
        assert providers.LocalhostRunTunnel()._parse_url("authenticated as anonymous user\n") is None

    def test_ignores_unrelated_output(self):
        assert providers.LocalhostRunTunnel()._parse_url("Warning: Permanently added\n") is None

    def test_strips_a_trailing_slash(self):
        line = "x.lhr.life tunneled with tls termination, https://x.lhr.life/\n"
        assert providers.LocalhostRunTunnel()._parse_url(line) == "https://x.lhr.life"


class TestCloudflareUrlParsing:
    def test_extracts_the_url_from_the_banner(self):
        for line in CF_BANNER.splitlines(keepends=True):
            url = providers.CloudflareQuickTunnel()._parse_url(line)
            if url:
                break
        assert url == "https://random-words-here.trycloudflare.com"

    def test_ignores_other_lines(self):
        assert providers.CloudflareQuickTunnel()._parse_url("INF Requesting new quick Tunnel\n") is None

    def test_ignores_a_non_quicktunnel_host(self):
        line = "INF |  https://dashboard.cloudflare.com  |\n"
        assert providers.CloudflareQuickTunnel()._parse_url(line) is None


# ── shared lifecycle (exercised through the CLI providers) ────────────


class TestStart:
    def test_parses_the_url_and_publishes_it(self, monkeypatch):
        _install_fake_popen(
            monkeypatch,
            _LinesStdout([
                "authenticated as anonymous user\n",
                _lhr_line("0b1c8d2351fa97.lhr.life"),
            ]),
        )
        tunnel = providers.LocalhostRunTunnel({"host": "localhost.run"})
        url = tunnel.start(8080)

        assert url == "https://0b1c8d2351fa97.lhr.life"
        assert tunnel.is_active is True
        assert os.environ["SLIFE_SHAREFILE_URL"] == url
        assert tunnel.share_url_for("tok") == f"{url}/share/tok"
        assert tunnel.status() == {"state": "active", "url": url}

    def test_second_start_reuses_the_live_tunnel(self, monkeypatch):
        captured = _install_fake_popen(monkeypatch, _LinesStdout([_lhr_line("a.lhr.life")]))
        tunnel = providers.LocalhostRunTunnel()
        first = tunnel.start(8080)
        assert tunnel.start(8080) == first
        assert len(captured) == 1  # no second child

    def test_start_while_another_is_in_flight_is_refused(self):
        tunnel = providers.LocalhostRunTunnel()
        tunnel._starting = True
        with pytest.raises(RuntimeError, match="already in progress"):
            tunnel.start(8080)

    def test_missing_binary_is_a_terminal_failure(self, monkeypatch):
        _install_missing_binary(monkeypatch)
        tunnel = providers.LocalhostRunTunnel()
        with pytest.raises(RuntimeError, match="ssh client not found"):
            tunnel.start(8080)

        status = tunnel.status()
        assert status["state"] == "failed"
        assert "ssh client not found" in status["reason"]

    def test_child_exiting_early_reports_its_output(self, monkeypatch):
        monkeypatch.setattr(providers, "_RETRY_DELAY", 0.0)
        _install_fake_popen(
            monkeypatch,
            lambda: _LinesStdout(["Permission denied (publickey).\n"]),
            returncode=255,
        )
        tunnel = providers.LocalhostRunTunnel()
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            tunnel.start(8080)

        reason = tunnel.status()["reason"]
        assert "code 255" in reason
        assert "Permission denied" in reason  # the tail explains the failure

    def test_silence_times_out_instead_of_hanging(self, monkeypatch):
        monkeypatch.setattr(providers, "_RETRY_DELAY", 0.0)
        monkeypatch.setattr(providers, "_START_TIMEOUT", 0.3)
        _install_fake_popen(monkeypatch, _SilentStdout())
        tunnel = providers.LocalhostRunTunnel()
        with pytest.raises(RuntimeError, match="timed out"):
            tunnel.start(8080)


class TestLocalhostRunArgv:
    def test_non_interactive_and_forwards_the_port(self, monkeypatch):
        captured = _install_fake_popen(monkeypatch, _LinesStdout([_lhr_line("a.lhr.life")]))
        providers.LocalhostRunTunnel({"remote_port": 80}).start(8080)

        argv = captured[0][0]
        assert argv[-1] == "nokey@localhost.run"
        assert "80:localhost:8080" in argv
        joined = " ".join(argv)
        # An unattended daemon must never sit on an interactive prompt.
        assert "BatchMode=yes" in joined
        assert "StrictHostKeyChecking=accept-new" in joined
        assert "-T" in argv  # no pseudo-tty — stdout stays line-oriented

    def test_binary_option_is_honoured(self, monkeypatch):
        captured = _install_fake_popen(monkeypatch, _LinesStdout([_lhr_line("a.lhr.life")]))
        providers.LocalhostRunTunnel({"binary": "/opt/ssh"}).start(8080)
        assert captured[0][0][0] == "/opt/ssh"

    def test_default_binary_is_ssh(self, monkeypatch):
        captured = _install_fake_popen(monkeypatch, _LinesStdout([_lhr_line("a.lhr.life")]))
        providers.LocalhostRunTunnel().start(8080)
        assert captured[0][0][0] == "ssh"

    def test_ssh_key_is_accepted_as_a_synonym_for_binary(self, monkeypatch):
        captured = _install_fake_popen(monkeypatch, _LinesStdout([_lhr_line("a.lhr.life")]))
        providers.LocalhostRunTunnel({"ssh": "/opt/ssh"}).start(8080)
        assert captured[0][0][0] == "/opt/ssh"


class TestCloudflareArgv:
    def test_runs_the_quick_tunnel_for_the_local_port(self, monkeypatch):
        captured = _install_fake_popen(monkeypatch, _LinesStdout([CF_BANNER]))
        url = providers.CloudflareQuickTunnel().start(8080)

        assert url == "https://random-words-here.trycloudflare.com"
        argv = captured[0][0]
        assert argv[0] == "cloudflared"
        assert argv[1] == "tunnel"
        assert "--no-autoupdate" in argv  # no self-update under a live daemon
        assert "http://localhost:8080" in argv

    def test_missing_binary_explains_how_to_install(self, monkeypatch):
        _install_missing_binary(monkeypatch)
        tunnel = providers.CloudflareQuickTunnel()
        with pytest.raises(RuntimeError, match="cloudflared not found"):
            tunnel.start(8080)
        assert "cloudflared not found" in tunnel.status()["reason"]


class TestStopAndHealth:
    def test_stop_clears_everything_and_is_idempotent(self, monkeypatch):
        _install_fake_popen(monkeypatch, _LinesStdout([_lhr_line("a.lhr.life")]))
        tunnel = providers.LocalhostRunTunnel()
        tunnel.start(8080)
        proc = tunnel._proc

        tunnel.stop()
        assert proc.terminated is True
        assert tunnel._proc is None
        assert tunnel.status() == {"state": "idle", "url": ""}
        assert "SLIFE_SHAREFILE_URL" not in os.environ

        tunnel.stop()  # no raise
        assert tunnel.status() == {"state": "idle", "url": ""}

    def test_is_alive_follows_the_child(self):
        tunnel = providers.LocalhostRunTunnel()
        assert tunnel.is_alive() is False  # never started

        proc = _FakeProcess(_LinesStdout([]))
        tunnel._proc = proc
        assert tunnel.is_alive() is True
        proc.returncode = 0  # the child died
        assert tunnel.is_alive() is False

    def test_rotated_url_is_swapped_in_place(self):
        tunnel = providers.LocalhostRunTunnel()
        tunnel._public_url = "https://old.lhr.life"
        os.environ["SLIFE_SHAREFILE_URL"] = "https://old.lhr.life"

        tunnel._read_output(_FakeProcess(_LinesStdout([_lhr_line("new.lhr.life")])))

        assert tunnel._public_url == "https://new.lhr.life"
        assert os.environ["SLIFE_SHAREFILE_URL"] == "https://new.lhr.life"
        assert tunnel.status() == {"state": "active", "url": "https://new.lhr.life"}


class TestStatus:
    def test_idle_before_any_attempt(self):
        assert providers.LocalhostRunTunnel().status() == {"state": "idle", "url": ""}

    def test_starting_while_an_attempt_is_in_flight(self):
        tunnel = providers.LocalhostRunTunnel()
        tunnel._starting = True
        assert tunnel.status() == {"state": "starting", "url": ""}

    def test_active_is_derived_from_the_url_only(self, monkeypatch):
        tunnel = providers.LocalhostRunTunnel()
        # A peer's URL in the env makes the tunnel usable (subagent reuse) but
        # is not this provider's own live tunnel.
        monkeypatch.setenv("SLIFE_SHAREFILE_URL", "https://peer.lhr.life")
        assert tunnel.is_active is True
        assert tunnel.status()["state"] == "idle"

    def test_missing_remote_port_option_does_not_raise(self):
        assert providers.LocalhostRunTunnel({"remote_port": "nope"})._remote_port == 80
