"""Sandbox provider abstraction (harness-style).

Provides a pluggable sandbox layer so that file and shell tools can run
against either an OpenSandbox container or the local filesystem depending on
configuration.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Sandbox(ABC):
    """Abstract sandbox environment for executing code and file operations."""

    @abstractmethod
    async def execute_command(
        self, command: str | list[str], timeout: int = 30, cwd: str = "",
    ) -> str:
        """Execute a shell command and return its output.

        Args:
            command: Shell command or argument list.
            timeout: Max execution time in seconds.
            cwd: Working directory (virtual path). Empty = default workspace.
        """
        pass

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """Read a file from the sandbox.

        ``path`` is a virtual path such as ``/mnt/user-data/workspace/foo.txt``.
        """
        pass

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None:
        """Write content to a file in the sandbox.

        ``path`` is a virtual path such as ``/mnt/user-data/workspace/foo.txt``.
        """
        pass

    @abstractmethod
    async def list_dir(self, path: str) -> list[str]:
        """List a directory in the sandbox.

        ``path`` is a virtual path such as ``/mnt/user-data/workspace``.
        """
        pass

    @abstractmethod
    async def glob(self, path: str, pattern: str) -> list[str]:
        """Return matching file/directory paths under ``path`` for ``pattern``.

        ``path`` is a virtual path such as ``/mnt/user-data/workspace``.
        """
        pass

    @abstractmethod
    async def grep(
        self,
        path: str,
        pattern: str,
        *,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> list[tuple[str, int, str]]:
        """Search ``pattern`` under ``path`` and return (relative_path, line_number, line).

        ``path`` is a virtual path such as ``/mnt/user-data/workspace``.
        """
        pass

    def resolve_path(self, virtual_path: str) -> str:
        """Resolve a virtual path to the sandbox-native path.

        For LocalSandbox this returns the host filesystem path.
        For OpenSandbox this returns the in-container path (often identical to
        the virtual path because the container bind-mounts at those prefixes).
        """
        raise NotImplementedError

    def sanitize_output(self, output: str) -> str:
        """Sanitize command output by masking physical paths.

        The default implementation returns the output unchanged; providers may
        override this to reverse-resolve host paths back to virtual paths.
        """
        return output


class SandboxProvider(ABC):
    """Abstract factory for acquiring and releasing sandbox instances."""

    @abstractmethod
    async def acquire(
        self, thread_id: str, workspace: str, *, user_id: str | None = None
    ) -> Sandbox:
        """Acquire a sandbox for the given thread, workspace, and optional user."""
        pass

    @abstractmethod
    async def release(self, sandbox: Sandbox) -> None:
        """Release a previously acquired sandbox."""
        pass


_default_provider: SandboxProvider | None = None
_default_provider_kwargs: dict[str, Any] | None = None


def _resolve_sandbox_use() -> str:
    """Resolve which provider to use.

    Priority:
      1. ``config.yaml`` → ``sandbox.use``
      2. Environment / ``.env`` → ``HARNESS_SANDBOX_USE``
      3. Empty string → LocalSandboxProvider
    """
    # 1. YAML config (config.yaml) takes highest priority
    sandbox_cfg = _load_sandbox_yaml_section()
    use = sandbox_cfg.get("use", "")
    if use:
        return str(use)

    # 2. Pydantic env settings
    from harness.config import load_config

    env_cfg = load_config()
    use = getattr(env_cfg, "sandbox_use", "")
    return str(use) if use else ""


def _load_sandbox_yaml_section() -> dict[str, Any]:
    """Return the ``sandbox`` section from ``config.yaml`` if it exists."""
    yaml_path = Path(__file__).resolve().parent.parent / "config.yaml"
    try:
        from harness.config.config_manager import ConfigManager

        cfg_mgr = ConfigManager(config_path=str(yaml_path))
        cfg_mgr.load()
        sandbox_cfg = cfg_mgr.get("sandbox", {})
        return sandbox_cfg if isinstance(sandbox_cfg, dict) else {}
    except Exception:
        return {}


def _opensandbox_server_available(server_url: str) -> bool:
    """Return True if the OpenSandbox server health endpoint responds."""
    try:
        import httpx

        resp = httpx.get(f"{server_url}/health", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


def get_sandbox_provider(**kwargs: Any) -> SandboxProvider:
    """Return the configured sandbox provider singleton.

    The provider is lazily created from ``config.yaml``. Recommended provider:

        sandbox:
          use: harness.services.open_sandbox_provider:OpenSandboxProvider

    If OpenSandbox is selected but its server is unreachable, the provider falls
    back to ``LocalSandboxProvider`` so the service stays usable during
    development.
    """
    global _default_provider, _default_provider_kwargs

    if _default_provider is not None and _default_provider_kwargs == kwargs:
        return _default_provider

    use = _resolve_sandbox_use()

    provider_kwargs = dict(kwargs)

    if not use:
        # Default to local sandbox provider when no provider is configured.
        from harness.services.local_sandbox_provider import LocalSandboxProvider

        _default_provider = LocalSandboxProvider(**provider_kwargs)
        _default_provider_kwargs = provider_kwargs
        logger.info("Using LocalSandboxProvider (no sandbox provider configured)")
        return _default_provider

    from harness.utils import resolve_variable

    provider_cls = resolve_variable(use, SandboxProvider)

    # OpenSandbox-specific runtime settings can be configured from config.yaml.
    if use.endswith("OpenSandboxProvider"):
        from harness.config import load_config

        env_cfg = load_config()
        defaults = {
            "image": getattr(env_cfg, "sandbox_image", "swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.12"),
            "server_url": getattr(env_cfg, "sandbox_server_url", "http://localhost:8080"),
            "api_key": getattr(env_cfg, "sandbox_api_key", ""),
            "resource_cpu": getattr(env_cfg, "sandbox_resource_cpu", "1"),
            "resource_memory": getattr(env_cfg, "sandbox_resource_memory", "2Gi"),
            "timeout_minutes": getattr(env_cfg, "sandbox_timeout_minutes", 30),
        }
        sandbox_cfg = _load_sandbox_yaml_section()
        for key in defaults:
            if key not in provider_kwargs:
                provider_kwargs[key] = sandbox_cfg.get(key, defaults[key])

        # Graceful fallback: if OpenSandbox server is unreachable, use local.
        if not _opensandbox_server_available(provider_kwargs["server_url"]):
            logger.warning(
                "OpenSandbox server at %s is unreachable. "
                "Falling back to LocalSandboxProvider.",
                provider_kwargs["server_url"],
            )
            from harness.services.local_sandbox_provider import LocalSandboxProvider

            _default_provider = LocalSandboxProvider(**provider_kwargs)
            _default_provider_kwargs = provider_kwargs
            return _default_provider

    _default_provider = provider_cls(**provider_kwargs)
    _default_provider_kwargs = provider_kwargs
    logger.info("Using sandbox provider: %s", use)
    return _default_provider


def reset_sandbox_provider() -> None:
    """Reset the cached sandbox provider."""
    global _default_provider, _default_provider_kwargs
    _default_provider = None
    _default_provider_kwargs = None
