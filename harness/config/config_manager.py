"""YAML-based configuration manager with mtime hot-reload.

Provides a thread-safe ConfigManager that loads YAML configuration,
interpolates environment variables, polls for file changes, and
notifies registered callbacks on reload.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional YAML support
# ---------------------------------------------------------------------------

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


def _resolve_env(match: re.Match) -> str:
    """Replace ``$VAR`` or ``${VAR}`` with the environment variable value.

    Returns the empty string if the variable is not set.
    """
    return os.environ.get(match.group(1), "")


def _interpolate_env(value: str) -> str:
    """Interpolate ``$VAR`` and ``${VAR}`` patterns in *value*."""
    return re.sub(r"\$\{?(\w+)\}?", _resolve_env, value)


def _interpolate_env_recursive(obj: Any) -> Any:
    """Recursively interpolate environment variables in strings.

    Works on nested dicts and lists so that leaf strings always have ``$VAR``
    references resolved.
    """
    if isinstance(obj, dict):
        return {k: _interpolate_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_env_recursive(v) for v in obj]
    if isinstance(obj, str):
        return _interpolate_env(obj)
    return obj


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Thread-safe YAML configuration manager with mtime-based hot-reload.

    Parameters
    ----------
    config_path:
        Path to the YAML configuration file (default ``config.yaml``).
    reload_interval:
        Seconds between mtime polls while the background watcher is running
        (default ``3.0``).
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        *,
        reload_interval: float = 3.0,
    ) -> None:
        self.config_path: str = config_path
        self.reload_interval: float = reload_interval

        self._data: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._mtime: float = 0.0
        self._watcher_task: asyncio.Task | None = None
        self._callbacks: list[Callable[[], None]] = []

    # -- public API --------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """Load (or reload) the YAML file and return the parsed configuration.

        On ``FileNotFoundError`` the internal data is reset to an empty dict
        and the stored mtime is set to 0 so that a subsequent
        :meth:`reload_if_changed` will pick up the file when it appears.
        """
        with self._lock:
            try:
                parsed = self._parse_yaml()
                self._data = parsed or {}
                self._mtime = os.path.getmtime(self.config_path)
                logger.info("Configuration loaded from %s", self.config_path)
            except FileNotFoundError:
                self._data = {}
                self._mtime = 0.0
                logger.warning(
                    "Configuration file not found: %s (using defaults)",
                    self.config_path,
                )
            return dict(self._data)

    def reload_if_changed(self) -> bool:
        """Check the file mtime and reload if it has changed.

        Returns ``True`` when the configuration was actually reloaded,
        ``False`` otherwise (file unchanged, file missing, or parse error).

        On YAML parse errors the previous data is preserved and a warning is
        logged.
        """
        with self._lock:
            try:
                current_mtime = os.path.getmtime(self.config_path)
            except FileNotFoundError:
                return False

            if current_mtime <= self._mtime:
                return False

            try:
                parsed = self._parse_yaml()
            except Exception:
                logger.warning(
                    "Failed to parse YAML configuration from %s (keeping old data)",
                    self.config_path,
                    exc_info=True,
                )
                return False

            self._data = parsed or {}
            self._mtime = current_mtime
            logger.info("Configuration reloaded from %s", self.config_path)

        # Fire callbacks outside the lock to avoid deadlock if a callback
        # itself tries to acquire the lock.
        for cb in self._callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Config change callback raised an error")
        return True

    def start_watcher(self) -> None:
        """Start a background asyncio task that polls :meth:`reload_if_changed`.

        If a watcher is already running this is a no-op.
        """
        if self._watcher_task is not None and not self._watcher_task.done():
            logger.debug("Config watcher already running")
            return

        self._watcher_task = asyncio.create_task(self._watcher_loop())

    def stop_watcher(self) -> None:
        """Cancel the background watcher task if it is running."""
        task = self._watcher_task
        if task is not None and not task.done():
            task.cancel()
            self._watcher_task = None

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback that is fired after each successful reload.

        The callback is called *outside* the internal lock.
        """
        self._callbacks.append(callback)

    def get(self, key: str, default: Any = None) -> Any:
        """Thread-safe nested key access using dot notation.

        Example::

            config.get("memory.enabled")          # → _data["memory"]["enabled"]
            config.get("nonexistent.key", False)   # → False
        """
        with self._lock:
            parts = key.split(".")
            current: Any = self._data
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return default
                if current is None:
                    return default
            return current

    def __getitem__(self, key: str) -> Any:
        """Dict-style access: ``config["memory"]``."""
        with self._lock:
            return self._data[key]

    def __contains__(self, key: str) -> bool:
        """``"memory" in config``."""
        with self._lock:
            return key in self._data

    @property
    def config_version(self) -> str | None:
        """Return the ``config_version`` field from configuration data."""
        return self.get("config_version")

    # -- internals ---------------------------------------------------------

    def _parse_yaml(self) -> dict[str, Any]:
        """Parse the YAML file and interpolate environment variables.

        Raises
        ------
        FileNotFoundError
            When the config file does not exist.
        yaml.YAMLError
            When the file contains invalid YAML.
        RuntimeError
            When PyYAML is not installed.
        """
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required. Install it with: pip install pyyaml"
            )

        with open(self.config_path, "r") as fh:
            raw: Any = yaml.safe_load(fh)

        if raw is None:
            return {}
        if not isinstance(raw, dict):
            logger.warning(
                "YAML root is not a dict (got %s); wrapping in empty config",
                type(raw).__name__,
            )
            return {}

        return _interpolate_env_recursive(raw)  # type: ignore[no-any-return]

    async def _watcher_loop(self) -> None:
        """Background loop that polls :meth:`reload_if_changed`."""
        try:
            while True:
                await asyncio.sleep(self.reload_interval)
                self.reload_if_changed()
        except asyncio.CancelledError:
            logger.debug("Config watcher cancelled")
            raise
        except Exception:
            logger.exception("Config watcher crashed unexpectedly")
            raise
