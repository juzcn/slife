"""Tests for the media plugin — non-chat model integration.

Mocks adapters / HTTP (no network) and exercises the MCP tool functions
directly, following the a2a plugin test pattern.
"""

import json5
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.unit

import slife.plugins.media.server as plugin
from slife.plugins.media import config as config_mod
from slife.plugins.media.adapters import dashscope_aigc
from slife.plugins.media.adapters.base import ArtifactSaver, MediaAdapterError
from slife.plugins.media.adapters.dashscope_aigc import DashScopeAIGCAdapter
from slife.plugins.media.adapters.openai_compat import OpenAICompatAdapter
from slife.plugins.media.config import (
    MediaConfig,
    MediaConfigError,
    ModelEntry,
    ProviderConfig,
    load_media_config,
)


# ── helpers ────────────────────────────────────────────────────────────


def _provider(**overrides):
    base = dict(
        api="dashscope-aigc",
        base_url="https://example.com/api/v1",
        api_key="sk-test",
    )
    base.update(overrides)
    return ProviderConfig(
        api=base["api"], base_url=base["base_url"], api_key=base["api_key"],
        models=base.get("models", []),
    )


def _full_config():
    return MediaConfig(
        defaults={"image": "test/img", "video": "test/vid"},
        providers={
            "test": _provider(models=[
                ModelEntry(model="img", kind="image"),
                ModelEntry(
                    model="vid", kind="video",
                    params={"resolution": "720P", "duration": 5},
                ),
                ModelEntry(model="say", kind="tts", voice="v1"),
                ModelEntry(model="hear", kind="asr"),
            ]),
        },
    )


def _write_config(tmp_path, section):
    path = tmp_path / "slife.json5"
    path.write_text(json5.dumps({"media": section}), encoding="utf-8")
    return path


@pytest.fixture
def fresh_plugin():
    """Reset plugin module globals around each test."""
    saved = (plugin._config, dict(plugin._adapters))
    plugin._config = None
    plugin._adapters = {}
    yield plugin
    plugin._config, plugin._adapters = saved[0], saved[1]


def _fake_adapter(**results):
    adapter = MagicMock()
    adapter.generate_image = AsyncMock(
        return_value=results.get("image", "/tmp/img.png"))
    adapter.generate_video = AsyncMock(
        return_value=results.get("video", "/tmp/vid.mp4"))
    adapter.text_to_speech = AsyncMock(
        return_value=results.get("tts", "/tmp/say.wav"))
    adapter.transcribe_audio = AsyncMock(
        return_value=results.get("asr", "hello world"))
    return adapter


# ═══════════════════════════════════════════════════════════════════════
# Config parsing + resolution
# ═══════════════════════════════════════════════════════════════════════


class TestLoadMediaConfig:
    def test_missing_section_is_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "slife.json5"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
        assert load_media_config().is_empty()

    def test_parses_providers_models_defaults(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, {
            "defaults": {"image": "p/m1"},
            "providers": {
                "p": {
                    "api": "dashscope-aigc",
                    "base_url": "https://h/api/v1/",
                    "api_key": "sk-x",
                    "models": [
                        {"model": "m1", "kind": "image"},
                        {"model": "m2", "kind": "video",
                         "params": {"resolution": "720P"}, "voice": ""},
                        {"model": "m3", "kind": "tts", "voice": "longx"},
                        {"model": "skip-me"},           # no kind → skipped
                        {"model": "m4", "kind": "weird"},  # bad kind → skipped
                    ],
                },
            },
        })
        monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
        cfg = load_media_config()
        assert not cfg.is_empty()
        assert cfg.defaults == {"image": "p/m1"}
        p = cfg.providers["p"]
        assert p.base_url == "https://h/api/v1"  # trailing slash stripped
        assert [m.model for m in p.models] == ["m1", "m2", "m3"]
        assert p.models[1].params == {"resolution": "720P"}
        assert p.models[2].voice == "longx"
        assert cfg.kinds_available() == {"image", "video", "tts"}

    def test_env_var_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEDIA_TEST_KEY", "sk-from-env")
        path = _write_config(tmp_path, {
            "providers": {"p": {
                "api": "dashscope-aigc", "base_url": "https://h",
                "api_key": "${MEDIA_TEST_KEY}",
                "models": [{"model": "m", "kind": "image"}],
            }},
        })
        monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
        cfg = load_media_config()
        assert cfg.providers["p"].api_key == "sk-from-env"

    def test_unresolvable_env_skips_provider(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, {
            "providers": {"p": {
                "api": "dashscope-aigc", "base_url": "https://h",
                "api_key": "${SURELY_NOT_SET_MEDIA_VAR}",
                "models": [{"model": "m", "kind": "image"}],
            }},
        })
        monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
        assert load_media_config().is_empty()


