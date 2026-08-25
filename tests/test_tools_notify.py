"""Tests for NotifyUserTool (slife.tools.meta)."""

import pytest; pytestmark = pytest.mark.unit

from unittest.mock import patch

from slife.tools.meta import NotifyUserTool


class TestNotifyUserTool:
    """Desktop notification — a pure UI tool in the Display category."""

    def test_category(self):
        assert NotifyUserTool.category == "Meta"

    def test_name(self):
        assert NotifyUserTool.name == "notify_user"

    @pytest.mark.asyncio
    async def test_missing_message(self):
        tool = NotifyUserTool()
        result = await tool.execute(message="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_notification_sent(self):
        tool = NotifyUserTool()
        with patch("slife.platform.desktop_notify"):
            result = await tool.execute(title="Test", message="Hello world")
        assert "Notification sent" in result
        assert "Test" in result
        assert "Hello world" in result
