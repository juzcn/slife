"""slife-sharefile — public file sharing plugin.

A self-contained, replaceable Streamable HTTP plugin (same contract as
memdb / media): the harness spawns ``server.py``, connects via MCP, and
registers the sharefile tools.  The plugin owns the in-process
token registry, the ngrok tunnel, and serves file bytes on the same port
via a custom HTTP route (``GET /share/{file_id}``).

Its sole LLM-visible tool is ``share_file`` — it publishes a local file
as a public HTTPS URL that multimodal LLM APIs can fetch directly
(instead of inline base64).  Publishing is always the LLM's explicit
choice; the file cabinet (memfiles) never auto-publishes.

LLM-visible tools: ``share_file``.
Internal tools (``__`` prefix, never LLM-visible): ``__check``,
``__register_file``.

Usage::
    uv run python -m slife.plugins.sharefile.server
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from urllib.parse import quote

from slife.plugins.sharefile.tunnel import NgrokTunnel
from slife.server_utils import (
    bind_free_port,
    create_plugin_server,
    run_plugin_server,
    shutdown_server_logging,
)

# ── Own port — bound by main() so the tunnel can forward to it ────────
_PLUGIN_PORT: int = 0

# The plugin owns its tunnel instance (single consumer of NgrokTunnel).
_tunnel = NgrokTunnel()


# ═══════════════════════════════════════════════════════════════════════
# Tunnel lifecycle — eager start on plugin startup, graceful failure
# ═══════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def _sharefile_lifespan(_app):
    """Eagerly start the ngrok tunnel on plugin startup.  A failed start is
    tolerated — the tunnel retries with its own bounded backoff, and
    ``share_file`` falls back to an on-demand start.  On shutdown the tunnel
    is disconnected.

    The tunnel is started on a background task and never awaited here, so the
    app serves immediately and the plugin loads fast (the port signal fires as
    soon as the app is ready; the tunnel keeps coming up in the background).
    Blocking app startup on the ~2s ngrok session creation would only delay
    loading — under the plugin loading contract the signal is deferred until
    the app is ready either way, so this is a startup-speed optimization.
    """
    if _PLUGIN_PORT:
        async def _eager_tunnel() -> None:
            try:
                from slife.threads import run_daemon
                await run_daemon(_tunnel.start, _PLUGIN_PORT, name="ngrok-tunnel")
            except Exception as e:
                logger.warning("sharefile_tunnel_eager_failed err=%s", e)

        asyncio.create_task(_eager_tunnel())
        # Background monitor — one-shot retry if the eager start failed.
        _tunnel.start_monitor(_PLUGIN_PORT)
    try:
        yield
    finally:
        _tunnel.stop_monitor()
        _tunnel.stop()


mcp, _log_path, logger = create_plugin_server(
    "slife-sharefile",
    instructions=(
        "slife-sharefile — public file sharing.  share_file(path) publishes "
        "a local file as a public HTTPS URL that multimodal LLMs can fetch "
        "directly.  Publishing is always the LLM's explicit choice — this "
        "plugin never auto-publishes; the file cabinet (memfiles) returns "
        "only local paths."
    ),
    lifespan=_sharefile_lifespan,
)


# ═══════════════════════════════════════════════════════════════════════
# Token registry — in-process (server + tunnel live in one process)
# ═══════════════════════════════════════════════════════════════════════

_registry: dict[str, str] = {}       # token → absolute local path
_path_to_token: dict[str, str] = {}  # absolute path → token (dedup)


def _register_file(file_path: str) -> str:
    """Return a short hex token for *file_path*, reusing an existing one.

    30 chars (``secrets.token_hex(15)``) — deliberately below 32 to avoid
    the generic ``[A-Za-z0-9]{32,}`` secret-sanitization pattern in
    ``logfmt.py``.  Hex (not base64url) so no underscores break the
    Textual/Rich markdown URL detection.
    """
    existing = _path_to_token.get(file_path)
    if existing:
        return existing
    tok = secrets.token_hex(15)
    _registry[tok] = file_path
    _path_to_token[file_path] = tok
    logger.debug("register_file token=%s path=%s", tok, file_path)
    return tok


def _lookup_file(token: str) -> str | None:
    """Return the local path for *token*, or ``None`` if unknown."""
    return _registry.get(token)


def _reset_registry() -> None:
    """Clear all registered tokens (used by tests)."""
    _registry.clear()
    _path_to_token.clear()


def _content_disposition(filename: str) -> str:
    """RFC 5987 content-disposition for (possibly) non-ASCII filenames.

    HTTP header values must be Latin-1.  For a non-Latin-1 filename we
    emit an ASCII fallback in ``filename=`` plus the real name
    percent-encoded in ``filename*=UTF-8''...`` — otherwise Starlette
    raises ``UnicodeEncodeError`` while writing the header (500).
    """
    try:
        filename.encode("latin-1")
    except UnicodeEncodeError:
        ascii_fallback = re.sub(r'[^ -~]', "_", filename)
        ascii_fallback = ascii_fallback.replace('"', "_").replace("\\", "_")
        ascii_fallback = ascii_fallback[:80] or "file"
        return (
            f'inline; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )
    safe = filename.replace('"', "_").replace("\\", "_")
    return f'inline; filename="{safe}"'


async def _ensure_tunnel() -> bool:
    """Start the tunnel if it isn't active (lazy on-demand fallback)."""
    if _tunnel.is_active:
        return True
    if not _PLUGIN_PORT:
        return False
    try:
        from slife.threads import run_daemon
        await run_daemon(_tunnel.start, _PLUGIN_PORT, name="ngrok-tunnel-on-demand")
        return _tunnel.is_active
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# HTTP route — serve file bytes on the same port as /mcp
# ═══════════════════════════════════════════════════════════════════════


