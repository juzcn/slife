"""Tests for SerperSearchTool (slife.tools.serper)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.tools.serper import SerperSearchTool


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def serper_tool():
    """Create a SerperSearchTool with a test API key."""
    return SerperSearchTool(api_key="test-key")


@pytest.fixture
def mock_serper_response():
    """Mock successful Serper API response."""
    return {
        "organic": [
            {
                "title": "Cats - Wikipedia",
                "snippet": "The cat is a small domesticated carnivorous mammal.",
                "link": "https://en.wikipedia.org/wiki/Cat",
            },
            {
                "title": "Cat Photos",
                "snippet": "Browse cute cat photos.",
                "link": "https://example.com/cats",
            },
        ]
    }


# ══════════════════════════════════════════════════════════════════════
# Tool Metadata
# ══════════════════════════════════════════════════════════════════════


class TestSerperMetadata:
    """Tests for class-level metadata."""

    def test_name(self):
        """Tool name is 'web_search'."""
        assert SerperSearchTool.name == "web_search"

    def test_description(self):
        """Tool has a non-empty description."""
        assert len(SerperSearchTool.description) > 10
        assert "search" in SerperSearchTool.description.lower()

    def test_parameters_schema(self):
        """Parameters define a 'query' string parameter."""
        params = SerperSearchTool.parameters
        assert params["type"] == "object"
        assert "query" in params["properties"]
        assert params["properties"]["query"]["type"] == "string"
        assert "query" in params["required"]

    def test_to_openai_function(self):
        """to_openai_function() returns correct format."""
        func = SerperSearchTool.to_openai_function()
        assert func["type"] == "function"
        assert func["function"]["name"] == "web_search"


# ══════════════════════════════════════════════════════════════════════
# Initialization
# ══════════════════════════════════════════════════════════════════════


class TestSerperInit:
    """Tests for __init__()."""

    def test_stores_api_key(self):
        """API key is stored on the instance."""
        tool = SerperSearchTool(api_key="my-key")
        assert tool.api_key == "my-key"

    def test_accepts_empty_key(self):
        """Empty string API key is stored (API will reject it)."""
        tool = SerperSearchTool(api_key="")
        assert tool.api_key == ""


# ══════════════════════════════════════════════════════════════════════
# execute()
# ══════════════════════════════════════════════════════════════════════


class TestSerperExecute:
    """Tests for SerperSearchTool.execute()."""

    @pytest.mark.asyncio
    async def test_successful_search(self, serper_tool, mock_serper_response):
        """Successful search formats and returns results."""
        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_serper_response
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await serper_tool.execute(query="cats")

            assert "Cats - Wikipedia" in result
            assert "https://en.wikipedia.org/wiki/Cat" in result
            assert "Cat Photos" in result

    @pytest.mark.asyncio
    async def test_calls_serper_api(self, serper_tool, mock_serper_response):
        """execute() makes a POST to the Serper API."""
        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_serper_response
            mock_instance.post = AsyncMock(return_value=mock_response)

            await serper_tool.execute(query="test query")

            # Verify the POST request
            mock_instance.post.assert_called_once()
            call_args = mock_instance.post.call_args
            assert call_args[0][0] == "https://google.serper.dev/search"
            assert call_args[1]["json"] == {"q": "test query"}
            assert call_args[1]["headers"]["X-API-KEY"] == "test-key"

    @pytest.mark.asyncio
    async def test_no_results(self, serper_tool):
        """When no organic results, returns 'No results found.'"""
        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {"organic": []}
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await serper_tool.execute(query="xyznonexistent123")
            assert result == "No results found."

    @pytest.mark.asyncio
    async def test_empty_response(self, serper_tool):
        """When response has no 'organic' key at all."""
        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {}
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await serper_tool.execute(query="test")
            assert result == "No results found."

    @pytest.mark.asyncio
    async def test_http_error(self, serper_tool):
        """HTTP errors propagate (raise_for_status)."""
        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            import httpx
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=MagicMock(status_code=403)
            )
            mock_instance.post = AsyncMock(return_value=mock_response)

            with pytest.raises(httpx.HTTPStatusError):
                await serper_tool.execute(query="test")

    @pytest.mark.asyncio
    async def test_unicode_query(self, serper_tool, mock_serper_response):
        """Unicode queries are handled correctly."""
        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_serper_response
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await serper_tool.execute(query="café résumé")
            assert result  # Should not raise

    @pytest.mark.asyncio
    async def test_results_limited_to_10(self, serper_tool):
        """Only first 10 organic results are shown."""
        many_results = {
            "organic": [
                {"title": f"Result {i}", "snippet": f"Snippet {i}", "link": f"https://{i}.com"}
                for i in range(20)
            ]
        }

        with patch("slife.tools.serper.httpx.AsyncClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = many_results
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await serper_tool.execute(query="test")

            # Count numbered lines (each result starts with "N. ")
            import re
            numbered = re.findall(r"^\d+\.", result, re.MULTILINE)
            assert len(numbered) == 10
            assert "Result 9" in result   # 10th result, 0-indexed
            assert "Result 10" not in result  # 11th result, should not exist


# ══════════════════════════════════════════════════════════════════════
# _format_results()
# ══════════════════════════════════════════════════════════════════════


class TestSerperFormatResults:
    """Tests for SerperSearchTool._format_results()."""

    def test_formats_complete_result(self, serper_tool):
        """Complete result includes title, snippet, and link."""
        data = {
            "organic": [
                {
                    "title": "Test Title",
                    "snippet": "Test snippet text",
                    "link": "https://example.com",
                }
            ]
        }
        result = serper_tool._format_results(data)
        assert "1. Test Title" in result
        assert "Test snippet text" in result
        assert "https://example.com" in result

    def test_missing_title(self, serper_tool):
        """Missing title shows 'No title'."""
        data = {
            "organic": [
                {
                    "snippet": "Snippet",
                    "link": "https://example.com",
                }
            ]
        }
        result = serper_tool._format_results(data)
        assert "No title" in result

    def test_missing_snippet(self, serper_tool):
        """Missing snippet shows 'No snippet'."""
        data = {
            "organic": [
                {
                    "title": "Title",
                    "link": "https://example.com",
                }
            ]
        }
        result = serper_tool._format_results(data)
        assert "No snippet" in result

    def test_missing_link(self, serper_tool):
        """Missing link is shown as empty string."""
        data = {
            "organic": [
                {
                    "title": "Title",
                    "snippet": "Snippet",
                }
            ]
        }
        result = serper_tool._format_results(data)
        assert "Title" in result

    def test_empty_organic(self, serper_tool):
        """Empty organic list returns 'No results found.'"""
        result = serper_tool._format_results({"organic": []})
        assert result == "No results found."

    def test_no_organic_key(self, serper_tool):
        """Missing 'organic' key returns 'No results found.'"""
        result = serper_tool._format_results({})
        assert result == "No results found."
