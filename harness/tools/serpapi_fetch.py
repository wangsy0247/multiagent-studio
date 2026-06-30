"""
Web Fetch Tool - Fetch web search results using SerpAPI (Google Search API).

SerpAPI provides real-time Google Search results via a JSON API.
An API key is required. Sign up at https://serpapi.com to get one.

Reference implementation: deer-flow Serper tool (deerflow/community/serper/tools.py).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_SERPAPI_ENDPOINT = "https://serpapi.com/search"
_api_key_warned = False


def _get_api_key() -> str | None:
    """Resolve the SerpAPI API key from environment variables.

    Checks ``SERPAPI_API_KEY`` first, then falls back to the legacy
    ``SERP_API_KEY`` variable.
    """
    return os.getenv("SERPAPI_API_KEY") or os.getenv("SERP_API_KEY")


def create_web_fetch_tool() -> Any:
    """Create the ``web_fetch`` tool backed by SerpAPI."""

    @tool("web_fetch", parse_docstring=True)
    def web_fetch(query: str, num_results: int = 5) -> str:
        """Fetch web search results via Google Search (SerpAPI).

        Args:
            query: Search keywords describing what you want to find. Be specific for better results.
            num_results: Maximum number of search results to return. Default is 5.
        """
        global _api_key_warned

        api_key = _get_api_key()
        if not api_key:
            if not _api_key_warned:
                _api_key_warned = True
                logger.warning(
                    "SerpAPI API key is not set. Set SERPAPI_API_KEY in your environment. "
                    "Sign up at https://serpapi.com"
                )
            return json.dumps(
                {"error": "SERPAPI_API_KEY is not configured", "query": query},
                ensure_ascii=False,
            )

        params = {
            "api_key": api_key,
            "q": query,
            "num": num_results,
            "engine": "google",
        }

        try:
            resp = requests.get(_SERPAPI_ENDPOINT, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            logger.error(
                "SerpAPI returned HTTP %s: %s",
                e.response.status_code,
                e.response.text,
            )
            return json.dumps(
                {
                    "error": f"SerpAPI API error: HTTP {e.response.status_code}",
                    "query": query,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error("SerpAPI search failed: %s: %s", type(e).__name__, e)
            return json.dumps(
                {"error": str(e), "query": query},
                ensure_ascii=False,
            )

        organic = data.get("organic_results", [])
        if not organic:
            return json.dumps(
                {"error": "No results found", "query": query},
                ensure_ascii=False,
            )

        normalized_results = [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "content": r.get("snippet", ""),
            }
            for r in organic[:num_results]
        ]

        output = {
            "query": query,
            "total_results": len(normalized_results),
            "results": normalized_results,
        }
        return json.dumps(output, indent=2, ensure_ascii=False)

    return web_fetch


def build_serpapi_tools() -> list[Any]:
    """Return all SerpAPI-based tools."""
    return [create_web_fetch_tool()]


# Module-level convenience instance.
web_fetch = create_web_fetch_tool()
