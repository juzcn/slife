"""Tests for local_embed.adopt — adopting a running local-embed, and takeover.

The fixed port IS the service's identity: hosts' embeddings ``base_url`` points
at it, not at whatever port this process serves.  So a local-embed already on
the port is something to stand behind, not a conflict to report — while a
stranger on it is still a hard error.

``probe_service`` is exercised against a REAL loopback HTTP server (stdlib
``http.server``) rather than a mocked ``urlopen``: the shape check that
separates "another local-embed" from "some other service" is the whole point,
and it only means something if a request actually crosses a socket.
"""

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

from local_embed import adopt
from local_embed.adopt import Adoption, get_adoption, probe_service, set_adoption

# Aliased, not imported as ``__check``: a bare ``__check`` referenced inside a
# test class body is name-mangled to ``_ClassName__check`` and the call becomes
# a NameError.
from local_embed.server import __check as check_tool
from local_embed.server import _eager_load_autoload, main, set_engine


# ── A real loopback service to probe ────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    body: bytes = b"{}"
    code: int = 200

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # keep the test output clean
        pass


@contextmanager
def _serving(body: bytes, code: int = 200):
    """Run a real HTTP server on a free loopback port; yield that port."""
    handler = type("_H", (_Handler,), {"body": body, "code": code})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # A short poll interval keeps shutdown() from costing its 0.5 s default
    # in every test that probes a real service.
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
    )
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _free_port() -> int:
    """A port nothing is listening on (bound then released)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _reset_adoption():
    """Adoption is module state — never leak it across tests."""
    set_adoption(None)
    yield
    set_adoption(None)


# ── probe_service ───────────────────────────────────────────────────────


class TestProbeService:
    def test_recognises_a_local_embed(self):
        body = json.dumps({
            "status": "ok",
            "models": [{"name": "bge-m3", "dimension": 1024, "loaded": True}],
        }).encode()
        with _serving(body) as port:
            payload = probe_service("127.0.0.1", port)
        assert payload is not None
        assert payload["models"][0]["name"] == "bge-m3"

    def test_an_unloaded_model_is_still_a_local_embed(self):
        """Lazy loading means "not loaded" is normal — adoption must not
        require the running instance to be warm."""
        body = json.dumps({
            "status": "degraded",
            "models": [{"name": "bge-m3", "loaded": False, "available": True}],
        }).encode()
        with _serving(body) as port:
            payload = probe_service("127.0.0.1", port)
        assert payload is not None
        assert payload["models"][0]["loaded"] is False

    def test_a_stranger_on_the_port_is_not_ours(self):
        """A JSON service that is not a local-embed must NOT be adopted —
        pointing the embeddings config at it would hide the real conflict."""
        with _serving(b'{"hello": "world"}') as port:
            assert probe_service("127.0.0.1", port) is None

    def test_a_models_key_that_is_not_a_list_is_not_ours(self):
        with _serving(b'{"models": "nope"}') as port:
            assert probe_service("127.0.0.1", port) is None

    def test_a_non_json_body_is_not_ours(self):
        with _serving(b"<html>hello</html>") as port:
            assert probe_service("127.0.0.1", port) is None

    def test_nothing_listening_is_none(self):
        assert probe_service("127.0.0.1", _free_port(), timeout=0.5) is None

    def test_a_configured_proxy_is_bypassed(self, monkeypatch):
        """A loopback probe must not be routed through a proxy — on a machine
        with one configured (env, or WinINET on Windows) the request would
        leave the box and adoption would silently never happen."""
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        body = json.dumps({"status": "ok", "models": [{"name": "bge-m3"}]}).encode()
        with _serving(body) as port:
            payload = probe_service("127.0.0.1", port)
        assert payload is not None
        assert payload["models"][0]["name"] == "bge-m3"

    def test_a_500_is_still_probed_for_shape(self):
        """An erroring local-embed is still a local-embed: status code alone
        must not decide adoption."""
        body = json.dumps({"status": "degraded", "models": []}).encode()
        with _serving(body, code=500) as port:
            # urlopen raises HTTPError for a 5xx, which is a miss — the service
            # is broken, but it is not a stranger either.  Adoption is decided
            # by the shape of a *healthy* answer.
            assert probe_service("127.0.0.1", port) is None


# ── main(): adopt vs hard error ─────────────────────────────────────────


def _run_main(*, probe_result, monkeypatch, settings=None):
    """Drive main() with the port taken; return the exit code."""
    engine = MagicMock()
    run = MagicMock(return_value=0)
    monkeypatch.setattr(
        "local_embed.config.resolve_engine_settings",
        lambda: settings or {"specs": [], "host": "127.0.0.1", "port": 17347},
    )
    monkeypatch.setattr("local_embed.server.Engine", lambda **kw: engine)
    monkeypatch.setattr("local_embed.server.build_server", lambda e: None)
    monkeypatch.setattr(
        "local_embed.server.bind_port",
        MagicMock(side_effect=RuntimeError("cannot bind 127.0.0.1:17347")),
    )
    monkeypatch.setattr("local_embed.server.probe_service", lambda *a, **k: probe_result)
    monkeypatch.setattr(
        "local_embed.server.bind_free_port",
        lambda host: (MagicMock(name="mcp-sock"), 5555),
    )
    monkeypatch.setattr("local_embed.server_utils.run_plugin_server", run)
    code = main()
    return code, run


class TestMainAdopts:
    def test_a_local_embed_on_the_port_is_adopted_not_failed(self, monkeypatch):
        payload = {"status": "ok", "models": [{"name": "bge-m3"}]}
        code, run = _run_main(probe_result=payload, monkeypatch=monkeypatch)

        assert code == 0  # NOT the old exit-1 load failure
        run.assert_called_once()
        adoption = get_adoption()
        assert adoption is not None
        assert adoption.endpoint == "http://127.0.0.1:17347"
        assert adoption.models == ("bge-m3",)

    def test_mcp_is_served_on_an_os_assigned_port(self, monkeypatch):
        """The fixed port is not ours to serve — MCP takes a free one, and the
        port signal carries it so the host still connects."""
        import local_embed.server as server

        payload = {"status": "ok", "models": []}
        code, run = _run_main(probe_result=payload, monkeypatch=monkeypatch)

        assert code == 0
        assert server._serve_port == 5555
        assert run.call_args.kwargs["sockets"][0] is not None

    def test_a_stranger_on_the_port_still_fails_loudly(self, monkeypatch):
        code, run = _run_main(probe_result=None, monkeypatch=monkeypatch)
        assert code == 1
        run.assert_not_called()
        assert get_adoption() is None


# ── The 2 GB guard ──────────────────────────────────────────────────────


class TestAdoptedLoadsNothing:
    @pytest.mark.asyncio
    async def test_eager_load_is_skipped_while_adopted(self):
        """The shipped local_embed.yaml sets autoload on bge-m3 — without this
        the adopter would pull exactly the model adoption exists to avoid."""
        engine = MagicMock()
        set_engine(engine)
        set_adoption(Adoption(host="127.0.0.1", port=17347, models=("bge-m3",)))
        with patch.object(engine, "load_autoload", new=AsyncMock()) as load:
            await _eager_load_autoload()
        load.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_eager_load_still_runs_when_not_adopted(self):
        engine = MagicMock()
        set_engine(engine)
        with patch.object(engine, "load_autoload", new=AsyncMock()) as load:
            await _eager_load_autoload()
        load.assert_awaited_once()


# ── __check while adopted ───────────────────────────────────────────────


class TestAdoptedCheck:
    @pytest.mark.asyncio
    async def test_reports_the_adopted_services_models(self, monkeypatch):
        set_adoption(Adoption(host="127.0.0.1", port=17347, models=("bge-m3",)))
        monkeypatch.setattr(
            "local_embed.adopt.probe_service",
            lambda *a, **k: {"models": [{"name": "bge-m3", "loaded": True}]},
        )
        payload = json.loads(await check_tool())
        assert payload["adopted"] is True
        assert payload["endpoint"] == "http://127.0.0.1:17347"
        assert payload["models"][0]["name"] == "bge-m3"

    @pytest.mark.asyncio
    async def test_a_dead_service_raises_rather_than_reporting_stale_health(
        self, monkeypatch,
    ):
        """The host turns a raising __check into `unavailable` with the reason
        — the honest report for a service that has gone away."""
        set_adoption(Adoption(host="127.0.0.1", port=17347, models=("bge-m3",)))
        monkeypatch.setattr("local_embed.adopt.probe_service", lambda *a, **k: None)
        with pytest.raises(RuntimeError, match="not answering"):
            await check_tool()


# ── Takeover ────────────────────────────────────────────────────────────


class _Stop(Exception):
    """Ends the watcher, which otherwise serves until the process exits."""


class _FakeServer:
    """Stands in for the ``asyncio.Server`` a takeover starts.

    A real class rather than ``MagicMock``: ``async with`` looks its protocol
    methods up on the TYPE, so an instance-attribute ``__aexit__`` would be
    ignored and MagicMock's own truthy return would silently swallow the
    sentinel that stops the watcher.
    """

    def __init__(self):
        self.served = False
        self.serve_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False  # never swallow — _Stop has to propagate

    async def serve_forever(self):
        self.served = True
        raise _Stop


class TestTakeover:
    @pytest.mark.asyncio
    async def test_takes_the_port_once_it_comes_free(self, monkeypatch):
        engine = MagicMock()
        engine.load_autoload = AsyncMock()
        set_adoption(Adoption(host="127.0.0.1", port=17347, models=("bge-m3",)))

        sock = MagicMock(name="fixed-sock")
        attempts = {"n": 0}

        def _bind(host, port):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("still being served")
            return sock, port

        server = _FakeServer()
        started = AsyncMock(return_value=server)

        monkeypatch.setattr("local_embed.server_utils.bind_port", _bind)
        monkeypatch.setattr("local_embed.adopt.asyncio.start_server", started)

        with pytest.raises(_Stop):
            await adopt.watch_and_take_over(
                "127.0.0.1", 17347, 5555, engine, interval=0.001,
            )

        assert attempts["n"] == 3  # kept retrying while it was still served
        assert server.served  # ...then served the port it took
        assert started.call_args.kwargs["sock"] is sock  # on the port it bound
        engine.load_autoload.assert_awaited_once()  # warm what we now serve
        assert get_adoption() is None  # we own the port now

    @pytest.mark.asyncio
    async def test_keeps_watching_while_the_port_is_served(self, monkeypatch):
        """It must not give up: the whole point is to notice a service that
        dies later."""
        engine = MagicMock()
        engine.load_autoload = AsyncMock()
        adoption = Adoption(host="127.0.0.1", port=17347, models=("bge-m3",))
        set_adoption(adoption)
        calls = {"n": 0}

        def _bind(host, port):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise _Stop  # break out of the forever loop
            raise RuntimeError("still being served")

        monkeypatch.setattr("local_embed.server_utils.bind_port", _bind)

        with pytest.raises(_Stop):
            await adopt.watch_and_take_over(
                "127.0.0.1", 17347, 5555, engine, interval=0.001,
            )
        assert calls["n"] == 3
        engine.load_autoload.assert_not_awaited()
        assert get_adoption() is adoption  # still standing behind the service
