import os

import httpx
from mcp.server.fastmcp import FastMCP

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")

mcp = FastMCP("searxng-search", host="0.0.0.0", port=8000, stateless_http=True)


@mcp.tool()
async def web_search(query: str, count: int = 5) -> str:
    """Search the web via the local SearXNG instance. Returns titles, URLs, and snippets."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(SEARXNG_URL, params={"q": query, "format": "json"})
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])[:count]
    if not results:
        return f"No results found for: {query}"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        lines.append(f"{i}. {title}\n   {url}\n   {content}")
    return "\n\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
