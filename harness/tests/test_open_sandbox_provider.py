"""Tests for the OpenSandbox provider."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from unittest.mock import ANY

from harness.config.paths import Paths, set_paths
from harness.services.local_sandbox_provider import LocalSandboxProvider
from harness.services.open_sandbox_provider import OpenSandbox, OpenSandboxProvider
from harness.services.sandbox_provider import (
    _opensandbox_server_available,
    get_sandbox_provider,
    reset_sandbox_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_sandbox_provider()
    yield
    reset_sandbox_provider()


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Use a temporary directory as the Harness data root."""
    set_paths(Paths(base_dir=str(tmp_path)))
    yield
    set_paths(Paths())


def _make_execution(*, stdout: list[str] | None = None, stderr: list[str] | None = None, exit_code: int = 0) -> Any:
    """Build a minimal OpenSandbox Execution-like object."""
    exec_mock = MagicMock()
    exec_mock.exit_code = exit_code

    def _log(texts):
        return [MagicMock(text=t) for t in (texts or [])]

    logs_mock = MagicMock()
    logs_mock.stdout = _log(stdout)
    logs_mock.stderr = _log(stderr)
    exec_mock.logs = logs_mock
    return exec_mock


class TestOpenSandboxProvider:
    def test_build_volumes(self, tmp_paths, tmp_path):
        provider = OpenSandboxProvider()
        volumes = provider._build_volumes("thread-1", user_id="u1")

        assert len(volumes) == 4
        names = {v.name for v in volumes}
        assert names == {
            "harness-thread-1-workspace",
            "harness-thread-1-uploads",
            "harness-thread-1-outputs",
            "harness-thread-1-acp",
        }

        by_mount = {v.mount_path: v for v in volumes}
        assert by_mount["/mnt/user-data/workspace"].host.path == str(
            tmp_path / "users" / "u1" / "threads" / "thread-1" / "user-data" / "workspace"
        )
        assert by_mount["/mnt/acp-workspace"].host.path == str(
            tmp_path / "users" / "u1" / "threads" / "thread-1" / "acp-workspace"
        )

    @pytest.mark.asyncio
    async def test_acquire_creates_sandbox(self, tmp_paths, monkeypatch):
        provider = OpenSandboxProvider()
        mock_sbx = MagicMock()
        create_mock = AsyncMock(return_value=mock_sbx)
        monkeypatch.setattr(
            "harness.services.open_sandbox_provider.OpenSandboxClient.create",
            create_mock,
        )

        sandbox = await provider.acquire("thread-1", "/workspace", user_id="u1")

        assert isinstance(sandbox, OpenSandbox)
        assert sandbox.thread_id == "thread-1"
        assert sandbox.user_id == "u1"
        create_mock.assert_awaited_once()
        args, kwargs = create_mock.call_args
        assert args[0] == "swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.12"
        assert kwargs["resource"] == {"cpu": "1", "memory": "2Gi"}
        assert len(kwargs["volumes"]) == 4

    @pytest.mark.asyncio
    async def test_release_kills_sandbox(self):
        provider = OpenSandboxProvider()
        mock_sbx = MagicMock()
        mock_sbx.kill = AsyncMock()
        mock_sbx.close = AsyncMock()
        sandbox = OpenSandbox("thread-1", mock_sbx)

        await provider.release(sandbox)

        mock_sbx.kill.assert_awaited_once()
        mock_sbx.close.assert_awaited_once()


