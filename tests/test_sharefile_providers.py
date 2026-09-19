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

#: The connector registration cloudflared logs a beat AFTER the banner — the
#: line that says the edge can actually route to this machine.  Between the
#: two, the hostname exists and answers HTTP 530.
CF_REGISTERED = (
    "2026-09-10T20:00:01Z INF Registered tunnel connection connIndex=0 "
    "connection=2f1e location=lax01 protocol=quic\n"
)
CF_UNREGISTERED = (
    "2026-09-10T20:00:09Z INF Unregistered tunnel connection connIndex=0\n"
)

#: A QUIC connection that times out.  cloudflared retries — and logs NO
#: "Unregistered tunnel connection" for it, which is the whole trap: a
#: connector set scraped from the output still looks complete while the edge
#: answers HTTP 530 to every request for the published hostname.
CF_SERVE_ERROR = (
    "2026-09-10T20:00:30Z ERR Serve tunnel error error=\"datagram manager "
    "error: timeout: no recent network activity\" connIndex=0 event=0\n"
)

#: The loopback metrics API the child announces before it registers.
CF_METRICS = "2026-09-10T20:00:00Z INF Starting metrics server on 127.0.0.1:20241/metrics\n"


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


class _BannerThenSilent:
    """A child that prints its URL banner and then never registers."""

    def __init__(self, banner: str):
        self._banner = banner
        self._sent = False
        self._release = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent:
            self._sent = True
            return self._banner
        self._release.wait()  # never set — the daemon reader dies with the test
        raise StopIteration

    def close(self):
        self.closed = True


class TestCloudflareReadiness:
    """The banner is not readiness.

    cloudflared prints its hostname before it holds an edge connection, and
    for that window the hostname answers HTTP 530 — a URL published off the
    banner is a dead link the caller only discovers by fetching it.
    """

    def test_a_url_without_a_connector_is_never_published(self, monkeypatch):
        monkeypatch.setattr("slife.timeouts.timeouts.ready.sharefile_retry_delay", 0.0)
        monkeypatch.setattr("slife.timeouts.timeouts.ready.tunnel_read_url", 0.3)
        _install_fake_popen(monkeypatch, lambda: _BannerThenSilent(CF_BANNER))
        tunnel = providers.CloudflareQuickTunnel()

        with pytest.raises(RuntimeError, match="registered no connection"):
            tunnel.start(8080)

        assert tunnel._public_url is None
        assert tunnel.status()["state"] == "failed"
        # The failure has to say what the link would have done, or the next
        # reader of the log learns nothing from it.
        assert "530" in tunnel.status()["reason"]

    def test_the_url_publishes_once_a_connector_registers(self, monkeypatch):
        _install_fake_popen(
            monkeypatch, lambda: _LinesStdout([CF_BANNER, CF_REGISTERED]),
        )
        tunnel = providers.CloudflareQuickTunnel()
        try:
            url = tunnel.start(8080)

            assert url == "https://random-words-here.trycloudflare.com"
            assert tunnel._registered == {"0"}
            assert tunnel.is_alive() is True
        finally:
            tunnel.stop()

    def test_losing_every_connector_reads_as_not_alive(self):
        """The process outlives the tunnel — "alive" must not mean "running"."""
        proc = _FakeProcess(_LinesStdout([CF_BANNER, CF_REGISTERED, CF_UNREGISTERED]))
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._proc = proc
        tunnel._read_output(proc)

        assert tunnel._registered == set()
        assert tunnel.is_alive() is False  # process up, transport gone

    def test_a_superseded_attempt_cannot_feed_the_current_one(self):
        """After a retry the old reader still holds the dead child's pipe: a
        late registration line must not mark the new attempt ready."""
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._proc = _FakeProcess(_LinesStdout([]))  # the CURRENT attempt
        tunnel._read_output(_FakeProcess(_LinesStdout([CF_REGISTERED])))  # stale child

        assert tunnel._registered == set()
        assert not tunnel._url_event.is_set()

    def test_localhost_run_needs_no_registration_vocabulary(self, monkeypatch):
        """Its hostname can only be printed once the forward is up, so the
        base contract still publishes on the URL alone."""
        _install_fake_popen(monkeypatch, lambda: _LinesStdout([_lhr_line("a.lhr.life")]))
        tunnel = providers.LocalhostRunTunnel()
        try:
            assert tunnel.start(8080) == "https://a.lhr.life"
        finally:
            tunnel.stop()


