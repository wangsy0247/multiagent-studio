"""Search tools with graceful fallbacks when no API keys are available."""
from __future__ import annotations

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


def _validate_pdf_safety(pdf_data: bytes, paper_id: str) -> str | None:
    """Validate the safety of downloaded PDF data.

    Returns ``None`` if the PDF passes all checks, or an error message
    string describing the first failed check.
    """
    # ── 1. Magic bytes: must start with %PDF- ──────────────────────────────
    if not pdf_data[:5] == b"%PDF-":
        # Check for common executable headers
        if pdf_data[:4] == b"\x7fELF":
            return f"Safety blocked: file '{paper_id}' is an ELF executable, not a PDF."
        if pdf_data[:2] in (b"MZ", b"PE"):
            return f"Safety blocked: file '{paper_id}' is a Windows executable, not a PDF."
        if pdf_data[:4] == b"\x89PNG":
            return f"Safety blocked: file '{paper_id}' is a PNG image, not a PDF."
        if pdf_data[:2] == b"\x1f\x8b":
            return f"Safety blocked: file '{paper_id}' is a gzip archive, not a PDF."
        if pdf_data[:4] == b"PK\x03\x04":
            return f"Safety blocked: file '{paper_id}' is a ZIP archive, not a PDF."
        # Generic fail
        magic = pdf_data[:20].hex()
        return (
            f"Safety blocked: file '{paper_id}' does not have a valid PDF header "
            f"(expected %%PDF-, got bytes: {magic})."
        )

    # ── 2. Size limit: max 100 MB ──────────────────────────────────────────
    _MAX_PDF_SIZE = 100 * 1024 * 1024  # 100 MB
    if len(pdf_data) > _MAX_PDF_SIZE:
        return (
            f"Safety blocked: PDF size {len(pdf_data) / 1024 / 1024:.1f} MB "
            f"exceeds maximum {_MAX_PDF_SIZE / 1024 / 1024:.0f} MB."
        )

    # ── 3. Embedded dangerous content detection ────────────────────────────
    # Scan raw bytes for suspicious patterns (case-insensitive binary search)
    _SUSPICIOUS_PATTERNS: list[tuple[bytes, str]] = [
        (b"/JavaScript", "embedded JavaScript action"),
        (b"/JS ", "embedded JavaScript action"),
        (b"/Launch ", "embedded launch action (arbitrary command execution)"),
        (b"/EmbeddedFile", "embedded file attachment"),
        (b"/RichMedia", "embedded rich media / Flash content"),
        (b"/OpenAction", "auto-open action on PDF load"),
        (b"/AA ", "additional automatic action"),
        (b"eval(", "JavaScript eval() in PDF stream"),
        (b"exec(", "shell exec() in PDF stream"),
        (b"os.system", "Python os.system call in PDF stream"),
        (b"subprocess", "Python subprocess call in PDF stream"),
        (b"rm -rf", "recursive delete command in PDF stream"),
        (b"curl http", "network exfiltration via curl in PDF stream"),
        (b"wget http", "network exfiltration via wget in PDF stream"),
    ]

    # Convert PDF to lowercase for case-insensitive binary search;
    # only scan the first 1MB to keep it fast.
    scan_chunk = pdf_data[:1024 * 1024].lower()
    for pattern, description in _SUSPICIOUS_PATTERNS:
        if pattern.lower() in scan_chunk:
            return (
                f"Safety blocked: detected {description} in '{paper_id}'. "
                f"This PDF may be malicious."
            )

    # ── 4. PDF trailer check ───────────────────────────────────────────────
    if b"%%EOF" not in pdf_data[-4096:]:
        logger.warning(
            "PDF '%s' is missing %%EOF trailer — may be truncated or malformed.",
            paper_id,
        )

    return None  # All checks passed


