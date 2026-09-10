"""Console entry point for local-embed.

Entry paths share this module:

- ``local-embed …`` (console script / ``python -m local_embed``): run the
  server as a standalone service — host, port and model config are read from
  ``local_embed.json5`` (the CLI takes no model/endpoint flags).

- ``local-embed set <model_name> [--HF_HUB_CACHE <dir>] [--port <n>]``:
  configure a transformer model in ``local_embed.json5`` (idempotent; the
  model must already be downloaded into the cache).

- ``local-embed set-gguf <model_name> --path <PATH> [--port <n>]``:
  configure a gguf model in ``local_embed.json5`` (idempotent; ``--path``
  must point at an existing ``.gguf`` file).

- ``python -m local_embed.server``: the **plugin spawn target** used by a
  host (slife) — binds a free port, serves MCP on ``/mcp`` and embeddings
  on the same port, and signals the parent.  That path lives in
  :mod:`local_embed.server` and does NOT go through this CLI.
"""

from __future__ import annotations

import argparse
import logging
import sys

from local_embed.config import DEFAULT_PORT, resolve_engine_settings
from local_embed.engine import Engine
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

    sub = p.add_subparsers(dest="command")
    set_p = sub.add_parser(
        "set",
        help="configure a transformer model",
        description=(
            "Add (or update) a transformer model in local_embed.json5 and pin "
            "the HF cache + port.  Idempotent; the model must already be "
            "downloaded into the cache."
        ),
    )
    set_p.add_argument(
        "model",
        metavar="<model_name>",
        help="Hugging Face repo id — the model key and the repo the server loads (e.g. BAAI/bge-m3)",
    )
    set_p.add_argument(
        "--HF_HUB_CACHE",
        metavar="<dir>",
        default=None,
        help="HF cache the model is pre-downloaded into (default: the HF_HUB_CACHE environment variable)",
    )
    set_p.add_argument("--port", type=int, default=DEFAULT_PORT,
                       help=f"port the server binds (default {DEFAULT_PORT})")

    gguf_p = sub.add_parser(
        "set-gguf",
        help="configure a gguf model",
        description=(
            "Add (or update) a gguf model in local_embed.json5 and pin the "
            "port.  Idempotent; --path must point at an existing .gguf file."
        ),
    )
    gguf_p.add_argument(
        "model",
        metavar="<model_name>",
        help="model key used in the config (e.g. bge-m3)",
    )
    gguf_p.add_argument(
        "--path",
        metavar="<PATH>",
        required=True,
        help="path to the local .gguf model file (required)",
    )
    gguf_p.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port the server binds (default {DEFAULT_PORT})")
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "set":
        from local_embed.cmd_set import run_set

        return run_set(args)
    if args.command == "set-gguf":
        from local_embed.cmd_set import run_set_gguf

        return run_set_gguf(args)

    setup_logging(getattr(logging, args.log_level.upper(), logging.INFO))

    # Everything (host/port/backend/model/gguf_path/device) comes from
    # local_embed.json5 — the CLI deliberately takes no model/endpoint flags.
    # A missing file is fine (defaults); a file that exists but won't parse
    # must fail cleanly, not traceback.
    try:
        settings = resolve_engine_settings()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Fix local_embed.json5, or run `local-embed set` to rewrite it.",
              file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: cannot read config: {e}", file=sys.stderr)
        return 2

    engine = Engine(specs=settings["specs"])

    # Validate every model for CONFIG-SHAPE problems only.  No backend is
    # imported at startup: finding out a transformer backend is missing
    # would require importing it (torch — seconds) and stalling the CLI for
    # every configured model.  Local-embed is a lazy server now — with no
    # single 'active' model there is nothing worth pre-importing, and a
    # model that can't load is reported truthfully via /v1/models's
    # ``available`` flag and fails per request (503), exactly like a cloud
    # endpoint.  gguf_path is still checked here because it is config, not
    # an import.
    for spec in settings["specs"]:
        problems: list[str] = []
        if spec.backend == "gguf" and not spec.gguf_path:
            problems.append("no gguf_path (set gguf_path in local_embed.json5)")
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
