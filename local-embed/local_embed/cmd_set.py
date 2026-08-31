"""``local-embed set`` / ``set-gguf`` — configure a model and make it active.

Pure, testable core + CLI orchestration for the two config subcommands:

- ``set`` configures a **transformer** model (an HF repo id).  The HF cache
  directory resolves as ``--HF_HUB_CACHE`` > ``HF_HUB_CACHE`` env var >
  hard error, and the model must **already be downloaded** into it.
- ``set-gguf`` configures a **gguf** model.  ``--path`` is required and the
  ``.gguf`` file it points at must already exist.

Both share the same logic: the model entry is upserted (existing models /
``env:`` keys are preserved, never deleted), the model becomes
``active_model``, ``port`` is pinned, and the config is written atomically
in the repo's canonical JSON5 style (:func:`local_embed.config.write_config`).
Idempotent — applying the same args twice yields the same config.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from local_embed.config import load_config, write_config

ENV_CACHE_KEY = "HF_HUB_CACHE"
ENV_OFFLINE_KEY = "HF_HUB_OFFLINE"

# llama-cpp-python ships only an sdist on PyPI — the standard install
# compiles from source (C compiler + CMake).  Windows has no default C
# toolchain (no MSVC), so it is the one platform that falls back to the
# upstream prebuilt CPU wheel (abetlen index) — the workaround, not the
# standard.
LLAMA_CPP_INDEXES = {
    "cpu": "https://abetlen.github.io/llama-cpp-python/whl/cpu",
}


def backend_install_hint(
    backend: str, platform: "str | None" = None, python: "str | None" = None,
) -> str:
    """The install command for a missing backend, INTO the venv that runs this.

    local-embed normally runs inside the slife tool venv (it is a slife
    dependency), so the backend must be added to *that* interpreter — ``uv
    pip install --python <this venv's python>``.  ``uv tool install
    'local-embed[gguf]'`` would rebuild a separate standalone tool instead of
    fixing the running venv, so it is never the right hint here.

    ``gguf`` = llama-cpp-python: no PyPI wheel, so the standard build
    compiles from source (C compiler + CMake) everywhere except **Windows**,
    which has no default C toolchain — there the abetlen CPU wheel index is
    added (the one workaround).  (CUDA / Metal pass ``CMAKE_ARGS`` — see the
    README install matrix.)
    """
    platform = platform or sys.platform
    pyp = python or sys.executable
    package = "llama-cpp-python==0.3.34" if backend == "gguf" else "sentence-transformers"
    if backend == "gguf" and platform == "win32":
        return (
            f"uv pip install --python {pyp} "
            f"--extra-index-url {LLAMA_CPP_INDEXES['cpu']} {package}"
        )
    return f"uv pip install --python {pyp} {package}"


def resolve_cache(raw: "str | None", environ: "dict[str, str] | None" = None) -> str:
    """Return the effective HF cache dir: flag > env var > ``ValueError``.

    The cache pinned by the command must be the one the model's weights are
    in — resolution mirrors how the server would look the repo up.
    """
    if raw:
        return raw
    value = (environ if environ is not None else os.environ).get(ENV_CACHE_KEY)
    if not value:
        raise ValueError(
            f"{ENV_CACHE_KEY} is not set — pass --{ENV_CACHE_KEY} <dir> or "
            f"set the {ENV_CACHE_KEY} environment variable"
        )
    return value


def model_in_cache(cache: str, repo: str) -> bool:
    """Whether the HF cache already holds ``repo``'s weights.

    Accepts both layouts ``huggingface_hub`` writes: the snapshot layout
    (``models--{org}--{repo}/``) and the legacy ``{org}/{repo}/`` directory.
    """
    root = Path(cache)
    return (root / f"models--{repo.replace('/', '--')}").is_dir() or (root / repo).is_dir()


def _upsert_model(cfg: dict, model: str, entry: dict, port: int) -> dict:
    """Insert or replace ``model``, make it active, pin ``port``.

    Idempotent: re-applying the same args to the result yields the same dict.
    """
    models = dict(cfg.get("models") or {})
    models[model] = entry
    return {**cfg, "models": models, "active_model": model, "port": int(port)}


def set_transformer_model(cfg: dict, model: str, cache: str, port: int) -> dict:
    """Upsert ``model`` as a transformer model, pin the pre-downloaded cache.

    The server is intended to run **offline** — the model is pre-downloaded
    into ``cache`` and ``set`` requires it to already be there — so
    ``HF_HUB_OFFLINE=1`` is written alongside the cache (the server resolves
    the repo against this cache and never hits the network).
    """
    env = dict(cfg.get("env") or {})
    env[ENV_CACHE_KEY] = cache
    env[ENV_OFFLINE_KEY] = "1"
    out = _upsert_model(cfg, model, {"backend": "transformer", "model": model}, port)
    out["env"] = env  # existing env keys preserved; cache + offline (re)written
    return out


def set_gguf_model(cfg: dict, model: str, gguf_path: str, port: int) -> dict:
    """Upsert ``model`` as a gguf model pointing at a local ``.gguf`` file."""
    return _upsert_model(cfg, model, {"backend": "gguf", "gguf_path": gguf_path}, port)


def _write_model(mutate, rows: "list[tuple[str, object]]") -> int:
    """Load, mutate and atomically write the config; shared result output."""
    try:
        cfg = load_config()
        path = write_config(mutate(cfg))
    except ValueError as e:  # config exists but won't parse
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: cannot write config: {e}", file=sys.stderr)
        return 2

    print("local-embed configured:")
    for label, value in rows:
        print(f"  {label!s:<16}: {value}")
    print(f"  {'config':<16}: {path}")
    print("Restart 'local-embed' to apply.")
    return 0


def run_set(args) -> int:
    """Orchestrate ``local-embed set`` (transformer) — returns the exit code."""
    try:
        cache = resolve_cache(getattr(args, ENV_CACHE_KEY, None))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if not model_in_cache(cache, args.model):
        print(
            f"Error: model '{args.model}' not found in HF cache '{cache}' — "
            f"download it first, e.g. `hf download {args.model}`.",
            file=sys.stderr,
        )
        return 2

    return _write_model(
        lambda cfg: set_transformer_model(cfg, args.model, cache, args.port),
        rows=[
            ("transformer model", f"{args.model}  (active)"),
            ("HF cache", cache),
            ("port", args.port),
        ],
    )


def run_set_gguf(args) -> int:
    """Orchestrate ``local-embed set-gguf`` (gguf) — returns the exit code."""
    if not Path(args.path).is_file():
        print(f"Error: gguf file not found: {args.path}", file=sys.stderr)
        return 2

    return _write_model(
        lambda cfg: set_gguf_model(cfg, args.model, args.path, args.port),
        rows=[
            ("gguf model", f"{args.model}  (active)"),
            ("gguf file", args.path),
            ("port", args.port),
        ],
    )