"""mem0 client singleton — initialized once from config.

Usage::

    from harness.memory.mem0_client import get_mem0, is_mem0_enabled
    if is_mem0_enabled():
        mem0 = get_mem0()
        results = mem0.search(query, filters={"user_id": uid, "agent_id": aid})
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_mem0_instance: Any | None = None  # mem0.Memory instance
_initialized: bool = False

# Match ``$VAR`` or ``${VAR}`` patterns
_ENV_VAR_RE = re.compile(r"(?<!\$)\$(\w+|\{[^}]+\})")


def _expand_config(config: dict) -> dict:
    """Recursively process config dict: expand ~ in paths, resolve env vars."""

    def _resolve(raw: str) -> str:
        # Expand ~ to user home
        if raw.startswith("~"):
            raw = str(Path(raw).expanduser())
        # Resolve ${VAR} env var references (safety net for unresolved ones)
        def _replacer(m: re.Match) -> str:
            inner = m.group(1)
            var_name = inner[1:-1] if inner.startswith("{") else inner
            return os.environ.get(var_name, m.group(0))

        return _ENV_VAR_RE.sub(_replacer, raw).replace("$$", "$")

    result = {}
    for key, val in config.items():
        if isinstance(val, dict):
            result[key] = _expand_config(val)
        elif isinstance(val, list):
            result[key] = [
                _expand_config(item) if isinstance(item, dict) else
                _resolve(item) if isinstance(item, str) else item
                for item in val
            ]
        elif isinstance(val, str):
            result[key] = _resolve(val)
        else:
            result[key] = val
    return result


def is_mem0_enabled() -> bool:
    """Check if mem0 backend is enabled."""
    from harness.config.memory_config import get_memory_config

    cfg = get_memory_config()
    return cfg.enabled and cfg.backend == "mem0"


def get_mem0() -> Any:
    """Get the singleton mem0.Memory instance.

    Lazily initialized on first call. Returns None if mem0 is not enabled
    or initialization failed.
    """
    global _mem0_instance, _initialized
    if _initialized:
        return _mem0_instance

    from harness.config.memory_config import get_memory_config

    cfg = get_memory_config()

    if not cfg.enabled or cfg.backend != "mem0":
        _initialized = True
        return None

    if not cfg.mem0_config:
        logger.error("mem0 backend enabled but mem0_config is empty")
        _initialized = True
        return None

    # 预处理配置：展开 ~，安全网解析残余 ${VAR}
    expanded_config = _expand_config(cfg.mem0_config)

    try:
        from mem0 import Memory

        _mem0_instance = Memory.from_config(expanded_config)
        logger.info(
            "mem0 client initialized — vs=%s llm=%s embed=%s",
            expanded_config.get("vector_store", {}).get("provider", "?"),
            expanded_config.get("llm", {}).get("config", {}).get("model", "?"),
            expanded_config.get("embedder", {}).get("config", {}).get("model", "?"),
        )
    except ImportError:
        logger.error("mem0ai not installed. Run: pip install mem0ai chromadb")
    except Exception as e:
        logger.error(
            "Failed to initialize mem0: %s (type=%s). "
            "Check mem0_config in config.yaml — llm.openai_base_url and "
            "embedder.openai_base_url must use 'openai_base_url' (not 'api_base').",
            e, type(e).__name__,
        )

    _initialized = True
    return _mem0_instance


def reset_mem0() -> None:
    """Reset the singleton (for testing)."""
    global _mem0_instance, _initialized
    _mem0_instance = None
    _initialized = False