# ── Python script that runs inside the sandbox to download + validate a PDF ──
_PAPER_DOWNLOAD_SCRIPT = r"""
import json, re, sys, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

arxiv_input, save_dir = sys.argv[1], sys.argv[2]

# ── 1. Parse paper ID ──────────────────────────────────────────────────────
patterns = [
    r"arxiv\.org/abs/([0-9]+\.[0-9]+(?:v[0-9]+)?)",
    r"arxiv\.org/pdf/([0-9]+\.[0-9]+(?:v[0-9]+)?)",
    r"^([0-9]+\.[0-9]+(?:v[0-9]+)?)$",
]
paper_id = None
for pat in patterns:
    m = re.search(pat, arxiv_input.strip())
    if m:
        paper_id = m.group(1)
        break
if paper_id is None:
    print(json.dumps({"error": f"Could not parse arXiv ID from '{arxiv_input}'"}))
    sys.exit(0)

pdf_url = f"https://arxiv.org/pdf/{paper_id}"

# ── 2. Download PDF ────────────────────────────────────────────────────────
try:
    req = urllib.request.Request(
        pdf_url,
        headers={"User-Agent": "MultiAgent-Studio/2.0 (mailto:research@example.com)"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        pdf_data = resp.read()
except Exception as exc:
    print(json.dumps({"error": f"Download failed: {exc}"}))
    sys.exit(0)

if len(pdf_data) < 1000:
    print(json.dumps({"error": f"Downloaded data too small ({len(pdf_data)} bytes)"}))
    sys.exit(0)

# ── 3. Safety validation ───────────────────────────────────────────────────
# Magic bytes: must be %PDF-
if pdf_data[:5] != b"%PDF-":
    magic = pdf_data[:20].hex()
    print(json.dumps({"error": f"Safety blocked: not a valid PDF (got bytes: {magic})"}))
    sys.exit(0)

# Size limit: 100 MB
_MAX = 100 * 1024 * 1024
if len(pdf_data) > _MAX:
    print(json.dumps({"error": f"Safety blocked: PDF too large ({len(pdf_data)/1024/1024:.1f} MB)"}))
    sys.exit(0)

# Suspicious content scan (first 1 MB, case-insensitive)
_SUSPICIOUS = [
    (b"/JavaScript", "embedded JavaScript"),
    (b"/JS ", "embedded JavaScript"),
    (b"/Launch ", "embedded launch action"),
    (b"/EmbeddedFile", "embedded file attachment"),
    (b"/RichMedia", "embedded rich media"),
    (b"/OpenAction", "auto-open action"),
    (b"/AA ", "automatic action"),
    (b"eval(", "JavaScript eval()"),
    (b"exec(", "shell exec()"),
    (b"os.system", "Python os.system"),
    (b"subprocess", "Python subprocess"),
    (b"rm -rf", "recursive delete"),
    (b"curl http", "network exfiltration (curl)"),
    (b"wget http", "network exfiltration (wget)"),
]
scan = pdf_data[:1024 * 1024].lower()
for pat, desc in _SUSPICIOUS:
    if pat.lower() in scan:
        print(json.dumps({"error": f"Safety blocked: detected {desc}"}))
        sys.exit(0)

# ── 4. Save PDF ────────────────────────────────────────────────────────────
save_path = Path(save_dir) / f"{paper_id}.pdf"
save_path.parent.mkdir(parents=True, exist_ok=True)
save_path.write_bytes(pdf_data)
size_kb = len(pdf_data) / 1024

# ── 5. Fetch title from arXiv API ──────────────────────────────────────────
title = paper_id
try:
    api_url = f"http://export.arxiv.org/api/query?id_list={paper_id}"
    api_req = urllib.request.Request(api_url, headers={"User-Agent": "MultiAgent-Studio/2.0"})
    with urllib.request.urlopen(api_req, timeout=30) as api_resp:
        root = ET.fromstring(api_resp.read())
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is not None:
        t = entry.findtext("atom:title", "", ns)
        if t:
            title = t.strip().replace("\n", " ")
except Exception:
    pass

print(json.dumps({
    "success": True,
    "title": title,
    "paper_id": paper_id,
    "file": str(save_path),
    "size_kb": round(size_kb, 1),
    "abs_url": f"https://arxiv.org/abs/{paper_id}",
}))
"""


def create_paper_download_tool() -> Any:
    """Create the ``paper_download`` tool — download full-text PDFs from arXiv in the sandbox."""

    @tool
    async def paper_download(arxiv_url_or_id: str, save_dir: str = "/mnt/user-data/workspace") -> str:
        """Download the full PDF of an academic paper from arXiv.

        Runs entirely inside the sandbox for isolation.  Accepts either a full
        arXiv URL (abs or pdf) or a bare paper ID.  The PDF is saved to
        *save_dir* using the paper ID as the filename.

        Args:
            arxiv_url_or_id: arXiv URL like https://arxiv.org/abs/2401.12345,
                https://arxiv.org/pdf/2401.12345, or bare ID like 2401.12345.
            save_dir: Directory to save the PDF (default: /mnt/user-data/workspace).

        Returns:
            Status message with the saved file path, paper title, and file size.
        """
        import json as _json
        import shlex

        from harness.tools.sandbox_tools import _get_sandbox

        try:
            sandbox = await _get_sandbox()
        except Exception as exc:
            return f"Error: sandbox unavailable — {exc}"

        # Write the download script into the sandbox workspace
        script_path = "/mnt/user-data/workspace/_paper_download.py"
        try:
            await sandbox.write_file(script_path, _PAPER_DOWNLOAD_SCRIPT)
        except Exception as exc:
            return f"Error: failed to write download script to sandbox: {exc}"

        # Execute the script in the sandbox
        cmd = (
            f"python3 {shlex.quote(script_path)} "
            f"{shlex.quote(arxiv_url_or_id)} "
            f"{shlex.quote(save_dir)}"
        )
        try:
            raw_output = await sandbox.execute_command(cmd, timeout=180)
            output = sandbox.sanitize_output(raw_output) if hasattr(sandbox, "sanitize_output") else raw_output
        except Exception as exc:
            return f"Error: sandbox execution failed — {exc}"

        # Clean up the script
        try:
            await sandbox.execute_command(f"rm -f {shlex.quote(script_path)}", timeout=5)
        except Exception:
            pass

        # Parse JSON output from the script (last non-empty line)
        stdout = output.strip()
        if not stdout:
            return "Error: no output from sandbox download script."

        try:
            data = _json.loads(stdout)
        except _json.JSONDecodeError:
            return f"Error: could not parse sandbox output:\n{stdout[:1000]}"

        if data.get("error"):
            return f"Error: {data['error']}"

        if data.get("success"):
            return (
                f"Paper downloaded successfully.\n"
                f"  Title: {data['title']}\n"
                f"  arXiv ID: {data['paper_id']}\n"
                f"  File: {data['file']}\n"
                f"  Size: {data['size_kb']} KB\n"
                f"  arXiv page: {data['abs_url']}"
            )

        return f"Error: unexpected sandbox output:\n{stdout[:1000]}"

    return paper_download


def build_search_tools() -> list[Any]:
    """Return all search tools."""
    return [
        create_web_search_tool(),
        create_arxiv_search_tool(),
        create_web_fetch_tool(),
        create_paper_download_tool(),
    ]


# Module-level convenience instances.
web_search = create_web_search_tool()
arxiv_search = create_arxiv_search_tool()
web_fetch = create_web_fetch_tool()
paper_download = create_paper_download_tool()