class TestResolveModel:
    def setup_method(self):
        self.cfg = _full_config()

    def test_explicit_provider_model_ref(self):
        pid, pcfg, entry = self.cfg.resolve_model("image", "test/img")
        assert (pid, entry.model) == ("test", "img")

    def test_bare_model_name(self):
        pid, _, entry = self.cfg.resolve_model("asr", "hear")
        assert entry.kind == "asr"

    def test_default_when_no_ref(self):
        _, _, entry = self.cfg.resolve_model("video", None)
        assert entry.model == "vid"

    def test_fallback_first_of_kind_without_default(self):
        cfg = MediaConfig(defaults={}, providers=self.cfg.providers)
        _, _, entry = cfg.resolve_model("tts", None)
        assert entry.model == "say"

    def test_unknown_provider(self):
        with pytest.raises(MediaConfigError, match="Unknown media provider"):
            self.cfg.resolve_model("image", "nope/img")

    def test_unknown_model(self):
        with pytest.raises(MediaConfigError, match="Unknown model"):
            self.cfg.resolve_model("image", "test/nope")

    def test_kind_mismatch(self):
        with pytest.raises(MediaConfigError, match="kind"):
            self.cfg.resolve_model("image", "test/vid")

    def test_nothing_of_kind(self):
        cfg = MediaConfig(
            providers={"t": _provider(models=[ModelEntry("i", "image")])})
        with pytest.raises(MediaConfigError, match="No video model"):
            cfg.resolve_model("video", None)


# ═══════════════════════════════════════════════════════════════════════
# Tool error paths
# ═══════════════════════════════════════════════════════════════════════


class TestToolErrors:
    @pytest.mark.asyncio
    async def test_no_config(self, fresh_plugin, monkeypatch):
        monkeypatch.setattr(
            plugin, "load_media_config", lambda: MediaConfig())
        result = await plugin.generate_image(prompt="x")
        assert result.startswith("Error: No media provider configured")

    @pytest.mark.asyncio
    async def test_unknown_model_ref(self, fresh_plugin):
        plugin._config = _full_config()
        result = await plugin.generate_image(prompt="x", model="test/nope")
        assert result.startswith("Error: Unknown model")

    @pytest.mark.asyncio
    async def test_kind_mismatch_ref(self, fresh_plugin):
        plugin._config = _full_config()
        result = await plugin.generate_image(prompt="x", model="test/vid")
        assert result.startswith("Error:") and "kind" in result

    @pytest.mark.asyncio
    async def test_missing_input_file(self, fresh_plugin):
        plugin._config = _full_config()
        plugin._adapters["test"] = _fake_adapter()
        result = await plugin.generate_image(
            prompt="x", image="/no/such/file.png")
        assert result.startswith("Error: File not found")

    @pytest.mark.asyncio
    async def test_adapter_error_surfaced(self, fresh_plugin):
        plugin._config = _full_config()
        adapter = _fake_adapter()
        adapter.generate_image = AsyncMock(
            side_effect=MediaAdapterError("Bailian API error (401): bad key",
                                          status_code=401))
        plugin._adapters["test"] = adapter
        result = await plugin.generate_image(prompt="x")
        assert result.startswith("Error: Bailian API error (401)")

    @pytest.mark.asyncio
    async def test_unsupported_capability(self, fresh_plugin):
        plugin._config = _full_config()
        adapter = _fake_adapter()
        adapter.generate_video = AsyncMock(
            side_effect=NotImplementedError("video generation"))
        plugin._adapters["test"] = adapter
        result = await plugin.generate_video(prompt="x")
        assert result.startswith("Error: Capability not supported")


# ═══════════════════════════════════════════════════════════════════════
# Tool success paths (fake adapter)
# ═══════════════════════════════════════════════════════════════════════


