"""Tests for sandbox provider selection and runtime config."""
from __future__ import annotations

import pytest

from harness.services.local_sandbox_provider import LocalSandboxProvider
from harness.services.open_sandbox_provider import OpenSandboxProvider
from harness.services.sandbox_provider import (
    _load_sandbox_yaml_section,
    _resolve_sandbox_use,
    get_sandbox_provider,
    reset_sandbox_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_sandbox_provider()
    yield
    reset_sandbox_provider()


class TestResolveSandboxUse:
    def test_yaml_use_takes_precedence(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._load_sandbox_yaml_section",
            lambda: {"use": "harness.services.open_sandbox_provider:OpenSandboxProvider"},
        )
        assert _resolve_sandbox_use().endswith("OpenSandboxProvider")

    def test_empty_use_returns_local(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._load_sandbox_yaml_section",
            lambda: {},
        )
        assert _resolve_sandbox_use() == ""


class TestOpenSandboxRuntimeConfig:
    def test_opensandbox_image_read_from_config_yaml(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._load_sandbox_yaml_section",
            lambda: {
                "use": "harness.services.open_sandbox_provider:OpenSandboxProvider",
                "image": "python:3.12-slim",
                "server_url": "http://localhost:8080",
                "resource_cpu": "2",
                "resource_memory": "4Gi",
            },
        )
        monkeypatch.setattr(
            "harness.services.sandbox_provider._opensandbox_server_available",
            lambda url: True,
        )

        provider = get_sandbox_provider()
        assert isinstance(provider, OpenSandboxProvider)
        assert provider.image == "python:3.12-slim"
        assert provider.resource == {"cpu": "2", "memory": "4Gi"}

    def test_explicit_kwargs_override_config(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._load_sandbox_yaml_section",
            lambda: {
                "use": "harness.services.open_sandbox_provider:OpenSandboxProvider",
                "image": "python:3.12-slim",
            },
        )
        monkeypatch.setattr(
            "harness.services.sandbox_provider._opensandbox_server_available",
            lambda url: True,
        )

        provider = get_sandbox_provider(image="python:3.10-slim")
        assert isinstance(provider, OpenSandboxProvider)
        assert provider.image == "python:3.10-slim"

    def test_opensandbox_unavailable_falls_back_to_local(self, monkeypatch):
        monkeypatch.setattr(
            "harness.services.sandbox_provider._load_sandbox_yaml_section",
            lambda: {
                "use": "harness.services.open_sandbox_provider:OpenSandboxProvider",
            },
        )
        monkeypatch.setattr(
            "harness.services.sandbox_provider._opensandbox_server_available",
            lambda url: False,
        )

        provider = get_sandbox_provider()
        assert isinstance(provider, LocalSandboxProvider)


class TestLocalProvider:
    def test_empty_use_defaults_to_local(self):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "harness.services.sandbox_provider._load_sandbox_yaml_section",
            lambda: {},
        )
        provider = get_sandbox_provider()
        assert isinstance(provider, LocalSandboxProvider)
        monkeypatch.undo()