class TestEdgeViaProxy:
    """The edge address a proxy in fake-ip mode answers with.

    A tunnel that registers and dies every 30-60s while publishing a link that
    answers HTTP 530 reads as Cloudflare's fault.  Measured, it was this: the
    proxy resolves the edge hostname into its fake-ip pool, so cloudflared's
    TCP dial lands on a synthetic address and times out — which no value of
    ``--protocol`` can change.  Detected off the child's own output, because
    it is otherwise invisible from inside slife.
    """

    #: The real line, from the machine that reported the flapping.
    CF_EDGE_LINE = (
        "2026-09-19T09:12:05Z INF Registered tunnel connection connIndex=0 "
        "connection=06c5c5d6 event=0 ip=198.18.0.32 location=lax07 protocol=http2"
    )

    def test_fake_ip_edge_is_flagged(self):
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._note_edge_ip(self.CF_EDGE_LINE)

        assert tunnel.edge_ip == "198.18.0.32"
        assert tunnel.edge_via_proxy is True

    def test_ipv6_fake_edge_is_flagged(self):
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._note_edge_ip("... ip=fdfe:dcba:9876::20 location=lax07")
        assert tunnel.edge_via_proxy is True

    def test_a_real_edge_address_is_not_flagged(self):
        """Otherwise every healthy tunnel reports a proxy interception."""
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._note_edge_ip("... ip=104.16.0.1 location=lax07 protocol=http2")

        assert tunnel.edge_ip == "104.16.0.1"
        assert tunnel.edge_via_proxy is False

    def test_an_unobserved_edge_is_not_flagged(self):
        """"" means "not observed", never "fine" — and must not read as a
        proxy interception on a provider that publishes no such line."""
        assert providers.CloudflareQuickTunnel().edge_via_proxy is False

    def test_the_line_is_really_read_from_the_child(self):
        """Not just the parser: the address rides the child's own stdout."""
        tunnel = providers.CloudflareQuickTunnel()
        proc = _FakeProcess(_LinesStdout([CF_BANNER, CF_REGISTERED, self.CF_EDGE_LINE]))
        tunnel._proc = proc
        tunnel._read_output(proc)

        assert tunnel.edge_via_proxy is True


class TestCloudflareLiveness:
    """Liveness comes from the transport, not from its prose.

    A QUIC connection that times out is logged as "Serve tunnel error" and
    retried, and cloudflared emits NO "Unregistered tunnel connection" for it.
    A connector set scraped from that output therefore reads healthy straight
    through an outage that answers the published hostname with HTTP 530 —
    which is exactly how a flapping tunnel came to report itself active, with
    the health monitor never firing once.
    """

    def test_a_lost_connection_that_is_never_unregistered(self, monkeypatch):
        tunnel = providers.CloudflareQuickTunnel()
        proc = _FakeProcess(_LinesStdout([CF_BANNER, CF_REGISTERED, CF_SERVE_ERROR]))
        tunnel._proc = proc
        monkeypatch.setattr(tunnel, "_metrics_ready", lambda: False)
        tunnel._read_output(proc)

        assert tunnel._registered == {"0"}  # the child's prose still claims it…
        assert tunnel.is_alive() is False   # …the transport says it is gone

    def test_a_healthy_transport_reads_as_alive(self, monkeypatch):
        tunnel = providers.CloudflareQuickTunnel()
        proc = _FakeProcess(_LinesStdout([CF_BANNER, CF_REGISTERED]))
        tunnel._proc = proc
        monkeypatch.setattr(tunnel, "_metrics_ready", lambda: True)
        tunnel._read_output(proc)

        assert tunnel.is_alive() is True

    def test_an_unanswered_probe_falls_back_to_the_childs_output(self, monkeypatch):
        """``None`` is "no answer", not "down" — refusing a share on a failed
        probe would be its own outage."""
        tunnel = providers.CloudflareQuickTunnel()
        proc = _FakeProcess(_LinesStdout([CF_BANNER, CF_REGISTERED]))
        tunnel._proc = proc
        monkeypatch.setattr(tunnel, "_metrics_ready", lambda: None)
        tunnel._read_output(proc)

        assert tunnel.is_alive() is True

    def test_picks_up_the_metrics_port_the_child_announces(self):
        tunnel = providers.CloudflareQuickTunnel()
        proc = _FakeProcess(_LinesStdout([CF_METRICS, CF_BANNER]))
        tunnel._proc = proc
        tunnel._read_output(proc)

        assert tunnel._metrics_base == "http://127.0.0.1:20241"

    def test_a_new_child_does_not_inherit_the_dead_ones_metrics_port(self, monkeypatch):
        """A probe aimed at the previous child's port would answer for a tunnel
        that no longer exists."""
        _install_fake_popen(monkeypatch, _LinesStdout([]))
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._metrics_base = "http://127.0.0.1:20241"

        with pytest.raises(RuntimeError):
            tunnel._spawn("cloudflared", 8080)  # child that never prints a URL

        assert tunnel._metrics_base is None

    @pytest.mark.parametrize("count, expected", [(1, True), (0, False)])
    def test_the_probe_reads_ready_connections(self, monkeypatch, count, expected):
        """cloudflared answers ``{"status":200,"readyConnections":N}``."""
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._metrics_base = "http://127.0.0.1:20241"

        class _Resp:
            def json(self):
                return {"status": 200 if count else 503, "readyConnections": count}

        class _Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url):
                assert url == "http://127.0.0.1:20241/ready"
                return _Resp()

        monkeypatch.setattr(providers.httpx2, "Client", _Client)
        assert tunnel._metrics_ready() is expected

    def test_a_probe_that_cannot_connect_is_not_an_outage(self, monkeypatch):
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._metrics_base = "http://127.0.0.1:20241"

        class _Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url):
                raise OSError("connection refused")

        monkeypatch.setattr(providers.httpx2, "Client", _Client)
        assert tunnel._metrics_ready() is None

    def test_no_metrics_server_means_nothing_to_ask(self):
        assert providers.CloudflareQuickTunnel()._metrics_ready() is None


