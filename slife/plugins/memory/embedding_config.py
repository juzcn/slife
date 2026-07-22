"""Embedding configuration helpers — read, write, validate, reload.

Used by the memory_set_embedding / memory_check_embedding /
memory_remove_embedding MCP tools to manage the ``memory.embedding``
section of ``slife.json5`` at runtime.
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
    """Return the current *memory.embedding* section, or None if absent."""
    raw = _read_raw()
    mem = raw.get("memory", {})
    if not isinstance(mem, dict):
        return None
    emb = mem.get("embedding")
    if not isinstance(emb, dict):
        return None
    return dict(emb)


def write_embedding_config(cfg: dict) -> None:
    """Write (overwrite) the *memory.embedding* section with *cfg*."""
    raw = _read_raw()
    if not isinstance(raw.get("memory"), dict):
        raw["memory"] = {}
    raw["memory"]["embedding"] = cfg
    _write_raw(raw)
    logger.info("embedding_config_written keys=%s", list(cfg.keys()))


def remove_embedding_config() -> None:
    """Remove the *memory.embedding* section entirely."""
    raw = _read_raw()
    mem = raw.get("memory", {})
    if isinstance(mem, dict):
        mem.pop("embedding", None)
    _write_raw(raw)
    logger.info("embedding_config_removed")


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
        return False, f"文件不存在: {p}"
    if not p.is_file():
        return False, f"不是文件: {p}"
    if not p.suffix.lower() in (".gguf", ".bin", ".ggml"):
        return False, f"文件后缀不是 .gguf / .bin / .ggml: {p}"
    return True, str(p)


# ── Embedder reload ───────────────────────────────────────────────────

# Imported lazily to avoid circular imports at module level.
_embedder_module = None


def _get_embedder_module():
    """Lazy-import the server module to access the global _embedder."""
    global _embedder_module
    if _embedder_module is None:
        import slife.plugins.memory.server as _embedder_module
    return _embedder_module


async def reload_embedder() -> dict:
    """Recreate the global _embedder from the current config.

    Returns a status dict suitable for returning from a tool.
    """
    from slife.plugins.memory.embeddings import EmbeddingClient  # local import

    mod = _get_embedder_module()
    mod._embedder = EmbeddingClient.from_config()

    e = mod._embedder
    if e.available:
        logger.info(
            "embedder_reloaded backend=%s model=%s dim=%d",
            e.backend, e._model, e.dimension,
        )
        return {
            "status": "ok",
            "backend": e.backend,
            "model": e._model,
            "dimension": e.dimension,
            "available": True,
            "message": f"已启用 {e.backend} 后端: {e._model} (dim={e.dimension})",
        }
    else:
        logger.info("embedder_reloaded — embeddings disabled")
        return {
            "status": "ok",
            "backend": "none",
            "model": "",
            "dimension": e.dimension,
            "available": False,
            "message": (
                "Embedding 未配置 — 语义搜索不可用，关键词搜索 (FTS5) 仍可正常工作。"
                "使用 memory_set_embedding 配置 GGUF 本地模型或 OpenAI API。"
            ),
        }


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
                "未配置 embedding。语义搜索 (hybrid 模式) 不可用。"
                "关键词搜索 (grep / fts5 / time) 仍可正常工作。"
                "使用 memory_set_embedding 配置: "
                "GGUF 本地模型: backend=gguf model=bge-m3 gguf_path=... "
                "或 OpenAI API: backend=api model=text-embedding-3-small"
            ),
        }

    backend = "gguf" if cfg.get("gguf_path") else "api"
    model = cfg.get("model", "")
    dim = cfg.get("dim", 1024)
    gguf_path = cfg.get("gguf_path")

    # Check actual availability
    from slife.plugins.memory.embeddings import EmbeddingClient, _check_runtime
    client = EmbeddingClient.from_config(quiet=True)

    result: dict = {
        "configured": True,
        "backend": backend,
        "model": model,
        "dimension": dim,
        "available": client.available,
    }

    if gguf_path:
        result["gguf_path"] = gguf_path

    if client.available:
        # All good — add a confirmation hint.
        if backend == "gguf":
            result["hint"] = (
                f"GGUF 嵌入模型已就绪: {model} (dim={dim}, path={gguf_path})"
            )
        else:
            result["hint"] = (
                f"API 嵌入已就绪: {model} (dim={dim})"
            )
    else:
        # Diagnose WHY it's unavailable — file missing vs package missing
        if backend == "gguf":
            file_ok, file_msg = validate_gguf_path(gguf_path) if gguf_path else (False, "no path configured")
            if not file_ok:
                result["gguf_error"] = file_msg
                result["hint"] = (
                    f"GGUF 文件不可用: {file_msg}。"
                    "下载模型文件或使用 memory_set_embedding 切换到 API 后端。"
                )
            elif not _check_runtime("gguf"):
                result["hint"] = (
                    f"GGUF 文件存在 ({gguf_path})，但 llama-cpp-python 未安装。"
                    "运行: pip install llama-cpp-python。"
                    "在此之前语义搜索 (hybrid 模式) 不可用；"
                    "关键词搜索 (grep/fts5/time) 仍可正常工作。"
                )
            else:
                result["hint"] = (
                    f"GGUF 后端不可用，原因未知。文件: {gguf_path}"
                )
        else:  # api
            if not _check_runtime("api"):
                result["hint"] = (
                    "API key 已配置，但 openai 包未安装。"
                    "运行: pip install openai。"
                    "在此之前语义搜索 (hybrid 模式) 不可用；"
                    "关键词搜索 (grep/fts5/time) 仍可正常工作。"
                )
            else:
                result["hint"] = (
                    "API backend 缺少 api_key。确认 models.providers 中配置了 api_key，"
                    "或改用 GGUF 本地模型: memory_set_embedding backend=gguf"
                )

    return result