class TestOpenSandboxOperations:
    @pytest.fixture
    def sandbox(self):
        mock_sbx = MagicMock()
        return OpenSandbox("thread-1", mock_sbx, user_id="u1")

    @pytest.mark.asyncio
    async def test_execute_command(self, sandbox):
        sandbox._sbx.commands.run = AsyncMock(
            return_value=_make_execution(stdout=["hello"], stderr=["warn"], exit_code=0)
        )

        result = await sandbox.execute_command("echo hello")

        sandbox._sbx.commands.run.assert_awaited_once_with(
            "echo hello",
            opts=ANY,
        )
        assert result.startswith("[exit:0]")
        assert "hello" in result
        assert "warn" in result

    @pytest.mark.asyncio
    async def test_execute_command_with_list(self, sandbox):
        sandbox._sbx.commands.run = AsyncMock(
            return_value=_make_execution(stdout=["ok"], exit_code=0)
        )

        await sandbox.execute_command(["echo", "hello world"])

        sandbox._sbx.commands.run.assert_awaited_once()
        call_args = sandbox._sbx.commands.run.call_args
        assert call_args[0][0] == "echo 'hello world'"

    @pytest.mark.asyncio
    async def test_read_file(self, sandbox):
        sandbox._sbx.files.read_file = AsyncMock(return_value="file content")

        content = await sandbox.read_file("/mnt/user-data/workspace/foo.txt")

        assert content == "file content"
        sandbox._sbx.files.read_file.assert_awaited_once_with("/mnt/user-data/workspace/foo.txt")

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, sandbox):
        sandbox._sbx.files.read_file = AsyncMock(side_effect=Exception("404 Not Found"))

        with pytest.raises(FileNotFoundError):
            await sandbox.read_file("/mnt/user-data/workspace/missing.txt")

    @pytest.mark.asyncio
    async def test_write_file(self, sandbox):
        sandbox._sbx.files.write_files = AsyncMock()

        await sandbox.write_file("/mnt/user-data/workspace/foo.txt", "hello")

        sandbox._sbx.files.write_files.assert_awaited_once()
        entries = sandbox._sbx.files.write_files.call_args[0][0]
        assert len(entries) == 1
        assert entries[0].path == "/mnt/user-data/workspace/foo.txt"
        assert entries[0].data == "hello"

    @pytest.mark.asyncio
    async def test_list_dir(self, sandbox):
        entries = [
            MagicMock(path="/mnt/user-data/workspace/", entry_type="directory"),
            MagicMock(path="/mnt/user-data/workspace/foo.txt", entry_type="file"),
            MagicMock(path="/mnt/user-data/workspace/bar", entry_type="directory"),
        ]
        sandbox._sbx.files.search = AsyncMock(return_value=entries)

        result = await sandbox.list_dir("/mnt/user-data/workspace")

        assert result == ["dir: bar", "file: foo.txt"]

    @pytest.mark.asyncio
    async def test_glob(self, sandbox):
        entries = [
            MagicMock(path="/mnt/user-data/workspace/a.py"),
            MagicMock(path="/mnt/user-data/workspace/b.py"),
        ]
        sandbox._sbx.files.search = AsyncMock(return_value=entries)

        result = await sandbox.glob("/mnt/user-data/workspace", "*.py")

        assert result == ["/mnt/user-data/workspace/a.py", "/mnt/user-data/workspace/b.py"]

    @pytest.mark.asyncio
    async def test_grep(self, sandbox):
        sandbox._sbx.commands.run = AsyncMock(
            return_value=_make_execution(
                stdout=[
                    "/mnt/user-data/workspace/a.py:10:def foo():",
                    "/mnt/user-data/workspace/b.py:3:foo = 1",
                ],
                exit_code=0,
            )
        )

        result = await sandbox.grep("/mnt/user-data/workspace", "foo")

        assert len(result) == 2
        assert result[0] == ("/mnt/user-data/workspace/a.py", 10, "def foo():")
        assert result[1] == ("/mnt/user-data/workspace/b.py", 3, "foo = 1")


class TestFallbackChain:
    def test_opensandbox_server_available_false_for_unreachable(self):
        assert _opensandbox_server_available("http://localhost:59999") is False

    def test_opensandbox_server_available_true_when_healthy(self, monkeypatch):
        import httpx

        class OKResponse:
            status_code = 200

        monkeypatch.setattr(httpx, "get", lambda url, timeout: OKResponse())
        assert _opensandbox_server_available("http://localhost:8080") is True

    def test_get_provider_fallback_to_local_when_opensandbox_unreachable(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._resolve_sandbox_use",
            lambda: "harness.services.open_sandbox_provider:OpenSandboxProvider",
        )
        monkeypatch.setattr(
            "harness.services.sandbox_provider._opensandbox_server_available",
            lambda url: False,
        )

        provider = get_sandbox_provider()

        assert isinstance(provider, LocalSandboxProvider)

    def test_get_provider_uses_opensandbox_when_server_reachable(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._resolve_sandbox_use",
            lambda: "harness.services.open_sandbox_provider:OpenSandboxProvider",
        )
        monkeypatch.setattr(
            "harness.services.sandbox_provider._opensandbox_server_available",
            lambda url: True,
        )

        provider = get_sandbox_provider()

        assert isinstance(provider, OpenSandboxProvider)

    def test_unknown_provider_raises(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._resolve_sandbox_use",
            lambda: "harness.services.nonexistent:NonexistentProvider",
        )
        monkeypatch.setattr(
            "harness.services.sandbox_provider._opensandbox_server_available",
            lambda url: True,
        )

        with pytest.raises(Exception):
            get_sandbox_provider()