class TestIsReachable:
    """``is_active`` says a URL exists; ``is_reachable`` says it would be
    served.  They come apart exactly when the transport lost the edge."""

    def test_false_before_any_url_is_published(self):
        assert providers.CloudflareQuickTunnel().is_reachable() is False

    def test_follows_the_transport_not_the_url(self, monkeypatch):
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._public_url = "https://x.trycloudflare.com"
        monkeypatch.setattr(tunnel, "is_alive", lambda: False)

        assert tunnel.is_active is True    # the URL is still published…
        assert tunnel.is_reachable() is False   # …and nothing is behind it

    def test_a_broken_probe_never_refuses_a_share(self, monkeypatch):
        """Refusing on a probe that cannot answer would turn a broken probe
        into an outage of its own."""
        tunnel = providers.CloudflareQuickTunnel()
        tunnel._public_url = "https://x.trycloudflare.com"

        def _boom():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(tunnel, "is_alive", _boom)
        assert tunnel.is_reachable() is True


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
        monkeypatch.setattr("slife.timeouts.timeouts.ready.sharefile_retry_delay", 0.0)
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
        monkeypatch.setattr("slife.timeouts.timeouts.ready.sharefile_retry_delay", 0.0)
        monkeypatch.setattr("slife.timeouts.timeouts.ready.tunnel_read_url", 0.3)
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
        captured = _install_fake_popen(
            monkeypatch, _LinesStdout([CF_BANNER, CF_REGISTERED]),
        )
        url = providers.CloudflareQuickTunnel().start(8080)

        assert url == "https://random-words-here.trycloudflare.com"
        argv = captured[0][0]
        assert argv[0] == "cloudflared"
        assert argv[1] == "tunnel"
        assert "--no-autoupdate" in argv  # no self-update under a live daemon
        assert "http://localhost:8080" in argv

    def test_defaults_to_http2_over_the_edge(self, monkeypatch):
        """QUIC is cloudflared's default and the one that fails behind a proxy
        or TUN adapter — it registers, then loses the connection repeatedly,
        and the published URL answers 530 in between.  http2 rides TCP."""
        captured = _install_fake_popen(
            monkeypatch, _LinesStdout([CF_BANNER, CF_REGISTERED]),
        )
        providers.CloudflareQuickTunnel().start(8080)

        argv = captured[0][0]
        assert argv[argv.index("--protocol") + 1] == "http2"

    def test_protocol_option_is_honoured(self, monkeypatch):
        captured = _install_fake_popen(
            monkeypatch, _LinesStdout([CF_BANNER, CF_REGISTERED]),
        )
        providers.CloudflareQuickTunnel({"protocol": "quic"}).start(8080)

        argv = captured[0][0]
        assert argv[argv.index("--protocol") + 1] == "quic"

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
