"""Serper.dev web search tool."""

import httpx

from slife.tools.base import Tool


class SerperSearchTool(Tool):
    """Search the web via Serper.dev (Google Search API)."""

    name = "web_search"
    description = (
        "Search the web using Google. "
        "Returns organic search results with titles, snippets, and links. "
        "Use this to find current information, news, or facts on the web."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web",
            },
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def execute(self, query: str) -> str:
        """Execute a web search via the Serper API."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                json={"q": query},
                headers={"X-API-KEY": self.api_key},
            )
            response.raise_for_status()
            data = response.json()
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
