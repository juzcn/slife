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


def _resolve_input_path(path_str: str) -> Path | None:
    if not path_str:
        return None
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: '{path_str}'")
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
        "Generate an image from a text prompt and save it locally. "
        "By default saves to the working directory; pass folder to save "
        "to a specific directory. Returns the absolute path of the "
        "saved image file."
    ),
)
async def generate_image(
    prompt: str, model: str = "", size: str = "", image: str = "",
    folder: str = "",
) -> str:
    """Generate an image from a text prompt.

    Args:
        prompt: Text description of the image to generate.
        model: Model reference as 'provider/model' (e.g.
            'bailian_personal/wan2.7-image') or a bare configured model
            name. Uses the configured image default when omitted.
        size: Output dimensions, e.g. '1024*1024'. Provider-specific;
            uses the model default when omitted.
        image: Absolute local path to a reference image for
            image-conditioned generation. Uploaded to the provider
            automatically.
        folder: Directory to save the image into (default: the working
            directory).
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
            image_path=_resolve_input_path(image),
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
        "Returns the absolute path of the saved MP4 file. Renders take "
        "minutes — pass _async: true and poll the returned task with "
        "check_async."
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
        model: Model reference as 'provider/model' (e.g.
            'bailian_personal/happyhorse-1.1-t2v') or a bare configured
            model name. Uses the configured video default when omitted.
        image: Absolute local path to a reference image for
            image-to-video / reference-to-video models. Uploaded to the
            provider automatically.
        resolution: Output resolution, e.g. '720P', '1080P'. Uses the
            model default when omitted.
        ratio: Aspect ratio, e.g. '16:9', '9:16', '1:1'. Uses the model
            default when omitted.
        duration: Video duration in seconds. Uses the model default when
            omitted.
        folder: Directory to save the video into (default: the working
            directory).
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
            image_path=_resolve_input_path(image),
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
        audio_path = _resolve_input_path(path)
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


def main() -> None:
    run_plugin_server(mcp)


if __name__ == "__main__":
    main()
