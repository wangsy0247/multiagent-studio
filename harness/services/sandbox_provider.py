"""Sandbox provider abstraction (DeerFlow-style).

Provides a pluggable sandbox layer so that file and shell tools can run
against either a Docker container or the local filesystem depending on
configuration.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Sandbox(ABC):
    """Abstract sandbox environment for executing code and file operations."""

    @abstractmethod
    async def execute_command(self, command: str | list[str], timeout: int = 30) -> str:
        """Execute a shell command and return its output."""
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
        For DockerSandbox this returns the in-container path (often identical to
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


def get_sandbox_provider(**kwargs: Any) -> SandboxProvider:
    """Return the configured sandbox provider singleton.

    The provider is lazily created from ``config.yaml``:

        sandbox:
          use: harness.services.docker_sandbox_provider:DockerSandboxProvider
    """
    global _default_provider, _default_provider_kwargs

    if _default_provider is not None and _default_provider_kwargs == kwargs:
        return _default_provider

    from harness.config import load_config

    cfg = load_config()
    use = cfg.sandbox_use if hasattr(cfg, "sandbox_use") else ""

    if not use:
        # Default to local sandbox provider when no provider is configured.
        from harness.services.local_sandbox_provider import LocalSandboxProvider

        _default_provider = LocalSandboxProvider(**kwargs)
        _default_provider_kwargs = kwargs
        return _default_provider

    from harness.utils import resolve_variable

    provider_cls = resolve_variable(use, SandboxProvider)
    _default_provider = provider_cls(**kwargs)
    _default_provider_kwargs = kwargs
    return _default_provider


def reset_sandbox_provider() -> None:
    """Reset the cached sandbox provider."""
    global _default_provider, _default_provider_kwargs
    _default_provider = None
    _default_provider_kwargs = None
