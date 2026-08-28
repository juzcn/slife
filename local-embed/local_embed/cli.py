"""Console entry point for local-embed.

Two entry paths share this module:

- ``local-embed …`` (console script / ``python -m local_embed``): run the
  server as a standalone service — host, port and model config are read from
  ``local_embed.json5`` (the CLI takes no model/endpoint flags).

- ``python -m local_embed.server``: the **plugin spawn target** used by a
  host (slife) — binds a free port, serves MCP on ``/mcp`` and embeddings
  on the same port, and signals the parent.  That path lives in
  :mod:`local_embed.server` and does NOT go through this CLI.
"""

from __future__ import annotations

import argparse
import logging
import sys

from local_embed.config import resolve_engine_settings
from local_embed.engine import Engine, resolve_backend_runtime
from local_embed.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="local-embed",
        description=(
            "Standalone local embedding server — expose a GGUF (llama-cpp) or "
            "HF transformer model as an OpenAI-compatible /v1/embeddings service. "
            "Host, port and model config come from local_embed.json5."
        ),
    )
    p.add_argument("--log-level", default="INFO", help="logging level (default INFO)")
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(getattr(logging, args.log_level.upper(), logging.INFO))

    # Everything (host/port/backend/model/gguf_path/device) comes from
    # local_embed.json5 — the CLI deliberately takes no model/endpoint flags.
    settings = resolve_engine_settings()
    engine = Engine(specs=settings["specs"], active=settings["active"])

    # Validate the models can actually run.  `resolve_backend_runtime` (not
    # `check_backend_runtime`, which never imports) so the answer reflects
    # reality.  Only the ACTIVE model blocks startup — the server can serve
    # it without the others; a non-active model with a missing backend or
    # gguf_path is a warning (it fails at load time if requested).
    active_name = engine.active_model
    for spec in settings["specs"]:
        problems: list[str] = []
        if spec.backend == "gguf" and not spec.gguf_path:
            problems.append("no gguf_path (set gguf_path in local_embed.json5)")
        if not resolve_backend_runtime(spec.backend):
            problems.append(
                f"{spec.backend} backend not installed "
                f"(uv pip install 'local-embed[{spec.backend}]')"
            )
        if spec.name == active_name and problems:
            print(
                f"Error: active model '{spec.name}' cannot start: {'; '.join(problems)}",
                file=sys.stderr,
            )
            return 2
        if problems:
            print(
                f"Warning: model '{spec.name}': {'; '.join(problems)} "
                "— it will fail if requested.",
                file=sys.stderr,
            )

    from local_embed.server import serve_standalone

    return serve_standalone(engine, host=settings["host"], port=settings["port"])


if __name__ == "__main__":
    sys.exit(main())
