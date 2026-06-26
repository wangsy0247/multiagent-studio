"""Search tools with graceful fallbacks when no API keys are available."""
from __future__ import annotations

import json
import logging
import os
from html.parser import HTMLParser
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


def _is_tavily_quota_error(exc: Exception) -> bool:
    """Detect Tavily quota/credit exhaustion."""
    text = str(exc).lower()
    indicators = [
        "quota",
        "credits",
        "insufficient",
        "limit",
        "exceeded",
        "too many requests",
    ]
    if any(ind in text for ind in indicators):
        return True

    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) in (429, 403, 402):
        return True

    return False


def _call_duckduckgo(query: str, num_results: int) -> str | None:
    """Call DuckDuckGo search as a free fallback."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.warning("duckduckgo-search is not installed; cannot use DuckDuckGo fallback")
        return None

    try:
        with DDGS(timeout=30) as ddgs:
            results = ddgs.text(query, max_results=num_results)
        if not results:
            return None

        lines = []
        for r in results[:num_results]:
            title = r.get("title", "")
            body = r.get("body", r.get("snippet", ""))
            href = r.get("href", r.get("link", ""))
            line = f"- {title}: {body}"
            if href:
                line += f"\n  来源: {href}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("DuckDuckGo search failed: %s", exc)
        return None


def _format_tavily_results(data: dict, num_results: int) -> str:
    """Format Tavily API response into plain text."""
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


class _HtmlTextExtractor(HTMLParser):
    """Very light HTML-to-text extractor using only the stdlib."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0
        self._skip_tags = {"script", "style", "nav", "footer", "header", "aside", "noscript"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip += 1
        if tag in {"br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags and self._skip > 0:
            self._skip -= 1
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        import re

        text = "".join(self._parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def _extract_text_from_html(html: str) -> str:
    """Extract readable text from raw HTML."""
    try:
        extractor = _HtmlTextExtractor()
        extractor.feed(html)
        return extractor.get_text()
    except Exception as exc:
        logger.warning("HTML text extraction failed: %s", exc)
        return ""


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

        # Try Tavily first (default provider).
        if has_tavily:
            try:
                resp = requests.post(
                    "https://api.tavily.com/search",
                    json={"query": query, "max_results": num_results},
                    headers={"Authorization": f"Bearer {os.getenv('TAVILY_API_KEY')}"},
                    timeout=30,
                )
                resp.raise_for_status()
                return _format_tavily_results(resp.json(), num_results)
            except Exception as exc:
                if _is_tavily_quota_error(exc):
                    logger.warning(
                        "Tavily quota/credits exhausted, falling back to DuckDuckGo: %s",
                        exc,
                    )
                else:
                    logger.warning("Tavily search failed: %s", exc)

        # Fallback 1: DuckDuckGo (free, no API key).
        result = _call_duckduckgo(query, num_results)
        if result is not None:
            logger.info("Using DuckDuckGo fallback for query: %s", query)
            return result

        # Final fallback: simulated placeholder results.
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


def create_web_fetch_tool() -> Any:
    """Create the ``web_fetch`` tool.

    Tries a direct HTTP fetch and extracts the main text from HTML.
    Falls back to Jina AI Reader if direct fetch fails or yields too little text.
    """

    @tool
    def web_fetch(url: str) -> str:
        """Fetch and extract the main text content of a web page.

        Args:
            url: The full URL to fetch (must include scheme, e.g. https://example.com).
        """
        if not url.startswith(("http://", "https://")):
            return "Error: URL must start with http:// or https://"

        # Primary path: direct fetch + stdlib HTML-to-text extraction.
        try:
            resp = requests.get(
                url,
                timeout=30,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            text = _extract_text_from_html(resp.text)
            if len(text) >= 200:
                return text[:8000]
            logger.warning("Direct fetch returned too little text (%d chars), trying Jina AI", len(text))
        except Exception as exc:
            logger.warning("Direct fetch failed: %s", exc)

        # Fallback: Jina AI Reader (useful when the site needs JS or blocks direct requests).
        try:
            jina_url = f"https://r.jina.ai/{url}"
            resp = requests.get(jina_url, timeout=30, headers={"Accept": "text/plain"})
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                return "Error: Jina AI returned empty content"
            return text[:8000]
        except Exception as exc:
            logger.warning("Jina AI fetch failed: %s", exc)
            return f"Error: failed to fetch {url}: {exc}"

    return web_fetch


def build_search_tools() -> list[Any]:
    """Return all search tools."""
    return [
        create_web_search_tool(),
        create_arxiv_search_tool(),
        create_web_fetch_tool(),
    ]


# Module-level convenience instances.
web_search = create_web_search_tool()
arxiv_search = create_arxiv_search_tool()
web_fetch = create_web_fetch_tool()
