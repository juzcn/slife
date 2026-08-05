"""slife-sharing — plain HTTP server for shared files.

Serves ``GET /share/<token>/<filename>`` → validates the signed token
and streams the local file.  No database — the token carries the file
path and is validated via HMAC.

Designed to be exposed to the internet via ngrok.

Usage::

    python -m slife.plugins.sharing.server
"""

import logging
import mimetypes
import os
from pathlib import Path

from aiohttp import web

from slife.server_utils import bind_free_port, signal_port, setup_server_logging
from slife.sharing.token import lookup_file

_log_path = setup_server_logging("_sharing")
logger = logging.getLogger("slife_sharing")


async def handle_share(request: web.Request) -> web.StreamResponse:
    """Serve a local file by random file ID.

    The file ID is a session-scoped random string looked up in the
    in-memory registry.  Returns 403 if the ID is unknown, 404 if the
    file no longer exists.
    """
    file_id = request.match_info["file_id"]

    file_path_str = lookup_file(file_id)
    if file_path_str is None:
        raise web.HTTPForbidden(text="Unknown share link or session expired")

    file_path = Path(file_path_str)
    if not file_path.is_file():
        raise web.HTTPNotFound(text="File no longer exists")

    # Determine content type
    if not mimetypes.inited:
        mimetypes.init()
    mime_type, _ = mimetypes.guess_type(str(file_path))
    content_type = mime_type or "application/octet-stream"

    logger.debug("share_served path=%s mime=%s", file_path, content_type)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": f'inline; filename="{file_path.name}"',
        },
    )
    await response.prepare(request)

    # Stream in 64 KB chunks
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            await response.write(chunk)

    return response


def main() -> None:
    """Run the sharing server.

    Binds a free port, signals it to the parent process, then starts
    the aiohttp server on a single socket.
    """
    logger.info("sharing_start log=%s pid=%s", _log_path, os.getpid())

    app = web.Application()
    app.router.add_get("/share/{file_id}", handle_share)

    sock, port = bind_free_port()
    signal_port(port)
    logger.info("sharing_ready port=%s", port)

    web.run_app(app, sock=sock, handle_signals=True, print=lambda _: None)


if __name__ == "__main__":
    main()
