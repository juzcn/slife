"""Embedding configuration helpers — read, write, validate, report.

Used by the memory_set_embedding / memory_check_embedding /
memory_set_enabled MCP tools to manage the ``memdb.embedding``
section of ``slife.json5`` at runtime.  The embedder itself is owned by
``SemanticManager`` (semantic.py); this module never mutates it.
"""

import logging
from pathlib import Path

from slife.paths import get_config_path
from slife.tools._config_io import read_config, write_config

logger = logging.getLogger(__name__)

_CONFIG_PATH = get_config_path()


def _read_raw() -> dict:
    """Read the full slife.json5 dict, returning {} on failure."""
    return read_config(_CONFIG_PATH)


def _write_raw(raw: dict) -> None:
    """Write the full slife.json5 dict."""
    write_config(_CONFIG_PATH, raw)


# ── Public API ────────────────────────────────────────────────────────


def read_embedding_config() -> dict | None:
    """Return the current *memdb.embedding* section, or None if absent."""
    raw = _read_raw()
    mem = raw.get("memdb", {})
    if not isinstance(mem, dict):
        return None
    emb = mem.get("embedding")
    if not isinstance(emb, dict):
        return None
    return dict(emb)


def write_embedding_config(cfg: dict) -> None:
    """Write (overwrite) the *memdb.embedding* section with *cfg*."""
    raw = _read_raw()
    if not isinstance(raw.get("memdb"), dict):
        raw["memdb"] = {}
    raw["memdb"]["embedding"] = cfg
    _write_raw(raw)
    logger.info("embedding_config_written keys=%s", list(cfg.keys()))


def remove_embedding_config() -> None:
    """Remove the *memdb.embedding* section entirely."""
    raw = _read_raw()
    mem = raw.get("memdb", {})
    if isinstance(mem, dict):
        mem.pop("embedding", None)
    _write_raw(raw)
    logger.info("embedding_config_removed")


def set_embedding_enabled(enabled: bool) -> bool:
    """Set *enabled* flag on the current embedding config.

    Returns True if the config exists and was updated, False if there
    is no config to enable/disable.
    """
    cfg = read_embedding_config()
    if cfg is None:
        logger.info("embedding_enable_skipped reason=no_config")
        return False
    cfg["enabled"] = enabled
    write_embedding_config(cfg)
    logger.info("embedding_enabled=%s", enabled)
    return True


def get_first_provider_api_key() -> str:
    """Return the api_key from the first configured provider, or ''."""
    raw = _read_raw()
    models = raw.get("models", {})
    providers = models.get("providers", {}) if isinstance(models, dict) else {}
    for _pid, pcfg in providers.items():
        if isinstance(pcfg, dict):
            key = pcfg.get("api_key", "")
            if key:
                return key
    return ""


def validate_gguf_path(path: str) -> tuple[bool, str]:
    """Check that a GGUF file path exists and is readable.

    Returns (ok, message).
    """
    p = Path(path).expanduser()
    if not p.exists():
        return False, f"file does not exist: {p}"
    if not p.is_file():
        return False, f"not a file: {p}"
    if p.suffix.lower() not in (".gguf", ".bin", ".ggml"):
        return False, f"file suffix is not .gguf / .bin / .ggml: {p}"
    return True, str(p)


def make_check_report() -> dict:
    """Build a status report dict for memory_check_embedding."""
    cfg = read_embedding_config()

    if cfg is None:
        return {
            "configured": False,
            "backend": "none",
            "model": "",
            "dimension": 1024,
            "available": False,
            "hint": (
                "No embedding configured. Semantic search (hybrid mode) is "
                "unavailable. Keyword search (grep / fts5 / time) still works. "
                "Configure with memory_set_embedding: "
                "GGUF local model: backend=gguf model=bge-m3 gguf_path=... "
                "or Transformer local model: backend=transformer model=BAAI/bge-m3 "
                "or OpenAI API: backend=api model=text-embedding-3-small"
            ),
        }

    backend = (
        "gguf" if cfg.get("gguf_path") else
        "transformer" if cfg.get("backend") == "transformer" else
        "api"
    )
    model = cfg.get("model", "")
    dim = cfg.get("dim", 1024)
    gguf_path = cfg.get("gguf_path")

    # Check actual availability — read the SAME config path the rest of this
    # module uses (embedding_config._CONFIG_PATH), not get_config_path(), so
    # the availability probe and the section read can't disagree and tests can
    # isolate it.
    from slife.plugins.memdb.embeddings import EmbeddingClient, _check_runtime
    client = EmbeddingClient.from_config(config_path=str(_CONFIG_PATH), quiet=True)

    result: dict = {
        "configured": True,
        "backend": backend,
        "model": model,
        "dimension": dim,
        "available": client.available,
    }

    if gguf_path:
        result["gguf_path"] = gguf_path
    if cfg.get("backend"):
        result["cfg_backend"] = cfg["backend"]

    if client.available:
        # All good — add a confirmation hint.
        if backend == "gguf":
            result["hint"] = (
                f"GGUF embedding model ready: {model} (dim={dim}, path={gguf_path})"
            )
        elif backend == "transformer":
            result["hint"] = (
                f"Transformer embedding model ready: {model} (dim={dim})"
            )
        else:
            result["hint"] = (
                f"API embedding ready: {model} (dim={dim})"
            )
    else:
        # Diagnose WHY it's unavailable — file missing vs package missing
        if backend == "gguf":
            file_ok, file_msg = validate_gguf_path(gguf_path) if gguf_path else (False, "no path configured")
            if not file_ok:
                result["gguf_error"] = file_msg
                result["hint"] = (
                    f"GGUF file unavailable: {file_msg}. Download the model file "
                    "or switch to the transformer / API backend with "
                    "memory_set_embedding."
                )
            elif not _check_runtime("gguf"):
                result["hint"] = (
                    f"GGUF file exists ({gguf_path}) but llama-cpp-python is not "
                    "installed. Run: uv pip install llama-cpp-python. "
                    "Until then semantic search (hybrid mode) is unavailable; "
                    "keyword search (grep/fts5/time) still works."
                )
            else:
                result["hint"] = (
                    f"GGUF backend unavailable for an unknown reason. File: {gguf_path}"
                )
        elif backend == "transformer":
            if not _check_runtime("transformer"):
                result["hint"] = (
                    f"Transformer model configured ({model}) but "
                    "sentence-transformers is not installed. Run: "
                    "uv pip install sentence-transformers. Until then semantic "
                    "search (hybrid mode) is unavailable; keyword search "
                    "(grep/fts5/time) still works."
                )
            else:
                result["hint"] = (
                    f"Transformer backend unavailable for an unknown reason. Model: {model}"
                )
        else:  # api
            if not _check_runtime("api"):
                result["hint"] = (
                    "API key configured but the openai package is not installed. "
                    "Run: uv pip install openai. Until then semantic search "
                    "(hybrid mode) is unavailable; keyword search "
                    "(grep/fts5/time) still works."
                )
            else:
                result["hint"] = (
                    "API backend is missing an api_key. Confirm api_key is set in "
                    "models.providers, or switch to a local model: "
                    "memory_set_embedding backend=gguf or backend=transformer"
                )

    return result
