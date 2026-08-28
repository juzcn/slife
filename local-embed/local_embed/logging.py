"""Structured logging for local-embed.

Standalone minimal setup — local-embed must not import slife.  Writes one
human-readable line per event to stderr (the harness / terminal captures
it) with a stable ``key=value`` suffix so failures are greppable, exactly
like slife's logfmt conventions.  The ``silence_noisy_loggers`` helper
drops the httpcore/httpx/uvicorn access noise so a request stream never
drowns the meaningful events.
"""

from __future__ import annotations

import logging

#: Loggers whose DEBUG/INFO chatter would drown the meaningful events.
_NOISY = (
    "httpx",
    "httpcore",
    "uvicorn.access",
    "mcp.server.lowlevel.server",
    "fastmcp",
)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging for the local-embed process (stderr only).

    Safe to call more than once — replaces handlers, keeps level as the
    more verbose of the current and requested.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def silence_noisy_loggers() -> None:
    """Raise the log level of noisy library loggers (idempotent)."""
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
