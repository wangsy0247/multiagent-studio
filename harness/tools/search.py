"""Search tools with graceful fallbacks when no API keys are available."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _simulate_web_results(query: str, num_results: int) -> str:
    return (
        f"[simulated web search results for '{query}']\n"
        + "\n".join(
            f"{i + 1}. Simulated result {i + 1} - no search API key configured"
            for i in range(min(num_results, 5))
        )
    )


def _call_serpapi(query: str, num_results: int, api_key: str) -> str | None:
    """Call SerpAPI Google search and return formatted results.

    Returns None if the request fails or returns no organic results.
    """
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google",
                "q": query,
                "api_key": api_key,
                "num": min(num_results, 10),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])
        if not results:
            return None

        lines = []
        for r in results[:num_results]:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            link = r.get("link", "")
            line = f"- {title}: {snippet}"
            if link:
                line += f"\n  来源: {link}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("SerpAPI search failed: %s", exc)
        return None


def create_web_search_tool() -> Any:
    """Create the ``web_search`` tool."""

    @tool
    def web_search(query: str, num_results: int = 5) -> str:
        """Search the web for information.

        Args:
            query: Search query.
            num_results: Maximum number of results.
        """
        has_tavily = bool(os.getenv("TAVILY_API_KEY"))
        has_serpapi = bool(os.getenv("SERPAPI_API_KEY"))
        if not has_tavily and not has_serpapi:
            return _simulate_web_results(query, num_results)

        # Try Tavily first
        if has_tavily:
            try:
                resp = requests.post(
                    "https://api.tavily.com/search",
                    json={"query": query, "max_results": num_results},
                    headers={"Authorization": f"Bearer {os.getenv('TAVILY_API_KEY')}"},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                lines = []
                for r in results[:num_results]:
                    title = r.get("title", "")
                    content = r.get("content", "")
                    url = r.get("url", "")
                    line = f"- {title}: {content}"
                    if url:
                        line += f"\n  来源: {url}"
                    lines.append(line)
                return "\n".join(lines)
            except Exception as exc:
                logger.warning("Tavily search failed: %s", exc)

        # Fallback to SerpAPI
        if has_serpapi:
            serpapi_key = os.getenv("SERPAPI_API_KEY")
            result = _call_serpapi(query, num_results, serpapi_key)
            if result is not None:
                return result

        return _simulate_web_results(query, num_results)

    return web_search


def create_arxiv_search_tool() -> Any:
    """Create the ``arxiv_search`` tool."""

    @tool
    def arxiv_search(query: str, max_results: int = 5) -> str:
        """Search arXiv for papers.

        Args:
            query: Search query.
            max_results: Maximum number of papers.
        """
        try:
            url = "http://export.arxiv.org/api/query"
            params = {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": min(max_results, 10),
            }
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            text = resp.text
            import xml.etree.ElementTree as ET

            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)
            lines = []
            for entry in entries[:max_results]:
                title = entry.findtext("atom:title", "", ns)
                summary = entry.findtext("atom:summary", "", ns)
                paper_url = entry.findtext("atom:id", "", ns)
                line = f"- {title.strip()}\n  {summary[:300].strip()}"
                if paper_url:
                    line += f"\n  来源: {paper_url.strip()}"
                lines.append(line)
            return "\n".join(lines) if lines else "[info] no arXiv results found"
        except Exception as exc:
            logger.warning("arXiv search failed: %s", exc)
            return (
                f"[simulated arXiv results for '{query}']\n"
                + "\n".join(
                    f"{i + 1}. Simulated arXiv paper {i + 1}"
                    for i in range(min(max_results, 5))
                )
            )

    return arxiv_search


def create_paper_search_tool() -> Any:
    """Create the ``paper_search`` tool (alias for arxiv_search)."""

    @tool
    def paper_search(query: str, max_results: int = 5) -> str:
        """Search for academic papers.

        Args:
            query: Search query.
            max_results: Maximum number of papers.
        """
        arxiv = create_arxiv_search_tool()
        return arxiv.invoke({"query": query, "max_results": max_results})

    return paper_search


def build_search_tools() -> list[Any]:
    """Return all search tools."""
    return [
        create_web_search_tool(),
        create_arxiv_search_tool(),
        create_paper_search_tool(),
    ]


# Module-level convenience instances.
web_search = create_web_search_tool()
arxiv_search = create_arxiv_search_tool()
paper_search = create_paper_search_tool()
