"""
FetchURL Tool — fetch and extract readable text from a web page.

Attempts direct HTTP GET with HTML-to-text extraction first (no external
API key required).  Falls back to Jina AI Reader when the direct fetch
returns too little text or the site blocks direct requests.
"""
from __future__ import annotations

import logging
from html.parser import HTMLParser
from typing import Any

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# HTML → text extractor (stdlib only, no BeautifulSoup dependency)
# ══════════════════════════════════════════════════════════════════════════════


class _HtmlTextExtractor(HTMLParser):
    """Lightweight HTML-to-text extractor using only the stdlib."""

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


# ══════════════════════════════════════════════════════════════════════════════
# Tool factory
# ══════════════════════════════════════════════════════════════════════════════


def create_web_fetch_tool() -> Any:
    """Create the ``web_fetch`` tool — HTTP GET + HTML extraction + Jina AI fallback."""

    @tool("web_fetch", parse_docstring=True)
    def web_fetch(url: str) -> str:
        """Fetch and extract the main text content of a web page.

        Use this tool to read the full content of a specific URL when
        search-result snippets are insufficient.  Returns the first
        8 000 characters of readable text.

        Args:
            url: The full URL to fetch (must include scheme, e.g. https://example.com).
        """
        if not url.startswith(("http://", "https://")):
            return "Error: URL must start with http:// or https://"

        # ── Primary path: direct HTTP GET + stdlib HTML-to-text ──
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
            logger.warning(
                "Direct fetch returned too little text (%d chars), trying Jina AI",
                len(text),
            )
        except Exception as exc:
            logger.warning("Direct fetch failed: %s", exc)

        # ── Fallback: Jina AI Reader (handles JS-rendered / blocked pages) ──
        try:
            jina_url = f"https://r.jina.ai/{url}"
            resp = requests.get(
                jina_url,
                timeout=30,
                headers={"Accept": "text/plain"},
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                return "Error: Jina AI returned empty content"
            return text[:8000]
        except Exception as exc:
            logger.warning("Jina AI fetch failed: %s", exc)
            return f"Error: failed to fetch {url}: {exc}"

    return web_fetch


# Module-level convenience instance.
web_fetch = create_web_fetch_tool()