@mcp.custom_route("/share/{file_id}", methods=["GET"])
async def handle_share(request: Request) -> Response:
    """Serve a registered local file by its share token.

    Returns 403 for an unknown/expired token, 404 if the file no longer
    exists.  Streams in 64 KB chunks.
    """
    file_id = request.path_params["file_id"]

    file_path_str = _lookup_file(file_id)
    if file_path_str is None:
        return Response("Unknown share link or session expired", status_code=403)

    file_path = Path(file_path_str)
    if not file_path.is_file():
        return Response("File no longer exists", status_code=404)

    if not mimetypes.inited:
        mimetypes.init()
    mime_type, _ = mimetypes.guess_type(str(file_path))
    content_type = mime_type or "application/octet-stream"
    file_size = file_path.stat().st_size

    logger.info("share_served path=%s mime=%s size=%s", file_path, content_type, file_size)

    def _iter_chunks():
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter_chunks(),
        status_code=200,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(file_size),
            "Content-Disposition": _content_disposition(file_path.name),
            # Suppress the ngrok free-tier interstitial browser-warning page.
            "ngrok-skip-browser-warning": "true",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# Internal tools (``__`` prefix — callable by the main process, never the LLM)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="__check", description="File-sharing tunnel live state as JSON facts. Internal — probed by the harness's system_health.")
async def __check() -> str:
    """Return ``{active, state, url, reason}`` live tunnel facts.

    ``state`` distinguishes the harness-relevant cases: ``active`` (a public
    URL is live), ``starting`` (an eager start attempt is still in flight —
    the harness waits for it to conclude), ``failed`` (terminal — safe to
    report the tunnel down; ``reason`` carries the last failure message),
    ``idle`` (no attempt made, e.g. subagent reusing the main agent's
    tunnel).  Facts only — the harness composes levels/hints.
    """
    st = _tunnel.status()
    return json.dumps(
        {
            "active": st["state"] == "active",
            "state": st["state"],
            "url": st.get("url", ""),
            "reason": st.get("reason", ""),
        },
        ensure_ascii=False,
    )


@mcp.tool(name="__register_file", description="Register a file and return its share URL.")
async def __register_file(path: str) -> str:
    """Register *path* and return ``{file_id, url}`` for the harness."""
    await _ensure_tunnel()
    file_id = _register_file(str(Path(path).resolve()))
    url = _tunnel.share_url_for(file_id) or ""
    return json.dumps({"file_id": file_id, "url": url}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools (registered by the harness)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="share_file",
    description=(
        "Share a local file as a public HTTPS URL for multimodal LLMs to "
        "fetch directly.  Requires the file-sharing tunnel to be active."
    ),
)
async def share_file(path: str) -> str:
    """Share a local file as a public HTTPS URL.

    Args:
        path: Absolute path to the local file to share.
    """
    p = Path(path)
    if not p.exists():
        return f"Error: file not found — {path}"
    if not p.is_file():
        return f"Error: not a file — {path}"

    if not await _ensure_tunnel():
        return (
            "Error: file sharing service is not available. "
            "Run system_health to check service status."
        )

    file_id = _register_file(str(p.resolve()))
    url = _tunnel.share_url_for(file_id)
    if url is None:
        return (
            "Error: file sharing service became unavailable. "
            "Please retry or run system_health for details."
        )

    return (
        f"Public URL for {p.name}:\n"
        f"{url}\n\n"
        f"Use this URL in multimodal API calls to let the LLM fetch "
        f"the file directly."
    )


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Run the sharefile plugin on Streamable HTTP.

    Binds its own port first (the plugin owns the ngrok tunnel and must
    know its port to forward to), then serves MCP on ``/mcp`` and file
    bytes on ``/share/{file_id}`` — same port.  ``run_plugin_server`` emits
    the port signal to the parent once the app is ready (the plugin loading
    contract) — this plugin does not signal early.
    """
    import os

    global _PLUGIN_PORT
    sock, port = bind_free_port()
    _PLUGIN_PORT = port
    os.environ["SLIFE_SHAREFILE_PORT"] = str(port)

    logger.info("sharefile_start log=%s port=%s", _log_path, port)

    try:
        run_plugin_server(mcp, sockets=[sock])
    finally:
        logger.info("sharefile_shutdown port=%s", port)
        shutdown_server_logging()


if __name__ == "__main__":
    main()
