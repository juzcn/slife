"""Serper.dev web search tool."""

import logging
import os

import httpx

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class SerperSearchTool(Tool):
    """Search the web via Serper.dev (Google Search API).

    Reads SERPER_API_KEY from the environment (set via env section
    in slife.json5).
    """

    name = "web_search"
    description = "Search the web via Google. Returns titles, snippets, and links."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
        },
        "required": ["query"],
    }

    async def execute(self, query: str) -> str:
        """Execute a web search via the Serper API."""
        api_key = os.environ.get("SERPER_API_KEY", "")
        logger.debug("Search: %.100s", query)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                json={"q": query},
                headers={"X-API-KEY": api_key},
            )
            response.raise_for_status()
            data = response.json()
            count = len(data.get("organic", []))
            logger.debug("Search done: %d results for '%.50s'", count, query)
            return self._format_results(data)

    def _format_results(self, data: dict) -> str:
        """Format Serper API response into readable text."""
        lines = []

        for i, item in enumerate(data.get("organic", [])[:10], 1):
            title = item.get("title", "No title")
            snippet = item.get("snippet", "No snippet")
            link = item.get("link", "")
            lines.append(f"{i}. {title}\n   {snippet}\n   {link}")

        if not lines:
            return "No results found."

        return "\n\n".join(lines)
