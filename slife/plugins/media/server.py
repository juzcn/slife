"""slife-media — non-chat model integration as a standard plugin.

Exposes generation capabilities (image, video, TTS, ASR) from any
configured provider as MCP tools.  The plugin owns everything: its own
``media:`` config section from slife.json5, the provider adapters, and
the HTTP calls.  Artifacts are saved to the working directory — generated
media are work products, never memfiles cabinet files.  The main slife
process is a thin MCP client and never touches provider APIs directly.

Capability models are declared per provider with ``kind`` (image /
video / tts / asr) and ``api`` (wire adapter — ``dashscope-aigc``,
``openai-images``).  Long renders are run in the background with the
harness's universal ``_async: true`` tool mode + ``check_async``.

LLM-visible tools: ``generate_image``, ``generate_video``,
``text_to_speech``, ``transcribe_audio``.

Usage::
    uv run python -m slife.plugins.media.server
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from slife.plugins.media.adapters import (
    MediaAdapter,
    MediaAdapterError,
    create_adapter,
)
from slife.plugins.media.config import (
    MediaConfig,
    MediaConfigError,
    load_media_config,
)
from slife.server_utils import create_plugin_server, run_plugin_server

@asynccontextmanager
async def _media_lifespan(_app):
    """Complete-MCP-lifecycle declaration for the media plugin.

    Config and adapters load lazily on the first tool call (media has no
    startup resource to establish), so the lifespan only yields — but
    declaring it keeps every built-in plugin on the same protocol shape:
    readiness is the MCP ``initialize`` handshake completing, and nothing
    here can stall it.  Any future startup init must stay handshake-fast
    (or go through ``warm_after_handshake``), never block in the lifespan.
    """
    yield


mcp, _log_path, logger = create_plugin_server(
    "slife-media",
    instructions=(
        "slife-media — generation capabilities (image, video, TTS, ASR) "
        "backed by the media: config section. Tools: generate_image, "
        "generate_video, text_to_speech, transcribe_audio. Generation "
        "tools return absolute paths of saved artifacts (transcribe_audio "
        "returns text). Long video renders: pass _async: true and poll "
        "with check_async."
    ),
    lifespan=_media_lifespan,
)

_config: MediaConfig | None = None
_adapters: dict[str, MediaAdapter] = {}


def _ensure_config() -> MediaConfig:
    """Lazy-load the media config; re-read while still empty so adding a
    ``media:`` section works without restarting the plugin."""
    global _config
    if _config is None or _config.is_empty():
        _config = load_media_config()
    return _config


def _get_adapter(provider_id: str, provider_config) -> MediaAdapter:
    adapter = _adapters.get(provider_id)
    if adapter is None:
        adapter = create_adapter(provider_config)
        _adapters[provider_id] = adapter
    return adapter


def _resolve_image_input(image: str) -> str | Path | None:
    """Resolve a reference-image argument for image-conditioned generation.

    Accepts three forms and returns a value the adapters understand:
      - a public ``http(s)://`` URL (e.g. one produced by share_file) —
        passed through as-is;
      - a ``data:<mime>;base64,...`` Data URI — passed through as-is;
      - a local file path — returned as a ``Path`` (adapters convert
        local files to a Data URI themselves).
    """
    if not image:
        return None
    s = image.strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    path = Path(s).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: '{image}'")
    return path.resolve()


def _resolve_output_dir(folder: str = "") -> str:
    """Where a generated artifact should be saved.

    ``folder`` (explicit user request) wins; else the working directory
    (the plugin child inherits the main process's CWD).  Returns an
    absolute path string.
    """
    if folder.strip():
        return str(Path(folder).expanduser().resolve())
    return str(Path.cwd().resolve())


def _error(e: Exception) -> str:
    if isinstance(e, NotImplementedError):
        return f"Error: Capability not supported by this provider: {e}"
    return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="generate_image",
    description=(
        "Generate an image from a text prompt and save it locally; "
        "returns the absolute path of the saved image file."
    ),
)
async def generate_image(
    prompt: str, model: str = "", size: str = "", image: str = "",
    folder: str = "",
) -> str:
    """Generate an image from a text prompt.

    Args:
        prompt: Text description of the image to generate.
        model: Model reference as 'provider/model' or a bare configured
            model name; provider default when omitted.
        size: Output dimensions, e.g. '1024*1024'; provider default when
            omitted.
        image: Reference image for image-conditioned generation — a
            public http(s) URL (e.g. from share_file), a Base64 Data URI,
            or an absolute local path.
        folder: Directory to save the image into (default: working dir).
    """
    try:
        cfg = _ensure_config()
        if cfg.is_empty():
            return (
                "Error: No media provider configured. Add a media: "
                "section to slife.json5."
            )
        pid, pcfg, entry = cfg.resolve_model("image", model or None)
        adapter = _get_adapter(pid, pcfg)
        result = await adapter.generate_image(
            model=entry.model,
            prompt=prompt,
            size=size,
            image=_resolve_image_input(image),
            outputs_dir=_resolve_output_dir(folder),
            extra_params=entry.params,
        )
        logger.info("media_image_generated provider=%s model=%s path=%s",
                    pid, entry.model, result)
        return result
    except (MediaConfigError, MediaAdapterError, FileNotFoundError,
            NotImplementedError) as e:
        return _error(e)
    except Exception as e:
        logger.exception("generate_image_failed")
        return _error(e)


@mcp.tool(
    name="generate_video",
    description=(
        "Generate a video from a text prompt (optionally conditioned on "
        "a reference image) and save it locally. By default saves to the "
        "working directory; pass folder to save to a specific directory. "
        "Returns the absolute path of the saved MP4 file."
    ),
)
async def generate_video(
    prompt: str, model: str = "", image: str = "",
    resolution: str = "", ratio: str = "", duration: int = 0,
    folder: str = "",
) -> str:
    """Generate a video from a text prompt.

    Args:
        prompt: Text description of the video to generate.
        model: Model reference as 'provider/model' or a bare configured
            model name; provider default when omitted.
        image: Absolute local path to a reference image for
            image-to-video / reference-to-video models (uploaded
            automatically).
        resolution: Output resolution, e.g. '720P', '1080P'; model
            default when omitted.
        ratio: Aspect ratio, e.g. '16:9', '9:16', '1:1'; model default
            when omitted.
        duration: Video duration in seconds; model default when omitted.
        folder: Directory to save the video into (default: working dir).
    """
    try:
        cfg = _ensure_config()
        if cfg.is_empty():
            return (
                "Error: No media provider configured. Add a media: "
                "section to slife.json5."
            )
        pid, pcfg, entry = cfg.resolve_model("video", model or None)
        adapter = _get_adapter(pid, pcfg)
        params = dict(entry.params)
        if resolution:
            params["resolution"] = resolution
        if ratio:
            params["ratio"] = ratio
        if duration > 0:
            params["duration"] = duration
        result = await adapter.generate_video(
            model=entry.model,
            prompt=prompt,
            image=_resolve_image_input(image),
            outputs_dir=_resolve_output_dir(folder),
            extra_params=params,
        )
        logger.info("media_video_generated provider=%s model=%s path=%s",
                    pid, entry.model, result)
        return result
    except (MediaConfigError, MediaAdapterError, FileNotFoundError,
            NotImplementedError) as e:
        return _error(e)
    except Exception as e:
        logger.exception("generate_video_failed")
        return _error(e)


@mcp.tool(
    name="text_to_speech",
    description=(
        "Synthesize speech from text and save it locally. By default "
        "saves to the working directory; pass folder to save to a "
        "specific directory. Returns the absolute path of the saved "
        "audio file."
    ),
)
async def text_to_speech(
    text: str, model: str = "", voice: str = "", folder: str = "",
) -> str:
    """Synthesize speech from text.

    Args:
        text: Text to synthesize into speech.
        model: Model reference as 'provider/model' or a bare configured
            model name. Uses the configured tts default when omitted.
        voice: Voice identifier (e.g. 'longxiaochun'). Uses the model's
            configured default voice when omitted.
        folder: Directory to save the audio into (default: the working
            directory).
    """
    try:
        cfg = _ensure_config()
        if cfg.is_empty():
            return (
                "Error: No media provider configured. Add a media: "
                "section to slife.json5."
            )
        pid, pcfg, entry = cfg.resolve_model("tts", model or None)
        adapter = _get_adapter(pid, pcfg)
        result = await adapter.text_to_speech(
            model=entry.model,
            text=text,
            voice=voice or entry.voice,
            outputs_dir=_resolve_output_dir(folder),
            extra_params=entry.params,
        )
        logger.info("media_tts_synthesized provider=%s model=%s path=%s",
                    pid, entry.model, result)
        return result
    except (MediaConfigError, MediaAdapterError, FileNotFoundError,
            NotImplementedError) as e:
        return _error(e)
    except Exception as e:
        logger.exception("text_to_speech_failed")
        return _error(e)


@mcp.tool(
    name="transcribe_audio",
    description=(
        "Transcribe speech from an audio file. Returns the transcript "
        "text."
    ),
)
async def transcribe_audio(path: str, model: str = "") -> str:
    """Transcribe speech from an audio file.

    Args:
        path: Absolute local path to the audio file to transcribe.
        model: Model reference as 'provider/model' or a bare configured
            model name. Uses the configured asr default when omitted.
    """
    try:
        cfg = _ensure_config()
        if cfg.is_empty():
            return (
                "Error: No media provider configured. Add a media: "
                "section to slife.json5."
            )
        pid, pcfg, entry = cfg.resolve_model("asr", model or None)
        adapter = _get_adapter(pid, pcfg)
        audio_path = None
        if path:
            p = Path(path).expanduser()
            if p.is_file():
                audio_path = p.resolve()
            else:
                return f"Error: File not found: '{path}'"
        if audio_path is None:
            return "Error: No audio file path provided."
        result = await adapter.transcribe_audio(
            model=entry.model,
            audio_path=audio_path,
            extra_params=entry.params,
        )
        logger.info("media_audio_transcribed provider=%s model=%s chars=%d",
                    pid, entry.model, len(result))
        return result
    except (MediaConfigError, MediaAdapterError, FileNotFoundError,
            NotImplementedError) as e:
        return _error(e)
    except Exception as e:
        logger.exception("transcribe_audio_failed")
        return _error(e)


@mcp.tool(
    name="__check",
    description=(
        "Media (image/video/TTS/ASR) live config facts: configured, each "
        "provider's api/capabilities/api_key presence. Internal — probed by "
        "the harness's system_health, never exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return raw media facts for the harness's health check.

    Media is configuration-only: its live technical state is the loaded
    provider config (capabilities + key presence).  Internal (``__`` prefix):
    probed by the harness's ``system_health``, which interprets the facts
    into health entries.  Facts only — no levels, no remediation hints.
    """
    result: dict = {"configured": False, "error": "", "providers": []}
    try:
        cfg = _ensure_config()
        if cfg.is_empty():
            return json.dumps(result, ensure_ascii=False, indent=2)
        result["configured"] = True
        for pid in sorted(cfg.providers):
            p = cfg.providers[pid]
            result["providers"].append({
                "id": pid,
                "api": p.api,
                "kinds": sorted({m.kind for m in p.models}),
                "has_api_key": bool(p.api_key),
            })
    except Exception as e:
        logger.warning("media_check_failed err=%s", e)
        result["error"] = str(e)
    return json.dumps(result, ensure_ascii=False, indent=2)


def main() -> None:
    run_plugin_server(mcp)


if __name__ == "__main__":
    main()
