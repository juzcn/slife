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
from slife.sharing.token import verify_token

_log_path = setup_server_logging("_sharing")
logger = logging.getLogger("slife_sharing")


async def handle_share(request: web.Request) -> web.StreamResponse:
    """Serve a local file by signed token.

    The token proves the URL was issued by this server.  The file path
    is embedded in the token — no database lookup needed.  Returns 403
    if the token is invalid, 404 if the file no longer exists.
    """
    token = request.match_info["token"]
    _filename = request.match_info["filename"]  # cosmetic — path is in token

    file_path_str = verify_token(token)
    if file_path_str is None:
        raise web.HTTPForbidden(text="Invalid or tampered share link")

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
    app.router.add_get("/share/{token}/{filename}", handle_share)

    sock, port = bind_free_port()
    signal_port(port)
    logger.info("sharing_ready port=%s", port)

    web.run_app(app, sock=sock, handle_signals=True, print=lambda _: None)


if __name__ == "__main__":
    main()
