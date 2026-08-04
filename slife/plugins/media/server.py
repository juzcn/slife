"""slife-media server — plain HTTP server for serving image BLOBs.

Serves ``GET /media/<image_id>`` → reads ``diary_images`` BLOB from the
same SQLite database used by the memory plugin.  Designed to be exposed
to the internet via ngrok so LLM vision APIs can fetch images by URL
instead of receiving inline base64 data URIs.

Usage::

    python -m slife.plugins.media.server
"""

import logging
import os

from aiohttp import web

from slife.paths import get_db_path
from slife.server_utils import bind_free_port, signal_port, setup_server_logging

_log_path = setup_server_logging("_media")
logger = logging.getLogger("slife_media")


async def handle_media(request: web.Request) -> web.Response:
    """Serve a single image BLOB by ID.

    Returns the raw image bytes with the correct ``Content-Type`` header,
    or 404 if the image_id is not found in ``diary_images``.
    """
    image_id = request.match_info["image_id"]
    db = request.app["db"]

    async with db.execute(
        "SELECT data, mime_type FROM diary_images WHERE image_id = ?",
        (image_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise web.HTTPNotFound(text=f"Image {image_id} not found")

    data, mime_type = row[0], row[1]
    logger.debug("media_served id=%s mime=%s size=%d", image_id, mime_type, len(data))
    return web.Response(body=data, content_type=mime_type)


async def on_startup(app: web.Application) -> None:
    """Open a read-only SQLite connection on server startup."""
    import aiosqlite

    agent_id = os.environ.get("SLIFE_AGENT_ID", "slife")
    db_path = get_db_path(agent_id)
    logger.info("media_db_open path=%s", db_path)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    # Ensure the table exists — plugins start in parallel.
    await db.execute(
        """CREATE TABLE IF NOT EXISTS diary_images (
               image_id  TEXT PRIMARY KEY,
               data      BLOB NOT NULL,
               mime_type TEXT NOT NULL DEFAULT 'image/png',
               file_name TEXT NOT NULL DEFAULT '',
               file_size INTEGER NOT NULL DEFAULT 0
           )"""
    )
    await db.commit()
    app["db"] = db


async def on_cleanup(app: web.Application) -> None:
    """Close the SQLite connection on server shutdown."""
    db = app.get("db")
    if db is not None:
        await db.close()
        logger.info("media_db_closed")


def main() -> None:
    """Run the media HTTP server.

    Binds a free port, signals it to the parent process, then starts
    the aiohttp server on a single socket.  Matching the standard
    plugin port-discovery protocol.
    """
    logger.info("media_start log=%s pid=%s", _log_path, os.getpid())

    app = web.Application()
    app.router.add_get("/media/{image_id}", handle_media)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    sock, port = bind_free_port()
    signal_port(port)
    logger.info("media_ready port=%s", port)

    web.run_app(app, sock=sock, handle_signals=True, print=lambda _: None)


if __name__ == "__main__":
    main()
