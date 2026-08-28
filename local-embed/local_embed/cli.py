"""Console entry point for local-embed.

Two entry paths share this module:

- ``local-embed …`` (console script / ``python -m local_embed``): run the
  server on an explicit host:port as a standalone service.

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
from local_embed.engine import Engine, check_backend_runtime
from local_embed.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="local-embed",
        description=(
            "Standalone local embedding server — expose a GGUF (llama-cpp) or "
            "HF transformer model as an OpenAI-compatible /v1/embeddings service."
        ),
    )
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    p.add_argument(
        "--backend",
        choices=("gguf", "transformer"),
        default="gguf",
        help="model backend (default gguf)",
    )
    p.add_argument("--model", default="bge-m3", help="model name/id (for metadata and dim guessing)")
    p.add_argument("--gguf-path", default="", help="path to a GGUF file (backend=gguf)")
    p.add_argument("--device", default="", help="device for transformer backend: cpu | cuda | '' (auto)")
    p.add_argument("--log-level", default="INFO", help="logging level (default INFO)")
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(getattr(logging, args.log_level.upper(), logging.INFO))

    # CLI flags are one-model overrides on top of local_embed.json5.  A
    # config file with a ``models`` map wins; explicit flags on a
    # single-model config override its keys.
    settings = resolve_engine_settings(
        overrides={
            "backend": args.backend,
            "model": args.model,
            "gguf_path": args.gguf_path,
            "device": args.device,
        }
    )
    engine = Engine(specs=settings["specs"], active=settings["active"])

    # Validate the gguf backend actually has a model to load.
    for spec in settings["specs"]:
        if spec.backend == "gguf" and not spec.gguf_path:
            print(
                f"Error: no gguf_path for model '{spec.name}'. "
                "Set gguf_path in local_embed.json5 or pass --gguf-path.",
                file=sys.stderr,
            )
            return 2
        if not check_backend_runtime(spec.backend):
            print(
                f"Error: {spec.backend} backend for model '{spec.name}' is not installed. "
                f"Install with: uv pip install 'local-embed[{spec.backend}]'",
                file=sys.stderr,
            )
            return 2

    from local_embed.server import serve_standalone

    return serve_standalone(engine, host=args.host, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