class TestToolSuccess:
    @pytest.mark.asyncio
    async def test_generate_image_uses_default_and_params(self, fresh_plugin):
        plugin._config = _full_config()
        adapter = _fake_adapter()
        plugin._adapters["test"] = adapter
        result = await plugin.generate_image(prompt="a cat", size="512*512")
        assert result == "/tmp/img.png"
        adapter.generate_image.assert_awaited_once()
        kwargs = adapter.generate_image.call_args.kwargs
        assert kwargs["model"] == "img"
        assert kwargs["size"] == "512*512"
        assert kwargs["extra_params"] == {}

    @pytest.mark.asyncio
    async def test_generate_video_merges_overrides(self, fresh_plugin):
        plugin._config = _full_config()
        adapter = _fake_adapter()
        plugin._adapters["test"] = adapter
        result = await plugin.generate_video(
            prompt="a ball", resolution="1080P", duration=8)
        assert result == "/tmp/vid.mp4"
        params = adapter.generate_video.call_args.kwargs["extra_params"]
        # entry params (resolution 720P, duration 5) overridden by call
        assert params == {"resolution": "1080P", "duration": 8}

    @pytest.mark.asyncio
    async def test_tts_voice_falls_back_to_config(self, fresh_plugin):
        plugin._config = _full_config()
        adapter = _fake_adapter()
        plugin._adapters["test"] = adapter
        await plugin.text_to_speech(text="hi")
        assert adapter.text_to_speech.call_args.kwargs["voice"] == "v1"
        await plugin.text_to_speech(text="hi", voice="other")
        assert adapter.text_to_speech.call_args.kwargs["voice"] == "other"

    @pytest.mark.asyncio
    async def test_transcribe_returns_text(self, fresh_plugin, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"fake")
        plugin._config = _full_config()
        adapter = _fake_adapter()
        plugin._adapters["test"] = adapter
        result = await plugin.transcribe_audio(path=str(audio))
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_transcribe_empty_path(self, fresh_plugin):
        plugin._config = _full_config()
        plugin._adapters["test"] = _fake_adapter()
        result = await plugin.transcribe_audio(path="")
        assert result.startswith("Error:")


# ═══════════════════════════════════════════════════════════════════════
# DashScope adapter — request shapes (mocked HTTP layer)
# ═══════════════════════════════════════════════════════════════════════


def _ds_adapter():
    return DashScopeAIGCAdapter(_provider())


