"""Tests for AttachImageTool (slife.tools.models)."""

import pytest; pytestmark = pytest.mark.unit


from unittest.mock import MagicMock, patch

from slife.config import Config, ModelConfig
from slife.tools.models import AttachImageTool


class TestAttachImageTool:
    """Tests for AttachImageTool."""

    def test_name(self):
        assert AttachImageTool.name == "attach_image"

    def test_category(self):
        assert AttachImageTool.category == "Models"

    def test_requires_vision(self):
        assert AttachImageTool._requires_vision is True

    def test_description(self):
        assert "Attach one or more images" in AttachImageTool.description

    def test_parameters_schema(self):
        params = AttachImageTool.parameters
        assert params["type"] == "object"
        assert "sources" in params["properties"]
        assert params["properties"]["sources"]["type"] == "array"
        assert "source" in params["properties"]
        # Either sources (list) or source (single) — never both required.
        assert params.get("oneOf") == [
            {"required": ["sources"]},
            {"required": ["source"]},
        ]
        assert "required" not in params

    @pytest.mark.asyncio
    async def test_execute_single_source_alias(self):
        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}
        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=([fake_block], []),
        ):
            tool = AttachImageTool()
            result = await tool.execute(source="D:\\photo.jpg")

        assert result == "Image included: D:\\photo.jpg"

    @pytest.mark.asyncio
    async def test_execute_multiple_sources(self):
        blocks = [
            {"type": "image_url", "image_url": {"url": "data:1"}},
            {"type": "image_url", "image_url": {"url": "data:2"}},
        ]
        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=(blocks, []),
        ):
            tool = AttachImageTool()
            result = await tool.execute(
                sources=["D:\\a.jpg", "https://example.com/b.png"],
            )

        assert result == "Image included: D:\\a.jpg, https://example.com/b.png"

    @pytest.mark.asyncio
    async def test_execute_invalid_source(self):
        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=([], ["D:\\missing.jpg"]),
        ):
            tool = AttachImageTool()
            result = await tool.execute(sources=["D:\\missing.jpg"])

        assert result.startswith("Error:")
        assert "cannot read image" in result
        assert "missing.jpg" in result

    @pytest.mark.asyncio
    async def test_execute_partial_failure_reports_missing(self):
        ok = {"type": "image_url", "image_url": {"url": "data:ok"}}
        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=([ok], ["D:\\missing.jpg"]),
        ):
            tool = AttachImageTool()
            result = await tool.execute(sources=["D:\\a.jpg", "D:\\missing.jpg"])

        assert "Image included: D:\\a.jpg" in result
        assert "Could not read: D:\\missing.jpg" in result
        # Partial failure is not a hard error — valid images still attached.
        assert not result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_execute_with_message_history(self):
        """When _ctx.message_history is set, all image blocks are injected."""
        blocks = [
            {"type": "image_url", "image_url": {"url": "data:1"}},
            {"type": "image_url", "image_url": {"url": "data:2"}},
        ]
        mock_conv = MagicMock()
        mock_conv.inject_images_to_last_user = MagicMock()

        mock_ctx = MagicMock()
        mock_ctx.config = None
        mock_ctx.message_history = mock_conv

        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=(blocks, []),
        ):
            tool = AttachImageTool()
            object.__setattr__(tool, "_ctx", mock_ctx)
            result = await tool.execute(
                sources=["D:\\a.jpg", "D:\\b.jpg"],
            )

        assert "Image included: D:\\a.jpg, D:\\b.jpg" in result
        mock_conv.inject_images_to_last_user.assert_called_once_with(blocks)


class TestResolveSources:
    """_resolve_sources — argument shape handling."""

    def test_sources_list(self):
        assert AttachImageTool._resolve_sources(
            {"sources": ["a", "b"]},
        ) == ["a", "b"]

    def test_source_single(self):
        assert AttachImageTool._resolve_sources(
            {"source": "a"},
        ) == ["a"]

    def test_string_in_sources_slot(self):
        # An LLM occasionally sends a bare string in the array slot.
        assert AttachImageTool._resolve_sources(
            {"sources": "a"},
        ) == ["a"]

    def test_missing_both_raises(self):
        with pytest.raises(ValueError):
            AttachImageTool._resolve_sources({})

    def test_duplicates_deduped_preserving_order(self):
        assert AttachImageTool._resolve_sources(
            {"sources": ["a", "b", "a", "c", "b"]},
        ) == ["a", "b", "c"]

    def test_duplicate_single_not_created(self):
        assert AttachImageTool._resolve_sources({"source": "a"}) == ["a"]


def _config_with_vision(supports_vision: bool) -> Config:
    mc = ModelConfig(
        ref="test/m",
        provider="test",
        api_model="m",
        display_name="M",
        api_key="k",
        supports_vision=supports_vision,
    )
    return Config(models=[mc], active_model_ref="test/m", tools=[])


class TestAttachImageVisionGate:
    """Runtime gate: a non-vision active model is refused, not silently fed."""

    @pytest.mark.asyncio
    async def test_non_vision_model_refuses(self):
        ctx = MagicMock()
        ctx.config = _config_with_vision(False)
        ctx.message_history = MagicMock()

        tool = AttachImageTool()
        object.__setattr__(tool, "_ctx", ctx)
        result = await tool.execute(sources=["D:\\img.jpg"])

        assert result.startswith("Error:")
        assert "does not support image input" in result
        # The image must never reach the history / vision API.
        ctx.message_history.inject_images_to_last_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_vision_model_allows(self):
        ctx = MagicMock()
        ctx.config = _config_with_vision(True)
        ctx.message_history = MagicMock()

        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}
        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=([fake_block], []),
        ):
            tool = AttachImageTool()
            object.__setattr__(tool, "_ctx", ctx)
            result = await tool.execute(sources=["D:\\img.jpg"])

        assert result == "Image included: D:\\img.jpg"
        ctx.message_history.inject_images_to_last_user.assert_called_once_with(
            [fake_block]
        )

    @pytest.mark.asyncio
    async def test_missing_config_does_not_block(self):
        """No config on ctx falls back to the old behavior (best-effort)."""
        ctx = MagicMock()
        ctx.config = None
        ctx.message_history = MagicMock()

        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}
        with patch(
            "slife.agent.multimodal.include_image_urls",
            return_value=([fake_block], []),
        ):
            tool = AttachImageTool()
            object.__setattr__(tool, "_ctx", ctx)
            result = await tool.execute(sources=["D:\\img.jpg"])

        assert result == "Image included: D:\\img.jpg"


class TestAttachImageAlwaysLoaded:
    """attach_image is always registered, regardless of the active model's
    vision support — the runtime gate in execute() refuses non-vision models."""

    def test_included_for_non_vision_config(self):
        from slife.tools.factory import create_tools_from_config

        registry = create_tools_from_config(config=_config_with_vision(False))
        assert registry.get("attach_image") is not None

    def test_included_for_vision_config(self):
        from slife.tools.factory import create_tools_from_config

        registry = create_tools_from_config(config=_config_with_vision(True))
        assert registry.get("attach_image") is not None

    def test_included_without_config(self):
        """No config at all — still loaded (factory no longer filters)."""
        from slife.tools.factory import create_tools_from_config

        registry = create_tools_from_config()
        assert registry.get("attach_image") is not None
