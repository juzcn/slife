"""Tests for slife.tools.vision — IncludeImageTool."""

import pytest; pytestmark = pytest.mark.unit


from unittest.mock import MagicMock, patch

from slife.tools.vision import IncludeImageTool


class TestIncludeImageTool:
    """Tests for IncludeImageTool."""

    def test_name(self):
        assert IncludeImageTool.name == "include_image"

    def test_category(self):
        assert IncludeImageTool.category == "Vision"

    def test_requires_vision(self):
        assert IncludeImageTool._requires_vision is True

    def test_description(self):
        assert "Include an image" in IncludeImageTool.description

    def test_parameters_schema(self):
        params = IncludeImageTool.parameters
        assert params["type"] == "object"
        assert "source" in params["properties"]
        assert params["required"] == ["source"]

    @pytest.mark.asyncio
    async def test_execute_calls_include_image_url(self):
        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}

        with patch(
            "slife.agent.multimodal.include_image_url",
            return_value=fake_block,
        ):
            tool = IncludeImageTool()
            result = await tool.execute(source="D:\\photo.jpg")

        assert result == "Image included: D:\\photo.jpg"

    @pytest.mark.asyncio
    async def test_execute_invalid_source(self):
        with patch(
            "slife.agent.multimodal.include_image_url", return_value=None
        ):
            tool = IncludeImageTool()
            result = await tool.execute(source="D:\\missing.jpg")

        assert result.startswith("Error:")
        assert "cannot read image" in result

    @pytest.mark.asyncio
    async def test_execute_with_conversation_context(self):
        """When _ctx.conversation is set, images are injected."""
        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}

        mock_conv = MagicMock()
        mock_conv.inject_images_to_last_user = MagicMock()

        mock_ctx = MagicMock()
        mock_ctx.conversation = mock_conv

        with patch(
            "slife.agent.multimodal.include_image_url",
            return_value=fake_block,
        ):
            tool = IncludeImageTool()
            object.__setattr__(tool, "_ctx", mock_ctx)
            result = await tool.execute(source="D:\\img.jpg")

        assert result == "Image included: D:\\img.jpg"
        mock_conv.inject_images_to_last_user.assert_called_once_with(
            [fake_block]
        )