class TestDashScopeAdapter:
    @pytest.mark.asyncio
    async def test_generate_image_request_shape(self, monkeypatch):
        adapter = _ds_adapter()
        captured = {}

        async def fake_request(method, url, *, extra_headers=None,
                               json_body=None):
            captured.update(method=method, url=url, body=json_body)
            return {"output": {"choices": [{"message": {"content": [
                {"image": "https://cdn/x.png"}]}}]}}

        monkeypatch.setattr(adapter, "_request", fake_request)
        saved = Path("/tmp/saved.png")
        monkeypatch.setattr(
            adapter._saver, "save_url",
            AsyncMock(return_value=saved))
        result = await adapter.generate_image(
            model="wan2.7-image", prompt="a cat", size="1024*1024")
        assert result == str(saved)
        assert captured["url"].endswith(
            "/services/aigc/multimodal-generation/generation")
        body = captured["body"]
        assert body["model"] == "wan2.7-image"
        content = body["input"]["messages"][0]["content"]
        assert content == [{"text": "a cat"}]
        assert body["parameters"] == {"size": "1024*1024"}

    @pytest.mark.asyncio
    async def test_generate_image_no_image_in_response(self, monkeypatch):
        adapter = _ds_adapter()

        async def fake_request(method, url, **kw):
            return {"output": {"choices": [{"message": {"content": [
                {"text": "sorry"}]}}]}}

        monkeypatch.setattr(adapter, "_request", fake_request)
        with pytest.raises(MediaAdapterError, match="No image"):
            await adapter.generate_image(model="m", prompt="p")

    @pytest.mark.asyncio
    async def test_video_submit_header_and_poll(self, monkeypatch):
        adapter = _ds_adapter()
        calls = []
        responses = [
            {"output": {"task_id": "t-1", "task_status": "PENDING"}},
            {"output": {"task_status": "RUNNING"}},
            {"output": {"task_status": "SUCCEEDED",
                        "video_url": "https://cdn/v.mp4"}},
        ]

        async def fake_request(method, url, *, extra_headers=None,
                               json_body=None):
            calls.append((method, url, extra_headers))
            return responses[len(calls) - 1]

        monkeypatch.setattr(adapter, "_request", fake_request)
        monkeypatch.setattr(dashscope_aigc.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(
            adapter._saver, "save_url",
            AsyncMock(return_value=Path("/tmp/v.mp4")))
        result = await adapter.generate_video(model="hh", prompt="bounce")
        assert result == str(Path("/tmp/v.mp4"))
        submit = calls[0]
        assert submit[1].endswith("/video-generation/video-synthesis")
        assert submit[2] == {"X-DashScope-Async": "enable"}
        assert calls[1][0] == "GET" and calls[1][1].endswith("/tasks/t-1")

    @pytest.mark.asyncio
    async def test_video_failed_task(self, monkeypatch):
        adapter = _ds_adapter()
        responses = iter([
            {"output": {"task_id": "t-2"}},
            {"output": {"task_status": "FAILED", "message": "boom"}},
        ])
        monkeypatch.setattr(
            adapter, "_request",
            AsyncMock(side_effect=lambda *a, **kw: next(responses)))
        monkeypatch.setattr(dashscope_aigc.asyncio, "sleep", AsyncMock())
        with pytest.raises(MediaAdapterError, match="boom"):
            await adapter.generate_video(model="hh", prompt="x")

    @pytest.mark.asyncio
    async def test_video_deadline_includes_task_id(self, monkeypatch):
        adapter = _ds_adapter()
        responses = iter([{"output": {"task_id": "t-3"}}])

        async def fake_request(method, url, **kw):
            try:
                return next(responses)
            except StopIteration:
                return {"output": {"task_status": "RUNNING"}}

        monkeypatch.setattr(adapter, "_request", fake_request)
        monkeypatch.setattr(dashscope_aigc.asyncio, "sleep", AsyncMock())
        with pytest.raises(MediaAdapterError, match="t-3"):
            await adapter.generate_video(
                model="hh", prompt="x", deadline_s=0)

    @pytest.mark.asyncio
    async def test_tts_flat_input_and_audio_url(self, monkeypatch):
        adapter = _ds_adapter()
        captured = {}

        async def fake_request(method, url, *, extra_headers=None,
                               json_body=None):
            captured["body"] = json_body
            return {"output": {"audio": {"url": "https://cdn/a.wav"}}}

        monkeypatch.setattr(adapter, "_request", fake_request)
        monkeypatch.setattr(
            adapter._saver, "save_url",
            AsyncMock(return_value=Path("/tmp/a.wav")))
        result = await adapter.text_to_speech(
            model="tts", text="hi", voice="longxiaochun")
        assert result == str(Path("/tmp/a.wav"))
        assert captured["body"] == {
            "model": "tts",
            "input": {"text": "hi", "voice": "longxiaochun"},
        }

    def test_audio_mime_is_platform_independent(self):
        """``_audio_mime`` must not depend on the host's mimetypes DB — Windows
        maps ``.wav`` → ``audio/x-wav``, Linux → ``audio/wav``.  Pin the
        canonical value so the Data URI is identical on every machine."""
        from pathlib import Path
        from slife.plugins.media.adapters.dashscope_aigc import DashScopeAIGCAdapter

        assert DashScopeAIGCAdapter._audio_mime(Path("a.wav")) == "audio/wav"
        assert DashScopeAIGCAdapter._audio_mime(Path("a.WAV")) == "audio/wav"
        assert DashScopeAIGCAdapter._audio_mime(Path("a.mp3")) == "audio/mpeg"

    @pytest.mark.asyncio
    async def test_transcribe_sends_data_uri(self, monkeypatch, tmp_path):
        adapter = _ds_adapter()
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"fake")
        captured = {}

        async def fake_request(method, url, *, extra_headers=None,
                               json_body=None):
            captured.update(method=method, url=url, headers=extra_headers,
                            body=json_body)
            return {"output": {"choices": [{"message": {"content": [
                {"text": "hello"}]}}]}}

        monkeypatch.setattr(adapter, "_request", fake_request)
        result = await adapter.transcribe_audio(model="asr", audio_path=audio)
        assert result == "hello"
        # The flash ASR models (qwen-audio-3.0-asr-flash / fun-asr-flash)
        # take a base64 Data URI on the multimodal generation endpoint —
        # no OSS upload / resolve header (that path 404s for them).
        assert captured["url"].endswith("/multimodal-generation/generation")
        assert captured["headers"] is None
        body = captured["body"]
        assert body["model"] == "asr"
        content = body["input"]["messages"][0]["content"]
        assert content == [{
            "type": "input_audio",
            "input_audio": {"data": "data:audio/wav;base64,ZmFrZQ=="},
        }]
        assert body["parameters"] == {"format": "wav", "sample_rate": "16000"}

    @pytest.mark.asyncio
    async def test_upload_file_two_step(self, monkeypatch, tmp_path):
        adapter = _ds_adapter()
        f = tmp_path / "pic.png"
        f.write_bytes(b"img")
        monkeypatch.setattr(adapter, "_get_upload_policy", AsyncMock(
            return_value={"data": {
                "oss_access_key_id": "AK", "policy": "POL",
                "signature": "SIG", "upload_dir": "dir/sub",
                "upload_host": "https://oss.example.com",
                "x_oss_forbid_overwrite": "true",
                "x_oss_object_acl": "private",
            }}))
        fake_client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        fake_client.post = AsyncMock(return_value=resp)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            dashscope_aigc.httpx, "AsyncClient",
            lambda **kw: fake_client)
        result = await adapter.upload_file(model="m", file_path=f)
        assert result == "oss://dir/sub/pic.png"
        form = fake_client.post.call_args.kwargs["data"]
        assert form["key"] == "dir/sub/pic.png"
        assert form["OSSAccessKeyId"] == "AK"
        assert form["x-oss-forbid-overwrite"] == "true"

    @pytest.mark.asyncio
    async def test_error_body_raises(self, monkeypatch):
        adapter = _ds_adapter()
        fake_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"code": "InvalidApiKey", "message": "bad"}
        fake_client.request = AsyncMock(return_value=resp)
        monkeypatch.setattr(adapter, "_ensure_client",
                            AsyncMock(return_value=fake_client))
        with pytest.raises(MediaAdapterError, match="InvalidApiKey"):
            await adapter._request("GET", "https://x")

    @pytest.mark.asyncio
    async def test_http_error_raises_with_status(self, monkeypatch):
        adapter = _ds_adapter()
        fake_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "unauthorized"
        fake_client.request = AsyncMock(return_value=resp)
        monkeypatch.setattr(adapter, "_ensure_client",
                            AsyncMock(return_value=fake_client))
        with pytest.raises(MediaAdapterError) as ei:
            await adapter._request("GET", "https://x")
        assert ei.value.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# OpenAI-compatible adapter
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAICompatAdapter:
    @pytest.mark.asyncio
    async def test_generate_image_url_response(self, monkeypatch):
        adapter = OpenAICompatAdapter(_provider(api="openai-images"))
        fake_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"data": [{"url": "https://cdn/i.png"}]}
        fake_client.post = AsyncMock(return_value=resp)
        monkeypatch.setattr(adapter, "_ensure_client",
                            AsyncMock(return_value=fake_client))
        monkeypatch.setattr(
            adapter._saver, "save_url",
            AsyncMock(return_value=Path("/tmp/i.png")))
        result = await adapter.generate_image(model="gpt-image-1", prompt="x")
        assert result == str(Path("/tmp/i.png"))
        url = fake_client.post.call_args.args[0]
        assert url.endswith("/images/generations")

    @pytest.mark.asyncio
    async def test_video_not_supported(self):
        adapter = OpenAICompatAdapter(_provider(api="openai-images"))
        with pytest.raises(NotImplementedError):
            await adapter.generate_video(model="m", prompt="p")


# ═══════════════════════════════════════════════════════════════════════
# Artifact saver
# ═══════════════════════════════════════════════════════════════════════


class TestArtifactSaver:
    def test_save_bytes_default_cwd(self, monkeypatch, tmp_path):
        """Generated artifacts default to the working directory — work
        products, not memfiles cabinet files."""
        monkeypatch.chdir(tmp_path)
        saver = ArtifactSaver()
        p1 = saver.save_bytes(b"a", "image", "png")
        p2 = saver.save_bytes(b"b", "image", ".png")
        assert p1 != p2
        assert p1.read_bytes() == b"a"
        assert p1.parent == tmp_path
        assert p1.suffix == ".png" and p2.suffix == ".png"

    def test_save_bytes_respects_outputs_dir(self, tmp_path):
        saver = ArtifactSaver()
        out = tmp_path / "sub"
        p = saver.save_bytes(b"x", "image", "jpg", str(out))
        assert p.parent == out
        assert p.read_bytes() == b"x"
