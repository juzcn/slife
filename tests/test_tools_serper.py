"""Tests for slife.tools.serper — Serper.dev web search tool."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.serper import SerperSearchTool


# ── Tool metadata ─────────────────────────────────────────────────────


class TestSerperMetadata:
    """Tests for SerperSearchTool class-level attributes."""

    def test_name(self):
        assert SerperSearchTool.name == "web_search"

    def test_description(self):
        assert "Search the web" in SerperSearchTool.description

    def test_parameters(self):
        params = SerperSearchTool.parameters
        assert params["type"] == "object"
        assert "query" in params["properties"]
        assert "query" in params["required"]


# ── Construction ─────────────────────────────────────────────────────


class TestSerperConstruction:
    """Tests for SerperSearchTool.__init__."""

    def test_api_key_stored(self):
        tool = SerperSearchTool(api_key="my-key")
        assert tool.api_key == "my-key"


# ── _format_results ──────────────────────────────────────────────────


class TestFormatResults:
    """Tests for SerperSearchTool._format_results."""

    def test_formats_organic_results(self):
        tool = SerperSearchTool(api_key="k")
        data = {
            "organic": [
                {
                    "title": "Test Title",
                    "snippet": "A snippet about testing.",
                    "link": "https://example.com",
                },
                {
                    "title": "Another Result",
                    "snippet": "More content here.",
                    "link": "https://example.org",
                },
            ]
        }
        result = tool._format_results(data)
        assert "Test Title" in result
        assert "A snippet about testing" in result
        assert "https://example.com" in result
        assert "Another Result" in result
        assert "https://example.org" in result

    def test_limits_to_10_results(self):
        tool = SerperSearchTool(api_key="k")
        data = {
            "organic": [
                {"title": f"Result {i}", "snippet": f"Snippet {i}", "link": f"https://{i}.com"}
                for i in range(20)
            ]
        }
        result = tool._format_results(data)
        # Count numbered entries
        lines = result.split("\n")
        numbered = [l for l in lines if l.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "10."))]
        assert len(numbered) <= 11  # 10 entries with possible "10." counted

    def test_no_results(self):
        tool = SerperSearchTool(api_key="k")
        result = tool._format_results({})
        assert result == "No results found."

    def test_empty_organic(self):
        tool = SerperSearchTool(api_key="k")
        result = tool._format_results({"organic": []})
        assert result == "No results found."

    def test_missing_fields(self):
        """Results with missing title/snippet/link get defaults."""
        tool = SerperSearchTool(api_key="k")
        data = {
            "organic": [
                {},
            ]
        }
        result = tool._format_results(data)
        assert "No title" in result
        assert "No snippet" in result


# ── execute ───────────────────────────────────────────────────────────


class TestSerperExecute:
    """Tests for SerperSearchTool.execute."""

    @pytest.mark.asyncio
    async def test_successful_search(self):
        """Execute returns formatted results on success."""
        tool = SerperSearchTool(api_key="test-key")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "organic": [
                {
                    "title": "Cats",
                    "snippet": "All about cats.",
                    "link": "https://cats.com",
                }
            ]
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("slife.tools.serper.httpx.AsyncClient", return_value=mock_client):
            result = await tool.execute(query="cats")

        assert "Cats" in result
        assert "cats.com" in result

    @pytest.mark.asyncio
    async def test_correct_api_call(self):
        """Verify the correct API endpoint and headers are used."""
        tool = SerperSearchTool(api_key="my-api-key")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"organic": []}

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("slife.tools.serper.httpx.AsyncClient", return_value=mock_client):
            await tool.execute(query="test query")

        mock_client.post.assert_called_once_with(
            "https://google.serper.dev/search",
            json={"q": "test query"},
            headers={"X-API-KEY": "my-api-key"},
        )

    @pytest.mark.asyncio
    async def test_http_error(self):
        """HTTP errors propagate as exceptions."""
        tool = SerperSearchTool(api_key="test-key")

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP 500")

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("slife.tools.serper.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(Exception, match="HTTP 500"):
                await tool.execute(query="test")
